"""The insurance claims SOP, declared as phase specs. The engine (agent.py) enforces them."""

from __future__ import annotations

from ..harness.spec import Autonomy, PhaseSpec

VERIFY_ID = "VERIFY_ID"
RESOLVE_INTENT = "RESOLVE_INTENT"
PROCESS_CASE = "PROCESS_CASE"
POST_PROCESS = "POST_PROCESS"
ESCALATED = "ESCALATED"
ENDED = "ENDED"

WORKFLOW = (VERIFY_ID, RESOLVE_INTENT, PROCESS_CASE, POST_PROCESS)

PHASES: dict[str, PhaseSpec] = {
    VERIFY_ID: PhaseSpec(
        name=VERIFY_ID,
        goal=(
            "Verify the caller with at least 3 identity factors before any account access. A representative must "
            "also be on file for the policyholder and get the policyholder's approval."
        ),
        autonomy=Autonomy.SCRIPTED,
        tools=frozenset(
            {"verify_identity", "check_representative", "request_consent", "check_consent", "create_handoff"}
        ),
        reply_context=("identity_progress", "caller_statements"),
        nlu_context=(),
        exit_guard=(
            "Exactly one policyholder matches every provided factor (at least 3 distinct). For a representative: "
            "also listed for that policyholder, and the policyholder approved."
        ),
    ),
    RESOLVE_INTENT: PhaseSpec(
        name=RESOLVE_INTENT,
        goal="Work out which of the verified caller's claims they are asking about.",
        autonomy=Autonomy.GUIDED,
        tools=frozenset({"list_claims", "get_claim_facts", "create_handoff"}),
        reply_context=("verified_caller", "claim_index", "memory"),
        nlu_context=("claim_index",),
        exit_guard="One claim selected from the caller's own claims, or no claims on file.",
    ),
    PROCESS_CASE: PhaseSpec(
        name=PROCESS_CASE,
        goal="Answer questions about the active claim from grounded facts only.",
        autonomy=Autonomy.OPEN,
        tools=frozenset({"list_claims", "get_claim_facts", "create_handoff"}),
        reply_context=("verified_caller", "claim_index", "active_case_facts", "memory"),
        nlu_context=("claim_index", "active_case"),
        exit_guard="Caller is done, asks about another claim, or asks for a human.",
    ),
    POST_PROCESS: PhaseSpec(
        name=POST_PROCESS,
        goal="Offer an email summary; send only with explicit consent, or skip.",
        autonomy=Autonomy.CONSTRAINED,
        tools=frozenset({"build_summary", "send_email_summary", "create_handoff"}),
        reply_context=("verified_caller", "email_offer", "memory"),
        nlu_context=("claim_index", "active_case"),
        exit_guard="Summary sent with explicit consent, or skipped.",
    ),
    ESCALATED: PhaseSpec(
        name=ESCALATED,
        goal="Handed off to a human representative.",
        autonomy=Autonomy.SCRIPTED,
        tools=frozenset(),
        reply_context=("handoff",),
        nlu_context=(),
        exit_guard="Terminal.",
        terminal=True,
    ),
    ENDED: PhaseSpec(
        name=ENDED,
        goal="Conversation closed.",
        autonomy=Autonomy.SCRIPTED,
        tools=frozenset(),
        reply_context=(),
        nlu_context=(),
        exit_guard="Terminal.",
        terminal=True,
    ),
}
