# Insurance Claims SOP Harness: Design

Status: draft for review, 2026-09-28

## 1. Goal and principles

This is a text-chat support agent for insurance claims. It follows a fixed SOP (`VERIFY_ID → RESOLVE_INTENT → PROCESS_CASE → POST_PROCESS`) and still talks naturally. The harness, not the model, controls phase order, safety gates, data access and side effects. The LLM interprets what the caller says and writes the replies, and it gets a different amount of freedom in each phase.

1. **Code decides; the LLM proposes.** Verification results, data access, phase transitions and email sends are all deterministic. The harness treats LLM output as untrusted input.
2. **Scope the context instead of asking the model not to leak.** Before verification, no LLM prompt contains any policyholder or claim record, so there is nothing to leak.
3. **Facts are grounded; phrasing is free.** Amounts, dates, denial reasons and document lists come from the records, the guidelines, or facts computed in code (for example, whether a deadline has passed). The LLM explains them and never invents them.
4. **Remember early, use later.** Whatever the caller says is stored as soon as they say it. Memory never grants access.
5. **The SOP is declarative.** Each phase is a spec (context scope, tool allowlist, autonomy level, exit guard) that a generic engine interprets.

## 2. Turn pipeline

```
caller message
  │
  ▼
[1] NLU (LLM, structured JSON)   slots, memory items, intent/topic, scope, sentiment,
  │                               requests (human / end / other case / email decision)
  ▼
[2] State update (code)          merge slots and memory, update counters
  ▼
[3] Policy engine (code)         phase handler → gated tool calls → exit guards;
  │                               loops until stable (several phases may complete in one turn)
  ▼
[4] Response plan (code)         ordered directives + phase-scoped fact sheet
  ▼
[5] NLG (LLM)                    renders the plan as a natural reply
  ▼
[6] Output guard (code)          blocks pre-verification disclosure, foreign case IDs,
  │                               ungrounded amounts
  ▼
reply + audit events (operator panel)
```

The LLM never calls tools. It proposes actions through the NLU output, for example a selected case or a request for a human. The engine carries them out through a single tool gateway. The gateway checks the current phase's allowlist and each tool's preconditions, for example `claim.party_id == session.verified_party_id`. The party ID always comes from the session, never from model output. Denied calls are logged and never executed.

Failures: if the NLU returns invalid JSON, it is retried once; after that the turn is treated as an empty extraction, with no state change and no phase advance. If the output guard rejects a reply, it is regenerated once, then replaced by a deterministic template.

## 3. Phase specs

| Phase | LLM can see | Gateway allowlist | Autonomy | Exit guard (code) |
|---|---|---|---|---|
| `VERIFY_ID` | transcript; which slot *types* were provided or refused; how many are still needed; the caller's own stated hints | `verify_identity` | **Scripted.** The engine decides what to ask; the LLM phrases it, empathizes and answers "why" questions. | Exactly one policyholder matches at least 3 distinct PII factors, with no contradictions |
| `RESOLVE_INTENT` | the verified caller's claim index (id, type, filed date, status); memory | `list_claims` | **Guided.** The LLM maps messy references onto a closed candidate set. | Exactly one active case, or no claims at all (goes to `POST_PROCESS`) |
| `PROCESS_CASE` | fact sheet for the active case (§8); memory | `get_claim`, `get_guidance`, `request_human` | **Open.** The LLM reasons freely over the fact sheet, but only over the fact sheet. | Caller is done → `POST_PROCESS`; another case → `RESOLVE_INTENT`; human → `ESCALATED` |
| `POST_PROCESS` | structured session summary; masked on-file email | `send_email_summary` (requires explicit consent tied to this summary and recipient) | **Constrained.** Send or skip. | Sent or skipped → `ENDED`; more questions → `PROCESS_CASE` |

Terminal states are `ESCALATED` (a human handoff ticket was created) and `ENDED`. Global handlers run in every phase: out-of-scope questions, emotion, explicit requests for a human, and injection attempts.

