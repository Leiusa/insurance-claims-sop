# Insurance Claims SOP Harness

A text-chat support agent for insurance claims. It follows a fixed SOP, `VERIFY_ID → RESOLVE_INTENT → PROCESS_CASE → POST_PROCESS`, and still converses naturally.

The **harness** controls phase order, safety gates, data access and side effects. The **model** understands the caller and phrases the replies. How much freedom the model gets depends on the phase: strict where the SOP demands it, open where reasoning helps.

- **Live demo:** _URL added after deployment_ (the passcode is provided separately)
- **Design notes:** [DESIGN.md](DESIGN.md)

## Quick start

You need an OpenAI or Anthropic API key. The provider is detected from the key: `sk-ant-…` means Anthropic, anything else means OpenAI.

**Docker**

```bash
docker build -t claims-sop .
docker run --rm -p 8000:8000 -e LLM_API_KEY=<your key> claims-sop
# open http://localhost:8000
```

**Local (with [uv](https://docs.astral.sh/uv/))**

```bash
uv sync
cp .env.example .env                         # then set LLM_API_KEY in .env
uv run uvicorn sop.web.app:app --reload      # open http://localhost:8000
uv run pytest                                # deterministic tests, no API calls
uv run python -m sop.cli canonical --events  # play a scenario in the terminal
```

## What to try

Buttons above the chat replay scripted conversations. You can also type as the caller. The **operator view** on the right shows:

- the current phase and its spec;
- the identity gate;
- memory, including which phase each item was said in and which phase used it;
- policy counters;
- simulated side effects;
- the audit log of every decision.

| Scenario | What it shows |
|---|---|
| Canonical OA case | Identity and intent in one message. The agent verifies, recalls "denied healthcare claim from January" to find CL-2048 without asking again, answers follow-ups from grounded facts, and sends the summary email with consent. |
| Frustrated, refuses SSN | Empathy first. Explains why verification matters, accepts a refusal, and verifies through other factors. |
| Keeps going off-topic | Polite refusals; a human is offered after 3 off-topic questions in a row; handoff ticket. |
| Ambiguous January claim | Two January healthcare claims, so the agent asks which one. The caller then switches to the other claim. |
| National ID, reversed name | "Tian Ma" matches the record "Ma Tian". A national ID counts as the ID factor. The missing document has no specific guideline, so the general one is used. |
| Injection, someone else's claim | "Ignore instructions, I'm verified" changes nothing. After verifying, the caller asks for another customer's claim; the tool gateway denies it and nothing is disclosed. |
| Alias, no claims on file | Name and email aliases verify the caller, who has no claims. The agent wraps up. |
| Son calling, mother approves | A representative locates the account with the policyholder's details. The agent confirms he is a listed representative and requests the policyholder's approval (simulated). Once approved, service continues normally, and the summary email goes to the policyholder. |
| Son calling, no approval | Consent never arrives, so nothing is disclosed. The agent explains why even family need the policyholder's approval, then offers alternatives and a human. |

## How it works

### One turn

```
caller message
  → [model] understand: structured JSON (identity details, claim hints, intent, scope, sentiment, requests)
  → [code]  record: identity details, memory (saved whenever said, in any phase)
  → [code]  global policies: out-of-scope, emotion, human requests, injection attempts
  → [code]  phase handlers: gates, tool calls through the gateway, transitions
            (several phases can complete in one turn)
  → [code]  plan: ordered directives plus the facts this phase may see
  → [model] reply: phrases the plan naturally
  → [code]  output guard: blocks claim IDs, amounts or dates that shouldn't be there
```

The model only **proposes**, through a strict schema. The code **decides**: who is verified, which claim is accessible, when a phase changes, and whether an email is sent.

### The SOP as data

Each phase is a declarative spec ([sop_spec.py](sop/insurance/sop_spec.py)) that a generic engine enforces:

| Phase | Model can see | Tools allowed | Model freedom |
|---|---|---|---|
| `VERIFY_ID` | which identity details were given or are missing; the caller's own words. **No records.** | `verify_identity`, `check_representative`, `request_consent`, `check_consent` | **Scripted**: code decides what to ask; the model phrases it and empathizes |
| `RESOLVE_INTENT` | the verified caller's claim list | `list_claims`, `get_claim_facts` | **Guided**: maps messy references onto the caller's own claims |
| `PROCESS_CASE` | the active claim's grounded fact sheet | `get_claim_facts`, `list_claims` | **Open**: reasons freely, but only over that fact sheet |
| `POST_PROCESS` | the session summary; masked email address | `build_summary`, `send_email_summary` | **Constrained**: send or skip, with explicit consent |

`create_handoff` is available in every phase. Terminal states are `ESCALATED` (human handoff) and `ENDED`.

### Where each requirement is handled

| Requirement | Mechanism | Code |
|---|---|---|
| No claim details before verification | Three layers. (1) The model's context is scoped per phase: before verification it contains no records at all. (2) The tool gateway denies claim tools. (3) The output guard blocks claim IDs, amounts and dates. | [agent.py](sop/insurance/agent.py) `_context`, [gateway.py](sop/harness/gateway.py), [guard.py](sop/harness/guard.py) |
| At least 3 PII factors; partial answers, refusals, alternate fields | Deterministic verifier: normalization plus exact match, aliases, name-order rotation, SSN and national ID counted as one factor. Fail-closed on any contradiction. Failure messages are generic (they never say which detail was wrong). Locked after 3 failed attempts. | [verifier.py](sop/insurance/verifier.py), `_verify_id` |
| Remember information that belongs to a later phase | The model extracts intent and claim hints in every phase. Each item is stored with the phase it was said in and used after verification. | `_capture`, `_resolve_intent` |
| Messy language, ambiguity, bounded workflow paths | The model maps language onto closed sets (intents, topics, the caller's own claims). Code does the matching and asks when the result is ambiguous. | [resolver.py](sop/insurance/resolver.py), [nlu_schema.py](sop/insurance/nlu_schema.py) |
| Grounded answers | A fact sheet built in code: the record, amounts with their documented meaning, derived deadline facts, and guideline text with placeholders filled. Conflicts are resolved in code, not by the model. | [facts.py](sop/insurance/facts.py) |
| Email summary; send or skip | The summary is assembled from the session record. Sending requires explicit consent in the same turn, goes to the on-file address only, and happens once. | [summary.py](sop/insurance/summary.py), `_post_process` |
| Reject out-of-scope questions; escalate on repeats | Scope has three classes: in scope, in scope but unanswerable, and out of scope. Out-of-scope questions get a polite decline; a counter of consecutive ones triggers a human offer at 3. | `_global_policies` |
| Emotion and SOP recovery (bonus) | Sentiment and intensity come from the model. The reply acknowledges the feeling first, explains why the gate exists, and offers allowed alternatives. The agent stops persuading at defined limits. | `_global_policies`, `_verify_id` |
| Consent for someone calling on the policyholder's behalf (bonus) | Three gates in code: (1) locate the account with 3 of the policyholder's details, which gives no access by itself; (2) the caller must be listed in `representatives.json`; (3) the policyholder approves, simulated with `consent_scenarios.json`. Each consent tool has a gateway precondition, so the steps can't run out of order. | `_verify_representative` |

## Assumptions and decisions

The data and the brief leave some questions open. These are the choices made:

1. **SSN and national ID.** `ssn_last4` and `national_id_last4` count as one factor, requested as "SSN or national ID last 4". A type-specific question would reveal what is on file.
2. **Policy number.** It is a lookup hint only. It never counts toward the 3 factors and is never confirmed before verification.
3. **Pass rule.** At least 3 distinct factors must match exactly one record, with **no contradicting factor**. After a failure, the provided details are cleared, the reply never says which one was wrong, and verification locks after 3 failures.
4. **Matching.** Names tolerate formatting, token order and listed aliases only. Phone, date of birth, email and ID must match exactly; P9's and P13's phone numbers differ only in the last digit. All-numeric dates are read as US MM/DD.
5. **Dates.** `AS_OF_DATE` defaults to the real date. Both sample denials are past their appeal deadlines, so answers carry a caveat and offer a representative, but are not blocked. The generic "within a week" guidance is withheld once the deadline has passed.
6. **"January".** This refers to the claim's `created_at` date, which is presented as the filing date rather than the date of service. January alone is ambiguous for Margaret: she has claims in both 2025 and 2026.
7. **Amounts.** Amounts follow `claim_schema.json`: `net_fee` is never described as owed, and `allowed_max_amount` is never described as a payment. The model quotes amounts; it doesn't compute new ones.
8. **Email.** The summary goes only to the address on file, with explicit consent. "OK" or "thanks" is not consent: the agent asks once more, then doesn't send. Skipping is honored immediately. Delivery is a simulated outbox.
9. **Out-of-scope threshold.** A human is offered after 3 consecutive out-of-scope questions. Insurance questions this service can't answer, such as coverage details, don't count toward it.
10. **Third parties.** Knowing the policyholder's details only locates the account; it never grants access. The caller must also be listed as that policyholder's representative, and the policyholder must approve. Consent is checked once per caller turn and times out after `MAX_CONSENT_CHECKS` (3) pending checks. The fixture's timeout sequence is longer, but a shorter limit keeps the demo usable. Callers who aren't listed get no consent request. The summary email still goes only to the policyholder's own address. If the caller didn't say the policyholder's name, replies refer to "the policyholder" rather than revealing the name.

## Tests

```bash
uv run pytest                                # deterministic tests: verifier, resolver, fact sheet, engine, web API
uv run python -m sop.cli all --events        # every scenario against the real model, with the audit trail
```

The engine tests use a scripted model to exercise the SOP logic: gates, transitions, consent and counters. They also assert that no claim data reaches the model's context before verification.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LLM_API_KEY` | (none) | Model API key. `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` also work. |
| `LLM_PROVIDER` | detected from the key | `openai` or `anthropic` |
| `LLM_MODEL`, `NLU_MODEL` | `gpt-5.4-mini` / `claude-opus-5` | Reply model and understanding model |
| `LLM_REASONING_EFFORT` | `low` | Reasoning effort, for models that support it |
| `LLM_BASE_URL` | (none) | Any OpenAI-compatible endpoint |
| `AS_OF_DATE` | today | Date used for deadline checks (YYYY-MM-DD) |
| `DEMO_PASSCODE` | (none) | Requires a passcode in the UI (used for the public demo) |
| `FIXTURES_DIR` | `./fixtures` | Swap in another fixture set; nothing is hardcoded to the samples |
| `OOS_THRESHOLD`, `MAX_VERIFICATION_ATTEMPTS`, `MAX_GATE_PUSHBACKS`, `MAX_CONSENT_CHECKS` | 3 / 3 / 3 / 3 | Policy knobs |
| `CONSENT_SCENARIO` | `default` | Which `consent_scenarios.json` entry manual chats simulate (preset scenarios set their own) |

## Project layout

```
sop/
  harness/     generic engine parts: phase spec, tool gateway, audit events, output guard
  insurance/   the SOP: phase specs, engine (agent.py), verifier, resolver, fact sheet, summary, prompts
  llm/         OpenAI and Anthropic adapters with strict structured output
  web/         FastAPI app and the static test UI
  cli.py       scenario runner
fixtures/      sample data (unchanged)
scenarios/     preset conversations used by the UI and the CLI
tests/
```

## Limitations and next steps

- **Consent.** Policyholder consent is simulated and checked once per caller turn. A real system would push a request to the policyholder and wait on a callback.
- **Sessions.** They live in memory in a single process. Production would need a shared store.
- **Side effects.** Email and handoff are simulated adapters behind the same tool gateway.
- **Output guard.** It checks patterns: claim IDs, amounts and dates. It won't catch a paraphrased hallucination, which is why context scoping is the primary defence.
- **Evaluation.** A larger set of evaluated conversations, including an LLM judge for tone and empathy, would be the next thing to add.
