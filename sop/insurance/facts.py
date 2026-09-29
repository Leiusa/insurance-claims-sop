"""The grounded fact sheet for one claim.

Everything the reply model may say about a claim is in this sheet: the record, amounts with
their documented meaning, facts derived in code (deadline status), and the guideline text that
applies to this claim with its placeholders already filled in. The model explains; it does not
look anything up or compute anything itself.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .data import Claim, FixtureRepo

AMOUNT_FIELDS = ("expected_reimbursement_amount", "allowed_max_amount", "net_pay", "net_fee")

# Guardrails on how amounts may be described (from claim_schema.json semantics).
AMOUNT_NOTES = {
    "expected_reimbursement_amount": "An expected figure, not a payment that has been made.",
    "allowed_max_amount": "A ceiling on what the insurer would pay, not a payment.",
    "net_pay": "What the insurer actually paid.",
    "net_fee": "A fee-schedule figure. It is NOT an amount the member owes; never describe it as a bill or balance.",
}


def _en(node: Any) -> str | None:
    if isinstance(node, dict):
        return node.get("en")
    return node if isinstance(node, str) else None


def money(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return f"${float(value):,.2f}"
    except ValueError:
        return value


def map_document(name: str, keys: list[str]) -> str | None:
    """Map a claim's document name to a guideline key: exact match first, then containment.

    "pathology report" -> "original pathology report"; "office note" -> "treating provider
    office note"; "diagnosis report" -> None (general guidance applies).
    """
    n = name.casefold().strip()
    for key in keys:
        if key.casefold() == n:
            return key
    for key in keys:
        k = key.casefold()
        if n in k or k in n:
            return key
    return None


class _Fill(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def deadline_facts(claim: Claim, today: date) -> dict[str, Any] | None:
    if not claim.appeal_deadline:
        return None
    delta = (date.fromisoformat(claim.appeal_deadline) - today).days
    facts: dict[str, Any] = {"appeal_deadline": claim.appeal_deadline, "appeal_deadline_passed": delta < 0}
    if delta < 0:
        facts["days_since_deadline"] = -delta
    else:
        facts["days_until_deadline"] = delta
    return facts


def build_fact_sheet(claim: Claim, repo: FixtureRepo, today: date) -> dict[str, Any]:
    g = repo.guideline
    descriptions = repo.claim_schema.get("field_descriptions", {})
    docs = list(claim.documents_needed)
    processing_time = _en(g.get("claim_followup_settings", {}).get("average_processing_time_after_submission"))
    fill = _Fill(
        case_id=claim.case_id,
        documents=", ".join(docs) or "the requested documents",
        average_processing_time_after_submission=processing_time or "not specified",
    )

    amounts = []
    for field in AMOUNT_FIELDS:
        value = getattr(claim, field, None)
        if value is None:
            continue
        amounts.append(
            {
                "field": field,
                "amount": money(value),
                "meaning": descriptions.get(field, {}).get("description", ""),
                "how_to_describe": AMOUNT_NOTES[field],
            }
        )

    doc_guidance = g.get("document_guidance", {})
    alternatives = g.get("document_alternative_guidance", {})
    documents = []
    for doc in docs:
        key = map_document(doc, list(doc_guidance))
        documents.append(
            {
                "document": doc,
                "matched_guideline_entry": key,
                "what_it_should_include": _en(doc_guidance.get(key)) if key else None,
                "if_not_available": _en(alternatives.get(key)) if key in alternatives else _en(alternatives.get("default")),
            }
        )

    deadline = deadline_facts(claim, today)
    deadline_passed = bool(deadline and deadline["appeal_deadline_passed"])

    followups = []
    for item in g.get("claim_followup_guidance", []):
        if item.get("requires_documents") and not docs:
            continue
        if item["topic"] == "submission_timing" and deadline_passed:
            # Code resolves the conflict: generic "within a week" guidance no longer applies
            # once the recorded deadline has passed, so the model never sees it (timing_note covers timing).
            continue
        followups.append({"topic": item["topic"], "guidance": item["en"].format_map(fill)})

    derived: dict[str, Any] = {}
    if deadline:
        derived.update(deadline)
    if claim.net_pay is not None:
        derived["payment_issued"] = float(claim.net_pay) > 0

    timing_note = None
    if deadline and deadline["appeal_deadline_passed"]:
        timing_note = (
            f"The recorded appeal deadline ({claim.appeal_deadline}) passed {deadline['days_since_deadline']} days ago. "
            "Do not tell the caller they still have a week or that the appeal window is open. Say that a claims "
            "representative must confirm whether a late submission can still be considered, and offer to connect them."
        )
    elif deadline:
        timing_note = (
            f"Submit before the recorded appeal deadline ({claim.appeal_deadline}), which is "
            f"{deadline['days_until_deadline']} days from today. If general guidance such as 'within a week' would "
            "fall after the deadline, the deadline controls."
        )

    guidance = {
        "general_submission": _en(g.get("default_guidance")),
        "case_type": _en(g.get("case_type_guidance", {}).get(claim.case_type)),
        "documents": documents,
        "followup_topics": followups,
        "average_processing_time_after_submission": processing_time if docs else None,
        "fallback": _en(g.get("claim_followup_fallback")) if docs else None,
        "when_to_involve_a_human": _en(
            g.get("claim_followup_settings", {}).get("human_review_after_document_alternatives_exhausted")
        ),
    }

    return {
        "as_of_date": today.isoformat(),
        "claim": {
            "case_id": claim.case_id,
            "case_type": claim.case_type,
            "filed_date": claim.created_at,
            "status": claim.status,
            "summary": claim.summary,
            "denial_reason": claim.denial_reason,
            "documents_needed": docs,
            "appeal_deadline": claim.appeal_deadline,
        },
        "amounts": amounts,
        "derived": derived,
        "timing_note": timing_note,
        "guidance": {k: v for k, v in guidance.items() if v not in (None, [], "")},
        "notes": ["filed_date is when the claim was created, not necessarily the date of service."],
    }


def amounts_in_sheet(sheet: dict[str, Any]) -> set[str]:
    """Normalized amounts ("1450.00") that the output guard allows in a reply about this claim."""
    values = set()
    for item in sheet.get("amounts", []):
        try:
            values.add(f"{float(item['amount'].replace('$', '').replace(',', '')):.2f}")
        except (ValueError, AttributeError):
            continue
    return values