## 4. Session state (abridged)

```
phase, caller_role (self | representative | unknown)
identity:  provided {slot: value, turn}, refused {slot}, failed_attempts, verified_party_id
memory:    [{kind: intent|case_hint|doc_status|email_pref|context, value, turn, phase_said}]
cases:     candidates, active_case_id, discussed_case_ids
counters:  oos_consecutive, gate_pushbacks, email_unclear
pending_question: need_identity | choose_case | anything_else | email_consent | ...
email:     offered, decision, sent_message_id          handoff: ticket | null
events:    audit log (phase changes, tool calls allowed/denied, guard hits, ...)
```

## 5. NLU output (one per message)

- **Identity:** `identity` {name, dob (ISO date), phone, email, id_last4, id_type_said}, `policy_number`, `refused_slots`.
- **Caller:** `caller_role`, plus the representative's name and relationship when given.
- **Intent and case:** `intent`, `case_hints` {case_id, case_type, status, month, year}, `selected_case_id` (must come from the candidate list supplied to the NLU), `followup_topic`, `memory_items`.
- **Scope:** `scope` (in_scope | in_scope_unanswerable | out_of_scope | smalltalk) and `has_oos_part`.
- **Conversation signals:** `sentiment` with `intensity`, `asks_why`, `pushback_on_gate`, `wants_human`, `wants_end`, `wants_other_case`, `email_decision` (send | skip | unclear), `injection_attempt`.

The `intent` enum reuses the `intent_hints` from `required_document_guideline.json`: denial_question, status_inquiry, document_submission, next_steps, general_claim_question.

## 6. Identity verification policy

- **Factors.** Full name, DOB, phone, email, and the last 4 digits of a government ID. The policy number is only a lookup hint. It never counts as a factor and is never confirmed before verification.
- **Normalization, then exact match:**
  - Name: ignores case, punctuation and whitespace, allows token-order rotation ("Ma Tian" = "Tian Ma"), and checks `name_aliases`.
  - Phone: compares the last 10 digits and checks `phone_aliases`.
  - Email: lowercased, and checks `email_aliases`.
  - DOB: parsed to an ISO date; all-numeric dates are read as US MM/DD.
  - Government ID: exactly 4 digits.

  There is no fuzzy matching: P9's and P13's phone numbers differ only in the last digit.
- **Government ID type.** `ssn_last4` and `national_id_last4` count as one factor, requested as "SSN or national ID last 4". The type the caller states is logged but does not have to match, because asking a type-specific question would reveal what is on file.
- **Pass rule (fail-closed).** Exactly one record matches at least 3 distinct factors, and none of the provided factors contradicts that record.
- **Anti-enumeration.** Until 3 factors have been provided, replies depend only on what was *provided*, never on what *matched*. A failed attempt gets one generic reply ("I couldn't verify those details"). The provided factors are then cleared (memory is kept) and the caller tries again. After 3 failed attempts, verification is locked and a human handoff is offered.
- **Refusals.** A refused factor is never asked for again; the agent offers the remaining ones instead. If fewer than 3 factors are still possible, the agent explains why and offers a human.

## 7. Case resolution

After verification, the remembered hints (case_id, type, status, and `created_at` month/year) filter the caller's claims:

- **One match:** select it and continue in the same turn. Name the claim so the caller can correct the choice.
- **More than one:** ask a single disambiguation question built from the attributes that tell the claims apart. Example: Margaret has two January healthcare claims, denied in 2026 and closed in 2025.
- **No match:** say so and briefly list the caller's claims.
- **No claims at all:** say so, offer a human, and move to `POST_PROCESS`.

`created_at` is presented as the filing date, not the date of service.

## 8. Grounded answering (`PROCESS_CASE`)

The fact sheet for the active case contains:

