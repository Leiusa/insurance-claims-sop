"""Engine tests with a scripted model: they exercise the SOP logic, not the model."""

from datetime import date

import pytest

from sop.config import Settings
from sop.harness.gateway import ToolDenied
from sop.harness.guard import check_reply
from sop.insurance.agent import InsuranceAgent
from sop.insurance.nlu_schema import NLUResult
from sop.llm.client import LLMError
from tests.conftest import FIXTURES

MARGARET = {"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"}


def nlu(identity=None, case_hints=None, **fields):
    data = NLUResult.empty().model_dump()
    data["identity"].update(identity or {})
    data["case_hints"].update(case_hints or {})
    data.update(fields)
    return NLUResult.model_validate(data)


class FakeLLM:
    """Returns scripted understanding results and records every reply prompt."""

    provider = model = nlu_model = "fake"

    def __init__(self):
        self.queue: list[NLUResult] = []
        self.reply_prompts: list[str] = []

    def structured(self, *, system, user, schema, name):
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def text(self, *, system, messages):
        self.reply_prompts.append(system)
        return "ok"


@pytest.fixture
def agent(repo):
    settings = Settings(
        provider="fake",
        api_key="x",
        model="fake",
        nlu_model="fake",
        base_url=None,
        reasoning_effort=None,
        fixtures_dir=FIXTURES,
        as_of_date=date(2026, 9, 28),
        demo_passcode=None,
        oos_threshold=3,
        max_verification_attempts=3,
        max_gate_pushbacks=3,
    )
    return InsuranceAgent(repo, settings, FakeLLM())


def say(agent, session, result):
    agent.llm.queue.append(result)
    agent.handle(session, "(scripted)")
    return agent.llm.reply_prompts[-1]


def kinds(session):
    return [e.kind for e in session.events]


def fail_turn(agent, session):
    """A turn where the understanding model fails twice (the engine retries once)."""
    agent.llm.queue += [LLMError("simulated outage"), LLMError("simulated outage")]
    return agent.handle(session, "(unreadable)")


def test_canonical_turn_verifies_and_uses_remembered_hint(agent):
    s = agent.new_session()
    prompt = say(
        agent,
        s,
        nlu(
            identity={**MARGARET, "policy_number": "POL-9921"},
            case_hints={"case_type": "healthcare", "status": "denied", "month": 1},
            intent="denial_question",
            reason_for_call="denied healthcare claim from January",
            caller_role="self",
        ),
    )
    assert s.phase == "PROCESS_CASE" and s.active_case_id == "CL-2048"
    hint = next(m for m in s.memory if m.kind == "case_hint")
    assert hint.phase_said == "VERIFY_ID" and "RESOLVE_INTENT" in hint.used_in
    assert "topic: denial_reason" in prompt and "pathology report" in prompt
    assert "deadline" in prompt  # the appeal deadline has passed as of 2026-09-28


def test_no_claim_data_reaches_the_model_before_verification(agent):
    s = agent.new_session()
    prompt = say(
        agent,
        s,
        nlu(identity={"full_name": "Margaret Chen"}, case_hints={"status": "denied"}, reason_for_call="my denied claim"),
    )
    assert s.phase == "VERIFY_ID"
    for secret in ("CL-2048", "pathology", "1450", "2026-01-12", "margaret@email.com", "4472"):
        assert secret not in prompt


def test_gateway_denies_claim_access_before_verification(agent):
    s = agent.new_session()
    with pytest.raises(ToolDenied):
        agent.gateway.call("list_claims", s)
    assert "tool_denied" in kinds(s)


