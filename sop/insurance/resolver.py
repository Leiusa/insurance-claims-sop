"""Case resolution: maps what the caller said about a claim onto the verified caller's own claims.

The LLM turns messy language into hints ("my denied medical claim from January" ->
status=denied, type=healthcare, month=1); this module does the matching, and only ever
against the claims of the verified caller.
"""

from __future__ import annotations

import calendar

from pydantic import BaseModel

from .data import Claim

TYPE_SYNONYMS = {
    "medical": "healthcare",
    "health": "healthcare",
    "hospital": "healthcare",
    "car": "auto",
    "vehicle": "auto",
    "automobile": "auto",
    "dentist": "dental",
}
STATUS_SYNONYMS = {
    "rejected": "denied",
    "declined": "denied",
    "in progress": "open",
    "pending": "open",
    "processing": "open",
    "settled": "closed",
    "paid": "closed",
    "completed": "closed",
}


class CaseHints(BaseModel):
    case_id: str | None = None
    case_type: str | None = None
    status: str | None = None
    month: int | None = None
    year: int | None = None

    def any(self) -> bool:
        return any(v is not None for v in self.model_dump().values())

    def merged(self, newer: CaseHints) -> CaseHints:
        """Newer non-empty values override older ones."""
        data = self.model_dump()
        data.update({k: v for k, v in newer.model_dump().items() if v is not None})
        return CaseHints(**data)

    def describe(self) -> str:
        parts = [p for p in (self.status, self.case_type) if p]
        text = " ".join(parts + ["claim"])
        if self.case_id:
            text += f" {self.case_id}"
        if self.month and 1 <= self.month <= 12:
            text += f" from {calendar.month_name[self.month]}"
            if self.year:
                text += f" {self.year}"
        elif self.year:
            text += f" from {self.year}"
        return text


def _canon(value: str, synonyms: dict[str, str]) -> str:
    v = value.strip().lower()
    return synonyms.get(v, v)


def claim_matches(claim: Claim, hints: CaseHints) -> bool:
    if hints.case_id and claim.case_id.upper() != hints.case_id.strip().upper():
        return False
    if hints.case_type and _canon(hints.case_type, TYPE_SYNONYMS) != claim.case_type.lower():
        return False
    if hints.status:
        status = claim.status.lower()
        if hints.status.strip().lower() != status and _canon(hints.status, STATUS_SYNONYMS) != status:
            return False
    year, month = int(claim.created_at[:4]), int(claim.created_at[5:7])
    if hints.month and hints.month != month:
        return False
    if hints.year and hints.year != year:
        return False
    return True


def match_claims(claims: list[Claim], hints: CaseHints) -> list[Claim]:
    return [c for c in claims if claim_matches(c, hints)]
