"""Identity verification: normalization and exact matching against policyholder records.

This is pure code on purpose. The LLM only extracts what the caller said; it never sees a
policyholder record, and it cannot influence the decision below.

Pass rule (fail-closed): at least REQUIRED_FACTORS distinct factors were provided and exactly
one record matches every one of them. A single contradicting factor fails the attempt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from .data import Policyholder

FACTORS = ("full_name", "dob", "phone", "email", "id_last4")
REQUIRED_FACTORS = 3

FACTOR_LABELS = {
    "full_name": "full name",
    "dob": "date of birth",
    "phone": "phone number on file",
    "email": "email address on file",
    "id_last4": "last 4 digits of SSN or national ID",
}

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _name_tokens(name: str) -> list[str]:
    return _WORD.findall(name.casefold())


def _name_keys(name: str) -> set[str]:
    """Every rotation of the name's tokens, joined without spaces.

    "Ya Wen Li" -> {"yawenli", "wenliya", "liyawen"}, so "Yawen Li" and "Li Yawen" match,
    and for two-token names both orders match ("Ma Tian" / "Tian Ma").
    """
    tokens = _name_tokens(name)
    return {"".join(tokens[i:] + tokens[:i]) for i in range(len(tokens))}


def normalize_name(raw: str) -> str | None:
    tokens = _name_tokens(raw)
    return "".join(tokens) if len(tokens) >= 2 else None  # a first name alone is not a full name


def normalize_dob(raw: str) -> str | None:
    try:
        return date.fromisoformat(raw.strip()).isoformat()
    except ValueError:
        return None


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def normalize_email(raw: str) -> str | None:
    value = raw.strip().casefold()
    return value if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) else None


def normalize_id_last4(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    return digits if len(digits) == 4 else None


_NORMALIZERS = {
    "full_name": normalize_name,
    "dob": normalize_dob,
    "phone": normalize_phone,
    "email": normalize_email,
    "id_last4": normalize_id_last4,
}


def normalize(factor: str, raw: str) -> str | None:
    """Canonical form of a caller-provided value, or None when it is unusable (e.g. 7-digit phone)."""
    return _NORMALIZERS[factor](raw)


def names_match(given: str, stored: str) -> bool:
    value = normalize_name(given)
    return value is not None and value in _name_keys(stored)


def factor_matches(factor: str, value: str, record: Policyholder) -> bool:
    """`value` must already be normalized."""
    if factor == "full_name":
        return any(value in _name_keys(n) for n in [record.name, *record.name_aliases])
    if factor == "dob":
        return normalize_dob(record.dob) == value
    if factor == "phone":
        phones = [p for p in [record.phone, *record.phone_aliases] if p]
        return any(normalize_phone(p) == value for p in phones)
    if factor == "email":
        emails = [e for e in [record.email, *record.email_aliases] if e]
        return any(normalize_email(e) == value for e in emails)
    if factor == "id_last4":
        # SSN and national ID last-4 are one factor; the stated type is not required to match,
        # because asking a type-specific question would reveal what is on file.
        return record.id_last4 == value
    return False


@dataclass
class VerificationResult:
    status: Literal["insufficient", "verified", "failed"]
    party_id: str | None = None
    factors: list[str] = field(default_factory=list)


def verify(provided: dict[str, str], records: list[Policyholder]) -> VerificationResult:
    """`provided` maps factor -> normalized value."""
    if len(provided) < REQUIRED_FACTORS:
        return VerificationResult("insufficient")
    hits = [r for r in records if all(factor_matches(f, v, r) for f, v in provided.items())]
    if len(hits) == 1:
        return VerificationResult("verified", hits[0].party_id, sorted(provided))
    return VerificationResult("failed")