- **The claim record.** Amount labels come from `claim_schema.json`: `net_fee` is not "what you owe", and `allowed_max_amount` is not a payment.
- **Derived facts.** Computed in code against `AS_OF_DATE` (default: today): whether `appeal_deadline_passed`, and the number of days until or since the deadline.
- **Guidance:**
  - The default guidance and the case-type guidance.
  - Per-document guidance and alternatives, matched to guideline keys by containment ("pathology report" ⊂ "original pathology report"). Unmatched documents such as "diagnosis report" fall back to the default entries.
  - Follow-up topics with `{case_id}`, `{documents}` and `{average_processing_time_after_submission}` filled in by code. These topics apply only to claims with `documents_needed`.
  - The fallback text and the rule for handing off to a human reviewer.

**Topic selection.** The NLU picks the topic from an enum. The `match_any` keywords are only hints, because they overlap ("how soon" matches two topics).

**Past deadlines.** If the appeal deadline has passed, the agent still answers questions about the denial reason, the required documents, alternatives and how to submit. Timing answers state the recorded deadline, say that it has passed, and explain that a representative has to confirm whether a late submission can still be considered; a human is offered. "Within a week" is never presented as still valid after the deadline.

**Output guard.** Any case ID in a reply must belong to the verified caller, and any amount must appear in the fact sheet.

## 9. `POST_PROCESS`: email summary

- **Entry.** The phase starts when the caller is done. The LLM fills a fixed structure: what was discussed, claim status/outcome, and next steps. The structure is validated like any other output, rendered from a template, and previewed in the UI.
- **Recipient.** Only the email on file, shown masked (`m*******@email.com`). A request for a different address is declined for privacy reasons, and the caller is pointed to the portal or a representative.
- **Consent.** The summary is sent only on explicit consent to the pending offer. "Thanks" or "ok" counts as unclear: the agent asks once more, and if the answer is still unclear, nothing is sent. A skip takes effect immediately, with no persuasion. Each summary is sent at most once. In the demo, sending means an in-app outbox that is labelled as simulated.
- **Earlier preference.** If the caller said earlier "email me the details", the offer mentions it, but consent is still confirmed here.

## 10. Conversation policies (all phases)

- **Scope.**
  - `in_scope`: normal flow.
  - `in_scope_unanswerable`, for example coverage questions the data can't answer: say the information isn't available here and offer a human. This does not count as out of scope.
  - `out_of_scope`: decline politely and steer back to the current step. `oos_consecutive` goes up by one and resets on any in-scope turn; at 3, the agent offers a human representative.
  - Mixed messages: handle the in-scope part and briefly decline the rest, without counting it.
