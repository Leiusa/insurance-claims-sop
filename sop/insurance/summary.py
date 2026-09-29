"""Email summary and simulated delivery.

The summary is assembled from the session record (claims discussed, their facts, next steps),
not free-written by the model, so everything in it is grounded.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .data import FixtureRepo
from .facts import deadline_facts, money

TOPIC_LABELS = {
    "overview": "claim overview",
    "denial_reason": "why the claim was denied",
    "documents_needed": "which documents are needed",
    "document_details": "what the documents must include",
    "file_format": "file format requirements",
    "alternatives": "options if a document is unavailable",
    "submission_method": "how to submit documents",
    "submission_timing": "submission timing",
    "processing_time": "processing time after submission",
    "receipt_confirmation": "how receipt is confirmed",
    "claim_status": "claim status",
    "payment_amounts": "payment amounts",
    "appeal_deadline": "the appeal deadline",
    "next_steps": "next steps",
    "other": "other questions",
}


def mask_email(email: str | None) -> str:
    if not email or "@" not in email:
        return "the email on file"
    local, _, domain = email.partition("@")
    return f"{local[0]}{'*' * max(len(local) - 1, 3)}@{domain}"


def build_summary(
    *,
    party_id: str,
    discussed: dict[str, list[str]],
    handoff: dict[str, Any] | None,
    repo: FixtureRepo,
    today: date,
    representative: str | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    holder = repo.policyholder(party_id)
    claims, next_steps = [], []
    for case_id, topics in discussed.items():
        claim = repo.claim(case_id)
        if claim is None or claim.party_id != party_id:
            continue
        status = f"{claim.case_type.capitalize()} claim {claim.case_id} (filed {claim.created_at}) is {claim.status}."
        if claim.denial_reason:
            status += f" Reason: {claim.denial_reason}."
        if claim.status == "closed" and claim.net_pay is not None:
            status += f" Amount paid: {money(claim.net_pay)}."
        if claim.status == "open" and claim.expected_reimbursement_amount:
            status += f" Expected reimbursement: {money(claim.expected_reimbursement_amount)}."
        claims.append({"case_id": case_id, "status": status, "topics": [TOPIC_LABELS.get(t, t) for t in dict.fromkeys(topics)]})
        if claim.documents_needed:
            docs = ", ".join(claim.documents_needed)
            next_steps.append(
                f"{case_id}: submit the missing documents ({docs}) through the member portal or claim upload link; "
                "support can arrange fax or mail if needed."
            )
            deadline = deadline_facts(claim, today)
            if deadline and deadline["appeal_deadline_passed"]:
                next_steps.append(
                    f"{case_id}: the appeal deadline on record ({claim.appeal_deadline}) has passed; a claims "
                    "representative needs to confirm whether a late submission can still be considered."
                )
            elif deadline:
                next_steps.append(f"{case_id}: submit before the appeal deadline on record, {claim.appeal_deadline}.")
            next_steps.append(
                f"{case_id}: if a document can't be obtained, request a replacement copy from the provider; if no "
                "reasonable substitute exists, a claims representative can review manual options."
            )
    if handoff:
        next_steps.append(f"A representative will follow up (reference {handoff['ticket_id']}).")
    return {
        "first_name": holder.first_name if holder else "there",
        "representative": representative,
        "recipient_masked": mask_email(holder.email if holder else None),
        "claims": claims,
        "caller_notes": list(notes or []),  # the caller's own words (document status, preferences)
        "next_steps": next_steps or ["No further action is needed right now."],
        "prepared_on": today.isoformat(),
    }


def render_email(summary: dict[str, Any], *, to: str) -> dict[str, str]:
    intro = "Here is a summary of your conversation with claims support today."
    if summary.get("representative"):
        intro = (
            f"Here is a summary of today's conversation with claims support, held with {summary['representative']}, "
            "whom you approved to discuss your claims."
        )
    lines = [f"Hi {summary['first_name']},", "", intro, ""]
    if summary["claims"]:
        lines.append("What we discussed")
        for claim in summary["claims"]:
            lines.append(f"- {claim['status']}")
            if claim["topics"]:
                lines.append(f"  Topics covered: {', '.join(claim['topics'])}.")
    else:
        lines.append("We verified your identity and found no claims on file for your account.")
    if summary.get("caller_notes"):
        lines += ["", "What you told us"] + [f"- {note}" for note in summary["caller_notes"]]
    lines += ["", "Next steps"] + [f"- {step}" for step in summary["next_steps"]]
    lines += ["", "If anything here looks wrong, reply to this email or contact claims support.", "", "Claims Support"]
    return {"to": to, "subject": "Summary of your claims support conversation", "body": "\n".join(lines)}
