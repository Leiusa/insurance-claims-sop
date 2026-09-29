"""The insurance SOP engine.

One turn:
  1. understand the caller's message (model, structured output)
  2. record what was said: identity details, hints, memory (code)
  3. global policies: out-of-scope, emotion, human requests, injection (code)
  4. phase handlers call tools through the gateway and may complete several phases (code)
  5. reply from the resulting plan, seeing only phase-scoped context (model)
  6. output guard (code)
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any

from ..config import Settings
from ..harness.events import emit
from ..harness.gateway import Tool, ToolDenied, ToolGateway
from ..harness.guard import GuardPolicy, check_reply, money_values
from ..llm.client import LLMClient, LLMError
from . import prompts
from .data import FixtureRepo, Representative
from .facts import build_fact_sheet
from .nlu_schema import NLUResult
from .resolver import CaseHints, match_claims
from .sop_spec import ENDED, ESCALATED, PHASES, POST_PROCESS, PROCESS_CASE, RESOLVE_INTENT, VERIFY_ID, WORKFLOW
from .state import MemoryItem, Session, SlotValue
from .summary import build_summary, mask_email, render_email
from .verifier import FACTOR_LABELS, FACTORS, REQUIRED_FACTORS, VerificationResult, names_match, normalize, verify

GREETING = (
    "Hi, thanks for contacting claims support. I can help with questions about your insurance claims. "
    "What can I help you with today?"
)

TIMING_TOPICS = {"submission_timing", "appeal_deadline", "next_steps"}
DEFAULT_TOPIC = {
    "denial_question": "denial_reason",
    "status_inquiry": "claim_status",
    "document_submission": "submission_method",
    "next_steps": "next_steps",
    "general_claim_question": "overview",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Plan:
    """What this turn's reply must do, in order. Decided by code, phrased by the model."""

    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    def add(self, kind: str, **params: Any) -> None:
        self.items.append((kind, params))

    def add_front(self, kind: str, **params: Any) -> None:
        self.items.insert(0, (kind, params))

    def has(self, kind: str) -> bool:
        return any(k == kind for k, _ in self.items)