def test_failed_attempts_are_generic_then_lock(agent):
    s = agent.new_session()
    wrong = {"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "0000"}
    for _ in range(3):
        say(agent, s, nlu(identity=wrong))
    assert s.identity.locked and s.identity.provided == {}
    assert s.phase == "VERIFY_ID"
    assert "verification_locked" in kinds(s) and "human_offered" in kinds(s)


def test_refusing_ssn_still_allows_other_factors(agent):
    s = agent.new_session()
    prompt = say(agent, s, nlu(identity={"full_name": "Margaret Chen"}, refused_factors=["id_last4"]))
    offered = prompt.split("they can choose any of:")[1].split("\n")[0]
    assert "last 4 digits" not in offered
    say(agent, s, nlu(identity={"dob": "1985-03-15", "phone": "650-521-2836"}))
    assert s.identity.verified_party_id == "P9"
    assert s.identity.verified_via == ["dob", "full_name", "phone"]


def test_verification_is_explained_only_while_still_needed(agent):
    explanation = "why identity verification comes first"
    # Verified in the same turn: explaining why verification is needed would be noise.
    s = agent.new_session()
    prompt = say(agent, s, nlu(identity=MARGARET, asks_why=True, case_hints={"status": "denied"}))
    assert s.identity.verified_party_id == "P9" and explanation not in prompt
    # Still verifying: the explanation comes before the request for details.
    s = agent.new_session()
    prompt = say(agent, s, nlu(identity={"full_name": "Margaret Chen"}, asks_why=True))
    assert explanation in prompt and prompt.index(explanation) < prompt.index("more detail(s) to verify")


def test_repeated_out_of_scope_offers_human(agent):
    s = agent.new_session()
    for _ in range(3):
        say(agent, s, nlu(scope="out_of_scope"))
    assert s.counters.oos_consecutive == 3
    assert "human_offered" in kinds(s)


def test_injection_changes_nothing(agent):
    s = agent.new_session()
    say(agent, s, nlu(injection_attempt=True, case_hints={"case_id": "CL-2048"}))
    assert s.phase == "VERIFY_ID" and s.identity.verified_party_id is None
    assert "injection_ignored" in kinds(s)


def test_january_ambiguity_then_caller_picks(agent):
    s = agent.new_session()
    say(agent, s, nlu(identity=MARGARET, case_hints={"case_type": "healthcare", "month": 1}))
    assert s.phase == "RESOLVE_INTENT" and sorted(s.candidate_case_ids) == ["CL-2011", "CL-2048"]
    say(agent, s, nlu(selected_case_id="CL-2011"))
    assert s.phase == "PROCESS_CASE" and s.active_case_id == "CL-2011"


def test_other_customers_claim_is_denied_by_gateway(agent):
    s = agent.new_session()
    say(
        agent,
        s,
        nlu(identity={"full_name": "Tian Ma", "dob": "1964-09-10", "id_last4": "6688"}, case_hints={"case_id": "CL-2048"}),
    )
    assert s.identity.verified_party_id == "P12"
    assert s.active_case_id is None and "tool_denied" in kinds(s) and "case_no_match" in kinds(s)


def test_email_needs_explicit_consent(agent):
    s = agent.new_session()
    say(agent, s, nlu(identity=MARGARET, case_hints={"status": "denied"}))
    say(agent, s, nlu(wants_end=True, scope="smalltalk"))
    assert s.phase == "POST_PROCESS" and s.email.offered
    say(agent, s, nlu(email_decision="unclear", scope="smalltalk"))
    assert s.email.message is None and s.phase == "POST_PROCESS"
    say(agent, s, nlu(email_decision="unclear", scope="smalltalk"))
    assert s.email.message is None and s.email.decision == "not_sent" and s.phase == "ENDED"


def test_email_sent_once_with_consent(agent):
    s = agent.new_session()
    say(agent, s, nlu(identity=MARGARET, case_hints={"status": "denied"}))
    say(agent, s, nlu(wants_end=True))
    say(agent, s, nlu(email_decision="send"))
    assert s.email.message and s.email.message["to"] == "margaret@email.com"
    assert "CL-2048" in s.email.message["body"] and s.phase == "ENDED"
    with pytest.raises(ToolDenied):
        agent.gateway.call("send_email_summary", s)


def test_skip_email_ends_without_sending(agent):
    s = agent.new_session()
    say(agent, s, nlu(identity=MARGARET, case_hints={"status": "denied"}))
    say(agent, s, nlu(wants_end=True))
    say(agent, s, nlu(email_decision="skip"))
    assert s.email.message is None and s.email.decision == "skip" and s.phase == "ENDED"


def test_switching_to_another_claim(agent):
    s = agent.new_session()
    say(agent, s, nlu(identity=MARGARET, case_hints={"status": "denied"}))
    say(agent, s, nlu(wants_other_case=True, case_hints={"case_type": "auto"}, intent="status_inquiry"))
    assert s.active_case_id == "CL-2102" and set(s.discussed) == {"CL-2048", "CL-2102"}


def test_verified_caller_without_claims_goes_to_wrap_up(agent):
    s = agent.new_session()
    say(agent, s, nlu(identity={"full_name": "Ava Lopez", "dob": "1990-08-21", "id_last4": "9180"}))
    assert s.phase == "POST_PROCESS" and "no_claims" in kinds(s)


def test_human_request_creates_handoff(agent):
    s = agent.new_session()
    say(agent, s, nlu(wants_human=True, reason_for_call="denied claim"))
    assert s.phase == "ESCALATED" and s.handoff["identity_verified"] is False


DAVID = {"caller_role": "representative", "representative_name": "David Chen", "representative_relationship": "son"}


def test_representative_gets_access_only_after_consent(agent):
    s = agent.new_session(consent_scenario="default")
    prompt = say(agent, s, nlu(identity=MARGARET, case_hints={"status": "denied"}, **DAVID))
    # Account located and representative on file, but nothing is disclosed while consent is pending.
    assert s.phase == "VERIFY_ID" and s.identity.verified_party_id is None and s.consent.status == "pending"
    assert {"account_located", "representative_on_file", "consent_requested"} <= set(kinds(s))
    for secret in ("CL-2048", "pathology", "1450", "margaret@email.com"):
        assert secret not in prompt
    say(agent, s, nlu(scope="smalltalk"))
    assert s.consent.status == "approved" and s.identity.verified_party_id == "P9"
    assert s.caller_role == "representative" and s.active_case_id == "CL-2048"
    assert s.caller_name is None  # the representative is not addressed by the policyholder's name


def test_consent_timeout_discloses_nothing(agent):
    s = agent.new_session(consent_scenario="timeout")
    say(agent, s, nlu(identity=MARGARET, **DAVID))
    say(agent, s, nlu(scope="smalltalk"))
    prompt = say(agent, s, nlu(scope="smalltalk", pushback_on_gate=True))
    assert s.consent.status == "timeout" and s.consent.checks == 3
    assert s.identity.verified_party_id is None and s.phase == "VERIFY_ID"
    assert "CL-2048" not in prompt and "human_offered" in kinds(s)


def test_unlisted_representative_is_refused_without_consent_request(agent):
    s = agent.new_session()
    say(agent, s, nlu(identity=MARGARET, caller_role="representative", representative_name="Kevin Lee"))
    assert s.consent.status == "not_on_file" and "consent_requested" not in kinds(s)
    assert s.identity.verified_party_id is None


def test_representative_needs_their_own_name(agent):
    s = agent.new_session()
    prompt = say(agent, s, nlu(identity=MARGARET, caller_role="representative"))
    assert s.consent.account_party_id == "P9" and s.consent.status is None
    assert "their own full name" in prompt
    say(agent, s, nlu(representative_name="David Chen"))
    assert s.consent.status == "pending"


def test_consent_tools_cannot_run_out_of_order(agent):
    s = agent.new_session()
    for tool in ("check_representative", "request_consent", "check_consent"):
        with pytest.raises(ToolDenied):
            agent.gateway.call(tool, s)


# --- Review findings (2026-09-29): each test reproduces the reported defect -------------------


def test_representative_change_while_pending_voids_authorization(agent):
    s = agent.new_session(consent_scenario="default")  # the next check would approve
    say(agent, s, nlu(identity=MARGARET, **DAVID))
    prompt = say(agent, s, nlu(representative_name="Kevin Lee"))
    assert s.consent.status == "revoked" and s.identity.verified_party_id is None
    assert "representative_changed" in kinds(s) and "consent_approved" not in kinds(s)
    assert "CL-2048" not in prompt


def test_representative_change_after_approval_revokes_access(agent):
    s = agent.new_session(consent_scenario="default")
    say(agent, s, nlu(identity=MARGARET, case_hints={"status": "denied"}, **DAVID))
    say(agent, s, nlu(scope="smalltalk"))
    assert s.identity.verified_party_id == "P9"
    prompt = say(agent, s, nlu(representative_name="Kevin Lee", followup_topic="payment_amounts"))
    assert s.identity.verified_party_id is None and s.phase == "VERIFY_ID" and s.active_case_id is None
    assert "1,450" not in prompt and "pathology" not in prompt


def test_first_name_alone_is_not_a_different_representative(agent):
    s = agent.new_session(consent_scenario="default")
    say(agent, s, nlu(identity=MARGARET, **DAVID))
    say(agent, s, nlu(representative_name="David"))
    assert s.consent.status == "approved" and s.representative_name == "David Chen"


def test_failed_understanding_changes_nothing_in_wrap_up(agent):
    s = agent.new_session()
    say(agent, s, nlu(identity=MARGARET, case_hints={"status": "denied"}))
    say(agent, s, nlu(wants_end=True))
    before = s.model_dump(exclude={"transcript", "events", "turn"})
    for _ in range(2):
        reply = fail_turn(agent, s)
    assert "didn't quite catch that" in reply
    assert s.model_dump(exclude={"transcript", "events", "turn"}) == before  # still waiting for send or skip


def test_failed_understanding_does_not_advance_consent(agent):
    s = agent.new_session(consent_scenario="default")
    say(agent, s, nlu(identity=MARGARET, **DAVID))
    fail_turn(agent, s)
    assert s.consent.status == "pending" and s.consent.checks == 1 and s.identity.verified_party_id is None


def test_switching_claims_without_details_asks_which(agent):
    s = agent.new_session()
    say(agent, s, nlu(identity=MARGARET, case_hints={"status": "denied"}))
    say(agent, s, nlu(wants_other_case=True, selected_case_id="CL-2048"))  # a carried-over pick is ignored
    assert s.phase == "RESOLVE_INTENT" and s.active_case_id is None
    assert "CL-2048" not in s.candidate_case_ids and len(s.candidate_case_ids) == 3


def test_question_in_the_same_message_as_send_is_answered_first(agent):
    s = agent.new_session()
    say(agent, s, nlu(identity=MARGARET, case_hints={"status": "denied"}))
    say(agent, s, nlu(wants_end=True))
    say(agent, s, nlu(email_decision="send", followup_topic="payment_amounts"))
    assert s.email.message is None and s.phase == "PROCESS_CASE" and "email_deferred" in kinds(s)
    say(agent, s, nlu(wants_end=True))
    assert s.phase == "POST_PROCESS" and "payment amounts" in str(s.email.summary)


def test_off_topic_during_the_email_offer_is_not_an_answer(agent):
    s = agent.new_session()
    say(agent, s, nlu(identity=MARGARET, case_hints={"status": "denied"}))
    say(agent, s, nlu(wants_end=True))
    for _ in range(3):
        say(agent, s, nlu(scope="out_of_scope"))
    assert s.phase == "POST_PROCESS" and s.email.decision is None and s.counters.email_unclear == 0
    assert "human_offered" in kinds(s)


def test_guard_rejects_amounts_only_the_caller_mentioned(agent):
    s = agent.new_session()
    say(agent, s, nlu(identity=MARGARET, case_hints={"status": "denied"}))
    s.transcript.append({"role": "user", "content": "I expect $9,999.00."})
    policy = agent._guard_policy(s, {"active_case_facts": agent._facts(s)})
    assert check_reply("The insurer has paid you $9,999.00.", policy)
    assert not check_reply("The fee-schedule figure is $1,450.00.", policy)


def test_caller_can_stop_during_verification(agent):
    s = agent.new_session()
    say(agent, s, nlu(identity={"full_name": "Margaret Chen"}))
    prompt = say(agent, s, nlu(wants_end=True, scope="smalltalk"))
    assert s.phase == "ENDED" and "more detail(s) to verify" not in prompt


def test_after_repeated_pushback_the_agent_stops_asking(agent):
    s = agent.new_session()
    for _ in range(3):
        prompt = say(agent, s, nlu(pushback_on_gate=True))
    assert "Stop asking for their details" in prompt and "human_offered" in kinds(s)


def test_summary_carries_the_callers_own_notes(agent):
    s = agent.new_session()
    note = {"kind": "doc_status", "value": "has the office note but not the pathology report"}
    say(agent, s, nlu(identity=MARGARET, case_hints={"status": "denied"}, memory_items=[note]))
    say(agent, s, nlu(wants_end=True))
    say(agent, s, nlu(email_decision="send"))
    assert note["value"] in s.email.message["body"]


def test_audit_log_masks_contact_details(agent):
    s = agent.new_session()
    pref = {"kind": "email_pref", "value": "send it to jane.doe@work.com or call 650-555-0101"}
    say(agent, s, nlu(memory_items=[pref]))
    details = " ".join(e.detail for e in s.events)
    assert "jane.doe@work.com" not in details and "650-555-0101" not in details