- **Emotion.** For frustration, anger, anxiety or confusion, the agent acknowledges it first and then continues the SOP step. At a gate, it explains why the gate exists (it protects the caller's medical and financial information), offers the allowed alternatives (other ID factors, a human), and keeps the conversation moving.
- **When to stop persuading.** An explicit request for a human is handed off immediately. The agent also offers a human instead of repeating itself when any of these holds: `gate_pushbacks` reaches 3, intensity reaches 3, verification is locked, or not enough factors remain.
- **Injection and social engineering.** Examples: "ignore your instructions", "the last agent already verified me". No text can change state. The reply restates the requirement, and an event is logged.
- **Handoff.** Creates a mock ticket with the phase, verification status, intent and summary, so the caller doesn't have to repeat themselves. The session moves to `ESCALATED`.

## 11. Third-party callers and consent (P1)

`representatives.json` and `consent_scenarios.json` are modelled as an authorized-representative flow:

1. The caller says they are calling for a policyholder.
2. The policyholder's factors are verified.
3. The caller is checked against the representative record.
4. The policyholder's consent is requested. This is simulated: the status sequence advances by one on each turn. In `default` it goes pending → approved; in `timeout` it stays pending.
5. If approved, the agent proceeds with `role=representative`. On timeout, nothing is disclosed and the agent offers a human or a callback.

P0 fallback: a third-party caller gets no account details, an explanation, and the option of a human.

## 12. Test UI

- **Layout.** A single page, with the chat on the left and an operator panel on the right.
- **Operator panel:**
  - phase stepper
  - identity factors provided and matched (types and masked values only)
  - memory, showing the phase each item was said in and the phase it was used in
  - active case and counters
  - audit events
  - email preview and outbox
  - handoff ticket
  - `AS_OF_DATE` and the model in use
- **Preset buttons.** Each replays a scripted conversation: canonical Margaret, frustrated caller, repeated out-of-scope questions, refuses SSN, January ambiguity, another customer's case ID, prompt injection, and son calling (P1).

## 13. Configuration and delivery

**Stack.** Python 3.12+, FastAPI, Pydantic, vanilla JS (no build step), one Docker image.

**Environment variables:**

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `openai` or `anthropic`; auto-detected from whichever key variable is set |
| `LLM_API_KEY` | model API key (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` also accepted) |
| `LLM_MODEL`, `NLU_MODEL` | reply model and extraction model |
| `LLM_BASE_URL` | any OpenAI-compatible endpoint |
| `AS_OF_DATE` | the date that deadline checks use |
| `FIXTURES_DIR` | where fixtures are loaded from |
| `DEMO_PASSCODE` | access code for the hosted demo |
| `OOS_THRESHOLD` | out-of-scope count that triggers the human offer |

API keys stay on the server and are never logged. All data comes from `FIXTURES_DIR`, and nothing is hardcoded to the sample customers.

Not included in this demo: real email delivery, telephony, persistent sessions (they live in memory), and authentication for the operator panel.

## 14. Tests

- **Deterministic (no LLM):**
  - verifier traps: near-identical phones, aliases, name order, national ID, factors from different customers, contradictions
  - case resolution: "January" is ambiguous, and adding "denied" resolves it to CL-2048
  - document mapping and deadline facts
  - tool gateway denials, email consent rules, and the out-of-scope counter
  - multi-turn engine flows driven by scripted NLU outputs
- **Live (real LLM):**
  - scenario scripts that assert on state and events, never on wording
  - a check that no claim data appears in any reply before verification

## 15. Trade-offs

- **Two LLM calls per turn (NLU + NLG).** Adds latency in exchange for control, testability and guaranteed memory capture.
- **Pre-built fact sheet instead of free tool use in `PROCESS_CASE`.** Gives the model less autonomy, but keeps the context bounded and grounded.
- **Fail-closed verification.** Can frustrate a caller whose phone or email is out of date. A retry and the human option soften this.
- **Pattern-level output guard.** Catches IDs and amounts but not paraphrased hallucinations. Scoping the context is the primary defence; the guard is a backstop.

## 16. Assumptions and decisions

1. SSN last 4 and national ID last 4 count as one factor.
2. The policy number is not a PII factor.
3. A pass needs at least 3 distinct factors matching one record, with no contradictions. Failures get a generic reply, and 3 failures lead to a human.
4. Name matching tolerates only formatting, token order and listed aliases.
5. `AS_OF_DATE` defaults to the real date. Both sample denials are past their appeal deadlines; this adds a caveat to answers but does not block them.
6. "January" refers to the claim's `created_at` date (the filing date).
7. Email goes only to the address on file, only with explicit consent, and sending is simulated.
8. The out-of-scope threshold is 3 consecutive turns. In-scope questions the agent can't answer don't count.
9. A representative needs the policyholder's consent (P1).
10. Human handoff and email are simulated adapters.

## 17. Layout

```
insurance_claims/
  fixtures/              given
  sop/
    harness/             generic engine: phase spec, tool gateway, events, output guard
    insurance/           the SOP: phase specs, handlers, verifier, resolver, fact sheet, email
    llm/                 provider adapters, NLU and NLG prompts
    web/                 FastAPI app and static UI
  scenarios/             preset conversations
  tests/
  pyproject.toml  Dockerfile  .env.example  README.md  DESIGN.md
```