class InsuranceAgent:
    def __init__(self, repo: FixtureRepo, settings: Settings, llm: LLMClient | None):
        self.repo = repo
        self.settings = settings
        self.llm = llm
        self.specs = PHASES
        self.gateway = ToolGateway(PHASES)
        self._facts_cache: dict[str, tuple[int, str, dict[str, Any]]] = {}
        prefixes = sorted({c.case_id.split("-")[0] for c in repo.claims if "-" in c.case_id}) or ["CL"]
        self.case_id_pattern = re.compile(r"\b(?:" + "|".join(map(re.escape, prefixes)) + r")-\d+\b", re.IGNORECASE)
        self.claim_date_phrases = self._date_phrases()
        self._register_tools()

    # ------------------------------------------------------------------ tools

    def _register_tools(self) -> None:
        repo = self.repo

        def require_verified(session: Session, **_: Any) -> str | None:
            return None if session.identity.verified_party_id else "caller is not verified"

        def owns_claim(session: Session, case_id: str) -> str | None:
            if not session.identity.verified_party_id:
                return "caller is not verified"
            claim = repo.claim(case_id)
            if claim is None or claim.party_id != session.identity.verified_party_id:
                return "claim is not on the verified caller's account"
            return None

        def has_consent(session: Session) -> str | None:
            if session.email.message is not None:
                return "summary already sent"
            if session.email.consent_turn != session.turn:
                return "no explicit consent in this turn"
            if not session.email.summary:
                return "no summary prepared"
            return None

        def account_located(session: Session, **_: Any) -> str | None:
            return None if session.consent.account_party_id else "policyholder account not located"

        def consent_requestable(session: Session, **_: Any) -> str | None:
            if not session.consent.representative_on_file:
                return "caller is not a listed representative"
            return "consent already requested" if session.consent.status else None

        def consent_pending(session: Session, **_: Any) -> str | None:
            return None if session.consent.status == "pending" else "no pending consent request"

        register = self.gateway.register
        register(Tool("verify_identity", lambda s, factors: verify(factors, repo.policyholders)))
        register(Tool("check_representative", self._check_representative, account_located))
        register(Tool("request_consent", self._request_consent, consent_requestable))
        register(Tool("check_consent", self._check_consent, consent_pending))
        register(Tool("list_claims", lambda s: repo.claims_for(s.identity.verified_party_id), require_verified))
        register(
            Tool(
                "get_claim_facts",
                lambda s, case_id: build_fact_sheet(repo.claim(case_id), repo, self.settings.today()),
                owns_claim,
            )
        )
        register(
            Tool(
                "build_summary",
                lambda s: build_summary(
                    party_id=s.identity.verified_party_id,
                    discussed=s.discussed,
                    handoff=s.handoff,
                    repo=repo,
                    today=self.settings.today(),
                    representative=s.representative_name if s.consent.status == "approved" else None,
                ),
                require_verified,
            )
        )
        register(Tool("send_email_summary", self._send_email, has_consent))
        register(Tool("create_handoff", self._create_handoff))

    def _send_email(self, session: Session) -> dict[str, Any]:
        holder = self.repo.policyholder(session.identity.verified_party_id)
        message = render_email(session.email.summary, to=holder.email)
        message.update(id="MSG-" + uuid.uuid4().hex[:8].upper(), sent_at=_now(), delivery="simulated outbox (demo)")
        session.email.message = message
        session.email.decision = "send"
        emit(session, "email_sent", f"Summary sent to {mask_email(holder.email)} (simulated outbox)")
        return message

    def _create_handoff(self, session: Session, reason: str) -> dict[str, Any]:
        ticket = {
            "ticket_id": "HR-" + uuid.uuid4().hex[:6].upper(),
            "created_at": _now(),
            "reason": reason,
            "phase_at_handoff": session.phase,
            "identity_verified": bool(session.identity.verified_party_id),
            "party_id": session.identity.verified_party_id,
            "caller_role": session.caller_role,
            "representative": session.representative_name,
            "consent_status": session.consent.status,
            "reason_for_call": session.reason_for_call,
            "active_case_id": session.active_case_id,
            "claims_discussed": list(session.discussed),
            "caller_notes": [m.value for m in session.memory],
            "delivery": "simulated queue (demo)",
        }
        session.handoff = ticket
        emit(session, "handoff_created", f"Handoff ticket {ticket['ticket_id']} created: {reason}")
        return ticket

    def _check_representative(self, session: Session) -> Representative | None:
        for rep in self.repo.representatives_for(session.consent.account_party_id):
            if names_match(session.representative_name or "", rep.rep_name):
                session.consent.representative_on_file = True
                return rep
        return None

    def _next_consent_status(self, session: Session) -> str:
        """Simulated policyholder response: one entry of the scenario's status_sequence per check."""
        sequence = self.repo.consent_scenarios.get(session.consent.scenario, {}).get("status_sequence", [])
        index = session.consent.checks
        session.consent.checks += 1
        if index < len(sequence):
            return sequence[index]
        return sequence[-1] if sequence else "pending"

    def _request_consent(self, session: Session) -> str:
        status = self._next_consent_status(session)
        session.consent.status = "approved" if status == "approved" else "pending"
        emit(
            session,
            "consent_requested",
            f"Authorization request sent to the policyholder (simulated '{session.consent.scenario}' scenario): {status}",
        )
        return status

    def _check_consent(self, session: Session) -> str:
        status = self._next_consent_status(session)
        emit(session, "consent_checked", f"Consent check {session.consent.checks}: {status}")
        return status

    # ------------------------------------------------------------------ sessions and turns

    def new_session(self, consent_scenario: str | None = None) -> Session:
        session = Session(id=uuid.uuid4().hex[:12])
        scenario = consent_scenario or self.settings.consent_scenario
        session.consent.scenario = scenario if scenario in self.repo.consent_scenarios else "default"
        session.transcript.append({"role": "assistant", "content": GREETING})
        emit(session, "session_started", "Conversation started")
        return session

    def handle(self, session: Session, text: str) -> str:
        text = text.strip()[:2000]
        if self.specs[session.phase].terminal:
            reply = prompts.closed_message(session.phase, session.handoff)
            session.transcript += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
            return reply
        session.turn += 1
        session.transcript.append({"role": "user", "content": text})
        nlu = self._understand(session, text)
        plan = Plan()
        self._capture(session, nlu)
        if not self._global_policies(session, nlu, plan):
            self._run_phases(session, nlu, plan)
        reply = self._reply(session, plan)
        session.transcript.append({"role": "assistant", "content": reply})
        return reply

    # ------------------------------------------------------------------ 1. understand

    def _understand(self, session: Session, text: str) -> NLUResult:
        if self.llm is None:
            emit(session, "nlu_failed", "No model configured; nothing changes this turn")
            return NLUResult.empty()
        spec = self.specs[session.phase]
        context = {key: self._context(key, session) for key in spec.nlu_context}
        user = prompts.nlu_user_prompt(
            today=self.settings.today().isoformat(),
            phase=session.phase,
            pending_question=session.pending_question,
            transcript=session.transcript,
            context=context,
            message=text,
        )
        error: Exception | None = None
        for _ in range(2):
            try:
                nlu = self.llm.structured(system=prompts.NLU_SYSTEM, user=user, schema=NLUResult, name="caller_message")
                emit(session, "understood", prompts.describe_nlu(nlu))
                return nlu
            except LLMError as exc:
                error = exc
        emit(session, "nlu_failed", f"Could not understand the message ({error}); nothing changes this turn")
        return NLUResult.empty()

    # ------------------------------------------------------------------ 2. record

    def _capture(self, session: Session, nlu: NLUResult) -> None:
        ident = session.identity
        # The caller's role is fixed once access is granted or the consent flow has started.
        role_open = not ident.verified_party_id and session.consent.status is None
        if role_open and nlu.caller_role != "unknown" and nlu.caller_role != session.caller_role:
            session.caller_role = nlu.caller_role
            emit(session, "caller_role", f"Caller role: {nlu.caller_role}")
        session.representative_name = nlu.representative_name or session.representative_name
        session.representative_relationship = nlu.representative_relationship or session.representative_relationship

        ident.unusable = []
        # For a representative these are the policyholder's details: they locate the account, but
        # never grant access by themselves (that also needs the representative record and consent).
        if not ident.verified_party_id and not ident.locked and not session.consent.account_party_id:
            for factor in FACTORS:
                raw = getattr(nlu.identity, factor)
                if not raw:
                    continue
                label = FACTOR_LABELS[factor]
                value = normalize(factor, raw)
                if value is None:
                    ident.unusable.append(factor)
                    emit(session, "factor_unusable", f"{label} was not usable as given")
                    continue
                previous = ident.provided.get(factor)
                ident.provided[factor] = SlotValue(value=value, turn=session.turn)
                if factor == "full_name" and session.caller_role != "representative":
                    session.caller_name = raw.strip()
                if factor in ident.refused:
                    ident.refused.remove(factor)
                if previous is None:
                    emit(session, "factor_captured", f"{label} captured (value withheld from log)")
                elif previous.value != value:
                    emit(session, "factor_corrected", f"{label} corrected by the caller")
            for factor in nlu.refused_factors:
                if factor not in ident.provided and factor not in ident.refused:
                    ident.refused.append(factor)
                    emit(session, "factor_declined", f"Caller declined to give {FACTOR_LABELS[factor]}")
            if nlu.identity.policy_number:
                ident.policy_number_hint = nlu.identity.policy_number.strip().upper()
                emit(session, "lookup_hint", "Policy number noted as a lookup hint; it does not count toward verification")

        # Which claim the caller means is remembered whenever they say it, including before
        # verification. Once a claim is being discussed, only an explicit switch changes it.
        choosing = session.phase in (VERIFY_ID, RESOLVE_INTENT) or nlu.wants_other_case
        hints = CaseHints(**nlu.case_hints.model_dump())
        if choosing:
            if nlu.intent:
                session.intent = nlu.intent
            if hints.any():
                session.case_hints = hints if nlu.wants_other_case else session.case_hints.merged(hints)
            if nlu.reason_for_call:
                session.reason_for_call = nlu.reason_for_call
            if hints.any() or nlu.reason_for_call:
                self._remember(session, "case_hint" if hints.any() else "intent", nlu.reason_for_call or hints.describe())
        for item in nlu.memory_items:
            self._remember(session, item.kind, item.value)
        session.sentiment = nlu.sentiment

    def _remember(self, session: Session, kind: str, value: str) -> None:
        value = value.strip()
        if not value or any(m.value.casefold() == value.casefold() for m in session.memory):
            return
        session.memory.append(MemoryItem(kind=kind, value=value, turn=session.turn, phase_said=session.phase))
        emit(session, "memory_saved", f"Remembered ({kind}): {value}")

    def _use_memory(self, session: Session, kinds: set[str]) -> None:
        for item in session.memory:
            if item.kind in kinds and item.phase_said != session.phase and session.phase not in item.used_in:
                item.used_in.append(session.phase)
                emit(session, "memory_used", f"Using '{item.value}' (said during {item.phase_said})")

    # ------------------------------------------------------------------ 3. global policies

    def _global_policies(self, session: Session, nlu: NLUResult, plan: Plan) -> bool:
        """Policies that apply in every phase. Returns True when the turn is fully handled."""
        counters = session.counters
        if nlu.injection_attempt:
            emit(session, "injection_ignored", "Attempt to change the rules or claim prior verification; no state changed")
            plan.add("injection_notice")
        if nlu.wants_human:
            self._handoff(session, plan, "caller asked for a human representative")
            return True
        if nlu.scope == "out_of_scope":
            counters.oos_consecutive += 1
            counters.oos_total += 1
            emit(
                session,
                "out_of_scope",
                f"Out-of-scope question declined ({counters.oos_consecutive} in a row; threshold {self.settings.oos_threshold})",
            )
            plan.add("decline_out_of_scope")
            if counters.oos_consecutive >= self.settings.oos_threshold:
                self._offer_human(session, plan, "they keep asking about topics outside insurance claims")
        else:
            if counters.oos_consecutive:
                emit(session, "out_of_scope_reset", "Back on topic; out-of-scope streak reset")
            counters.oos_consecutive = 0
            if nlu.has_oos_part:
                plan.add("decline_out_of_scope_part")
            if nlu.scope == "in_scope_unanswerable":
                emit(session, "unanswerable", "Insurance question outside what this service holds")
                plan.add("unanswerable")
                self._offer_human(session, plan, "a representative can help with that question")
        if nlu.sentiment != "neutral" and nlu.intensity >= 2:
            emit(session, "emotion", f"Caller seems {nlu.sentiment} ({nlu.intensity}/3); acknowledging before continuing")
            plan.add_front("acknowledge_emotion", sentiment=nlu.sentiment, intensity=nlu.intensity)
            if nlu.intensity >= 3:
                self._offer_human(session, plan, "they are very upset")
        return False

    def _offer_human(self, session: Session, plan: Plan, reason: str) -> None:
        if plan.has("offer_human"):
            return
        plan.add("offer_human", reason=reason)
        emit(session, "human_offered", f"Offered a human representative: {reason}")

    def _handoff(self, session: Session, plan: Plan, reason: str) -> None:
        ticket = self.gateway.call("create_handoff", session, reason=reason)
        plan.add("handoff_created", ticket_id=ticket["ticket_id"], verified=bool(session.identity.verified_party_id))
        self._transition(session, ESCALATED, reason)

    # ------------------------------------------------------------------ 4. phase handlers

    def _run_phases(self, session: Session, nlu: NLUResult, plan: Plan) -> None:
        handlers = {
            VERIFY_ID: self._verify_id,
            RESOLVE_INTENT: self._resolve_intent,
            PROCESS_CASE: self._process_case,
            POST_PROCESS: self._post_process,
        }
        entered = False  # True once a phase was entered during this turn
        for _ in range(6):
            phase = session.phase
            handler = handlers.get(phase)
            if handler is None:
                return
            handler(session, nlu, plan, entered)
            if session.phase == phase:
                return
            entered = True

    def _transition(self, session: Session, to: str, reason: str) -> None:
        emit(session, "phase_transition", f"{session.phase} → {to}: {reason}", from_phase=session.phase, to_phase=to)
        session.phase_history.append({"from": session.phase, "to": to, "turn": session.turn, "reason": reason})
        session.phase = to
        session.pending_question = None

    def _verify_id(self, session: Session, nlu: NLUResult, plan: Plan, entered: bool) -> None:
        if session.identity.locked:
            plan.add("verification_locked")
            self._offer_human(session, plan, "verification is locked")
            return
        if session.caller_role == "representative":
            return self._verify_representative(session, nlu, plan)
        if nlu.asks_why or nlu.pushback_on_gate:
            plan.add("explain_verification")
        self._count_pushback(session, nlu, plan)
        result = self._match_factors(session, nlu, plan, whose="theirs")
        if result is None:
            return
        ident = session.identity
        holder = self.repo.policyholder(result.party_id)
        ident.verified_party_id = result.party_id
        ident.verified_via = result.factors
        emit(
            session,
            "verified",
            "Identity verified via " + ", ".join(FACTOR_LABELS[f] for f in result.factors),
            party_id=result.party_id,
        )
        plan.add("verification_success", name=session.caller_name or holder.name)
        self._transition(session, RESOLVE_INTENT, "identity verified")

    def _count_pushback(self, session: Session, nlu: NLUResult, plan: Plan) -> None:
        if not nlu.pushback_on_gate:
            return
        session.counters.gate_pushbacks += 1
        limit = self.settings.max_gate_pushbacks
        emit(session, "gate_pushback", f"Caller pushed back on a required step ({session.counters.gate_pushbacks}/{limit})")
        if session.counters.gate_pushbacks >= limit:
            self._offer_human(session, plan, "they may prefer to continue with a person")

    def _holder_label(self, session: Session) -> str:
        """How to refer to the policyholder in front of a representative: by name only if the caller gave it."""
        holder = self.repo.policyholder(session.consent.account_party_id or "")
        return holder.name if holder and "full_name" in session.identity.verified_via else "the policyholder"

    def _verify_representative(self, session: Session, nlu: NLUResult, plan: Plan) -> None:
        """Three gates for someone calling on a policyholder's behalf: locate the account with the
        policyholder's details, find the caller in the representative record, get the policyholder's approval."""
        consent = session.consent
        if not consent.explained:
            consent.explained = True
            emit(session, "third_party_caller", "Caller is acting for someone else: locate account → check record → get consent")
            plan.add("representative_process", relationship=session.representative_relationship)
        if nlu.pushback_on_gate or nlu.asks_why:
            plan.add("explain_consent")
        self._count_pushback(session, nlu, plan)

        if consent.status in ("timeout", "not_on_file"):
            kind = "consent_timeout" if consent.status == "timeout" else "representative_not_on_file"
            plan.add(kind, holder=self._holder_label(session))
            self._offer_human(session, plan, "a representative can help with authorized access")
            return
        if consent.status == "pending":
            if self.gateway.call("check_consent", session) == "approved":
                return self._grant_representative(session, plan)
            if consent.checks >= self.settings.max_consent_checks:
                consent.status = "timeout"
                emit(session, "consent_timeout", f"No approval after {consent.checks} checks; nothing disclosed")
                plan.add("consent_timeout", holder=self._holder_label(session))
                self._offer_human(session, plan, "the policyholder hasn't approved access")
                return
            plan.add("consent_pending", holder=self._holder_label(session))
            session.pending_question = "consent_pending"
            return

        # Gate 1: locate the account. This alone gives no access.
        if not consent.account_party_id:
            result = self._match_factors(session, nlu, plan, whose="the policyholder's")
            if result is None:
                if not session.representative_name:
                    plan.add("ask_representative_name")
                return
            consent.account_party_id = result.party_id
            session.identity.verified_via = result.factors
            emit(
                session,
                "account_located",
                "Policyholder account located via "
                + ", ".join(FACTOR_LABELS[f] for f in result.factors)
                + "; no access until the representative is confirmed and the policyholder approves",
                party_id=result.party_id,
            )
        if not session.representative_name:
            plan.add("ask_representative_name")
            session.pending_question = "representative_name"
            return

        # Gate 2: the caller must be on file as this policyholder's representative.
        rep = self.gateway.call("check_representative", session)
        if rep is None:
            consent.status = "not_on_file"
            emit(session, "representative_not_on_file", "Caller is not a listed representative on this account; nothing disclosed")
            plan.add("representative_not_on_file", holder=self._holder_label(session))
            self._offer_human(session, plan, "the policyholder can add an authorized contact")
            return
        emit(session, "representative_on_file", f"{rep.rep_name} is on file as the policyholder's {rep.relationship}")

        # Gate 3: the policyholder approves (simulated).
        if self.gateway.call("request_consent", session) == "approved":
            return self._grant_representative(session, plan)
        plan.add("consent_requested", holder=self._holder_label(session))
        session.pending_question = "consent_pending"

    def _grant_representative(self, session: Session, plan: Plan) -> None:
        consent = session.consent
        consent.status = "approved"
        session.identity.verified_party_id = consent.account_party_id
        emit(
            session,
            "consent_approved",
            f"Policyholder approved access for {session.representative_name}; continuing as an authorized representative",
            party_id=consent.account_party_id,
        )
        plan.add("consent_approved", holder=self._holder_label(session), representative=session.representative_name)
        self._transition(session, RESOLVE_INTENT, "policyholder approved the representative")

    def _match_factors(self, session: Session, nlu: NLUResult, plan: Plan, whose: str) -> VerificationResult | None:
        """Check the identity details once at least 3 are in; otherwise plan to ask for more.

        Returns the verified match, or None after adding what the caller should be told.
        """
        ident = session.identity
        max_attempts = self.settings.max_verification_attempts
        if len(ident.provided) >= REQUIRED_FACTORS:
            result = self.gateway.call(
                "verify_identity", session, factors={f: s.value for f, s in ident.provided.items()}
            )
            if result.status == "verified":
                return result
            ident.failed_attempts += 1
            ident.provided = {}
            emit(
                session,
                "verification_failed",
                f"Attempt {ident.failed_attempts} of {max_attempts} failed; details cleared, nothing disclosed about which one",
            )
            if ident.failed_attempts >= max_attempts:
                ident.locked = True
                emit(session, "verification_locked", "Too many failed attempts; verification locked for this chat")
                plan.add("verification_locked")
                self._offer_human(session, plan, "verification is locked")
                return
            plan.add("verification_failed", attempts_left=max_attempts - ident.failed_attempts)

        if nlu.reason_for_call or CaseHints(**nlu.case_hints.model_dump()).any():
            plan.add("note_reason_for_call", reason=session.reason_for_call or session.case_hints.describe())

        available = [f for f in FACTORS if f not in ident.provided and f not in ident.refused]
        need = REQUIRED_FACTORS - len(ident.provided)
        if len(available) < need:
            emit(session, "verification_impossible", "Too few identity details left after the caller's refusals")
            plan.add("cannot_verify_with_remaining", declined=[FACTOR_LABELS[f] for f in ident.refused])
            self._offer_human(session, plan, "they can verify with a representative instead")
            return
        plan.add(
            "ask_identity",
            have=[FACTOR_LABELS[f] for f in ident.provided],
            need=need,
            options=[FACTOR_LABELS[f] for f in available],
            unusable=[FACTOR_LABELS[f] for f in ident.unusable],
            declined=[FACTOR_LABELS[f] for f in ident.refused],
            whose=whose,
        )
        session.pending_question = "need_identity"
        return None

    def _resolve_intent(self, session: Session, nlu: NLUResult, plan: Plan, entered: bool) -> None:
        claims = self.gateway.call("list_claims", session)
        if not claims:
            emit(session, "no_claims", "No claims on file for the verified caller")
            plan.add("no_claims_on_file")
            self._transition(session, POST_PROCESS, "no claims on file")
            return
        by_id = {c.case_id.upper(): c for c in claims}

        # The model may pick a claim, but only from the caller's own claims (a closed set).
        pick = (nlu.selected_case_id or "").strip().upper()
        if pick in by_id:
            return self._select_case(session, plan, by_id[pick].case_id, "the caller's choice")

        # The caller answered our "which one?" question with a description.
        if not entered and session.candidate_case_ids:
            new_hints = CaseHints(**nlu.case_hints.model_dump())
            if new_hints.any():
                candidates = [by_id[i] for i in session.candidate_case_ids if i in by_id]
                narrowed = match_claims(candidates, new_hints)
                if len(narrowed) == 1:
                    return self._select_case(session, plan, narrowed[0].case_id, "the caller's clarification")

        hints = session.case_hints
        if hints.case_id and hints.case_id.strip().upper() not in by_id:
            # A lookup by ID goes through the gateway, which enforces ownership.
            try:
                self.gateway.call("get_claim_facts", session, case_id=hints.case_id.strip().upper())
            except ToolDenied:
                pass
        if hints.any():
            self._use_memory(session, {"case_hint", "intent"})
            matches = match_claims(claims, hints)
            if len(matches) == 1:
                return self._select_case(
                    session, plan, matches[0].case_id, f"matches '{hints.describe()}'", from_memory=entered
                )
            if matches:
                session.candidate_case_ids = [c.case_id.upper() for c in matches]
                emit(session, "case_ambiguous", f"{len(matches)} claims match '{hints.describe()}'; asking the caller")
                plan.add("ask_choose_case", described=hints.describe(), candidates=[c.brief() for c in matches])
                session.pending_question = "choose_case"
                return
            emit(session, "case_no_match", f"No claim on this account matches '{hints.describe()}'")
            plan.add("no_matching_case", described=hints.describe(), claims=[c.brief() for c in claims])
            session.case_hints = CaseHints()
            session.candidate_case_ids = list(by_id)
            session.pending_question = "choose_case"
            return
        if len(claims) == 1:
            return self._select_case(session, plan, claims[0].case_id, "the only claim on file")
        session.candidate_case_ids = list(by_id)
        plan.add("ask_which_claim", claims=[c.brief() for c in claims])
        session.pending_question = "choose_case"

    def _select_case(self, session: Session, plan: Plan, case_id: str, how: str, from_memory: bool = False) -> None:
        claim = self.repo.claim(case_id)
        session.active_case_id = claim.case_id
        session.candidate_case_ids = []
        session.discussed.setdefault(claim.case_id, [])
        emit(session, "case_selected", f"{claim.case_id} selected ({how})")
        plan.add("case_selected", case=claim.brief(), from_memory=from_memory)
        self._transition(session, PROCESS_CASE, f"{claim.case_id} selected")

    def _process_case(self, session: Session, nlu: NLUResult, plan: Plan, entered: bool) -> None:
        if not entered:
            if nlu.wants_other_case:
                session.active_case_id = None
                self._transition(session, RESOLVE_INTENT, "caller asked about a different claim")
                return
            if nlu.wants_end and not nlu.followup_topic:
                self._transition(session, POST_PROCESS, "caller has no more questions")
                return
            if nlu.scope in ("out_of_scope", "in_scope_unanswerable") or (nlu.scope == "smalltalk" and not nlu.followup_topic):
                plan.add("invite_followup")
                session.pending_question = "anything_else"
                return
        case_id = session.active_case_id
        claim = self.repo.claim(case_id)
        facts = self._facts(session)
        topics = session.discussed.setdefault(case_id, [])
        first = not topics
        topic = nlu.followup_topic or (DEFAULT_TOPIC.get(session.intent or "", "overview") if first else "other")
        if topic == "denial_reason" and claim.status.lower() != "denied":
            topic = "overview"
        topics.append(topic)
        emit(session, "grounded_answer", f"Answering '{topic}' from the {case_id} fact sheet")
        plan.add("answer", topic=topic, first_answer=first)
        self._use_memory(session, {"doc_status", "context", "contact_pref"})

        derived = facts.get("derived", {})
        if derived.get("appeal_deadline_passed") and (topic in TIMING_TOPICS or case_id not in session.deadline_caveat_given):
            if case_id not in session.deadline_caveat_given:
                session.deadline_caveat_given.append(case_id)
            emit(
                session,
                "deadline_caveat",
                f"Appeal deadline {derived['appeal_deadline']} passed {derived['days_since_deadline']} days before "
                f"{facts['as_of_date']}; caveat added",
            )
            plan.add("deadline_caveat", deadline=derived["appeal_deadline"])
            if topic in TIMING_TOPICS:
                self._offer_human(session, plan, "a representative can confirm whether a late submission is possible")
        plan.add("invite_followup")
        session.pending_question = "anything_else"

    def _post_process(self, session: Session, nlu: NLUResult, plan: Plan, entered: bool) -> None:
        email = session.email
        holder = self.repo.policyholder(session.identity.verified_party_id)
        masked = mask_email(holder.email if holder else None)
        if entered or not email.offered:
            email.summary = self.gateway.call("build_summary", session)
            email.offered = True
            email.decision = None
            session.counters.email_unclear = 0
            prefs = [m for m in session.memory if m.kind == "email_pref"]
            if prefs:
                self._use_memory(session, {"email_pref"})
            plan.add(
                "offer_email",
                masked_email=masked,
                preference=prefs[-1].value if prefs else None,
                claims_discussed=bool(session.discussed),
                owner=self._holder_label(session) if session.caller_role == "representative" else None,
            )
            session.pending_question = "email_consent"
            emit(session, "email_offered", f"Email summary offered to {masked}; waiting for send or skip")
            return
        if nlu.wants_other_email_address:
            emit(session, "email_other_address_declined", "Caller asked for another address; only the email on file is allowed")
            plan.add("email_other_address_declined", masked_email=masked)
            return
        if nlu.email_decision == "send":
            email.consent_turn = session.turn
            try:
                self.gateway.call("send_email_summary", session)
                plan.add("email_sent", masked_email=masked)
            except ToolDenied:
                pass
            plan.add("goodbye")
            self._transition(session, ENDED, "summary sent with the caller's consent")
            return
        if nlu.email_decision == "skip":
            email.decision = "skip"
            emit(session, "email_skipped", "Caller chose to skip the email summary")
            plan.add("email_skipped")
            plan.add("goodbye")
            self._transition(session, ENDED, "caller skipped the summary")
            return
        if nlu.followup_topic or nlu.wants_other_case:
            email.offered = False
            target = RESOLVE_INTENT if (nlu.wants_other_case or not session.active_case_id) else PROCESS_CASE
            self._transition(session, target, "caller has another question")
            return
        session.counters.email_unclear += 1
        if session.counters.email_unclear >= 2:
            email.decision = "not_sent"
            emit(session, "email_not_sent", "No clear answer after asking twice; email not sent")
            plan.add("email_not_sent")
            plan.add("goodbye")
            self._transition(session, ENDED, "no clear consent for the email")
            return
        emit(session, "email_unclear", "Unclear answer to the email offer; asking again")
        plan.add("email_reask", masked_email=masked)

    # ------------------------------------------------------------------ 5-6. reply and guard

    def _facts(self, session: Session) -> dict[str, Any]:
        case_id = session.active_case_id
        cached = self._facts_cache.get(session.id)
        if cached and cached[0] == session.turn and cached[1] == case_id:
            return cached[2]
        facts = self.gateway.call("get_claim_facts", session, case_id=case_id)
        self._facts_cache[session.id] = (session.turn, case_id, facts)
        return facts

    def _context(self, key: str, session: Session) -> Any:
        """Context providers. A phase spec lists which of these the model may see."""
        ident = session.identity
        party = ident.verified_party_id
        holder = self.repo.policyholder(party) if party else None
        if key == "identity_progress":
            available = [f for f in FACTORS if f not in ident.provided and f not in ident.refused]
            if session.caller_role == "representative":
                return {
                    "caller": "acting on behalf of the policyholder",
                    "representative_name": session.representative_name,
                    "policyholder_details_provided": [FACTOR_LABELS[f] for f in ident.provided],
                    "policyholder_details_still_needed": 0
                    if session.consent.account_party_id
                    else max(0, REQUIRED_FACTORS - len(ident.provided)),
                    "account_located": bool(session.consent.account_party_id),
                    "policyholder_authorization": session.consent.status or "not requested yet",
                    "note": "Nothing about the account or its claims may be shared until the policyholder approves.",
                }
            return {
                "details_provided": [FACTOR_LABELS[f] for f in ident.provided],
                "details_still_needed": max(0, REQUIRED_FACTORS - len(ident.provided)),
                "details_they_can_choose_from": [FACTOR_LABELS[f] for f in available],
                "details_declined": [FACTOR_LABELS[f] for f in ident.refused],
                "failed_attempts": ident.failed_attempts,
            }
        if key == "caller_statements":
            return {
                "what_they_said_they_need": session.reason_for_call,
                "note": "These are the customer's own words, not records. You have no access to their account or claims.",
            }
        if key == "verified_caller":
            if not holder:
                return None
            if session.caller_role == "representative":
                relationship = session.representative_relationship or "representative"
                return {
                    "speaking_with": session.representative_name,
                    "role": f"authorized representative ({relationship}) of the policyholder, approved by the policyholder",
                    "policyholder": self._holder_label(session),
                    "policy_number": holder.policy_number,
                }
            return {"name_as_they_gave_it": session.caller_name or holder.name, "policy_number": holder.policy_number}
        if key == "claim_index":
            return [c.brief() for c in self.repo.claims_for(party)] if party else []
        if key == "active_case":
            return session.active_case_id
        if key == "active_case_facts":
            return self._facts(session) if (party and session.active_case_id) else None
        if key == "memory":
            return [{"kind": m.kind, "value": m.value, "said_during": m.phase_said} for m in session.memory]
        if key == "email_offer":
            return {"to": mask_email(holder.email if holder else None), "summary": session.email.summary}
        if key == "handoff":
            return session.handoff
        return None

    def _date_phrases(self) -> set[str]:
        phrases = set()
        for claim in self.repo.claims:
            for value in (claim.created_at, claim.appeal_deadline):
                if not value:
                    continue
                d = date.fromisoformat(value)
                phrases |= {value, f"{d:%B} {d.day}", f"{d:%b} {d.day},", f"{d:%b} {d.day} "}
        return phrases

    def _guard_policy(self, session: Session, context: dict[str, Any]) -> GuardPolicy:
        said = " ".join(m["content"] for m in session.transcript if m["role"] == "user")
        caller_ids = {m.upper() for m in self.case_id_pattern.findall(said)}
        caller_money = money_values(said)
        party = session.identity.verified_party_id
        if not party:
            # Before verification: no claim IDs, amounts or claim dates, except what the caller said themselves.
            return GuardPolicy(
                id_pattern=self.case_id_pattern,
                allowed_ids=caller_ids,
                allowed_money=caller_money,
                forbidden_phrases=self.claim_date_phrases,
            )
        own = {c.case_id.upper() for c in self.repo.claims_for(party)}
        in_context = money_values(json.dumps(context, default=str))
        return GuardPolicy(id_pattern=self.case_id_pattern, allowed_ids=own | caller_ids, allowed_money=caller_money | in_context)

    def _reply(self, session: Session, plan: Plan) -> str:
        spec = self.specs[session.phase]
        context = {key: self._context(key, session) for key in spec.reply_context}
        policy = self._guard_policy(session, context)
        system = prompts.nlg_system(spec, context, plan.items)
        messages = session.transcript[-12:]
        if self.llm is not None:
            feedback = ""
            for _ in range(2):
                try:
                    reply = self.llm.text(system=system + feedback, messages=messages)
                except LLMError as exc:
                    emit(session, "reply_failed", f"Reply model failed ({exc})")
                    break
                violations = check_reply(reply, policy)
                if not violations:
                    return reply
                emit(session, "guard_blocked", "Reply blocked by the output guard: " + "; ".join(violations))
                feedback = prompts.guard_feedback(violations)
        emit(session, "fallback_reply", "Used the deterministic fallback reply")
        return prompts.fallback_reply(session.phase, plan.items, context)

    # ------------------------------------------------------------------ operator view

    def snapshot(self, session: Session) -> dict[str, Any]:
        ident = session.identity
        party = ident.verified_party_id
        holder = self.repo.policyholder(party) if party else None
        active = self.repo.claim(session.active_case_id) if session.active_case_id else None
        status = "verified" if party else ("locked" if ident.locked else "in progress")
        message = dict(session.email.message) if session.email.message else None
        if message:
            message["to"] = mask_email(message["to"])
        return {
            "session_id": session.id,
            "turn": session.turn,
            "phase": session.phase,
            "workflow": list(WORKFLOW),
            "phases": {name: spec.describe() for name, spec in self.specs.items()},
            "phase_history": session.phase_history,
            "as_of_date": self.settings.today().isoformat(),
            "identity": {
                "status": status,
                "provided": [FACTOR_LABELS[f] for f in ident.provided],
                "required": REQUIRED_FACTORS,
                "declined": [FACTOR_LABELS[f] for f in ident.refused],
                "failed_attempts": ident.failed_attempts,
                "max_attempts": self.settings.max_verification_attempts,
                "verified_as": f"{holder.name} ({holder.party_id})" if holder else None,
                "verified_via": [FACTOR_LABELS[f] for f in ident.verified_via]
                + (["policyholder consent"] if session.consent.status == "approved" else []),
                "policy_number_hint": ident.policy_number_hint,
            },
            "caller_role": session.caller_role,
            "consent": {
                "scenario": session.consent.scenario,
                "status": session.consent.status,
                "checks": session.consent.checks,
                "max_checks": self.settings.max_consent_checks,
                "account_located": bool(session.consent.account_party_id),
                "representative": session.representative_name,
                "relationship": session.representative_relationship,
            },
            "intent": session.intent,
            "case_hints": session.case_hints.describe() if session.case_hints.any() else None,
            "memory": [m.model_dump() for m in session.memory],
            "active_case": active.brief() if active else None,
            "candidates": session.candidate_case_ids,
            "discussed": session.discussed,
            "counters": session.counters.model_dump(),
            "limits": {
                "oos_threshold": self.settings.oos_threshold,
                "max_gate_pushbacks": self.settings.max_gate_pushbacks,
            },
            "sentiment": session.sentiment,
            "pending_question": session.pending_question,
            "email": {
                "offered": session.email.offered,
                "decision": session.email.decision,
                "to": mask_email(holder.email) if holder else None,
                "summary": session.email.summary,
                "message": message,
            },
            "handoff": session.handoff,
            "events": [e.model_dump() for e in session.events[-150:]],
        }
