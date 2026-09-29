"""Fixture loading. Everything the agent knows about customers and claims comes from here.

Models allow extra fields so a different fixture set with more columns still loads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict


class Policyholder(BaseModel):
    model_config = ConfigDict(extra="allow")

    party_id: str
    name: str
    dob: str
    policy_number: str | None = None
    id_type: str | None = None
    id_last4: str | None = None
    phone: str | None = None
    email: str | None = None
    name_aliases: list[str] = []
    phone_aliases: list[str] = []
    email_aliases: list[str] = []

    @property
    def first_name(self) -> str:
        return self.name.split()[0]


class Claim(BaseModel):
    model_config = ConfigDict(extra="allow")

    case_id: str
    party_id: str
    case_type: str
    created_at: str
    status: str
    summary: str = ""
    denial_reason: str | None = None
    documents_needed: list[str] = []
    appeal_deadline: str | None = None
    expected_reimbursement_amount: str | None = None
    allowed_max_amount: str | None = None
    net_pay: str | None = None
    net_fee: str | None = None

    def brief(self) -> dict[str, str]:
        """The minimal description used to list or disambiguate claims."""
        return {
            "case_id": self.case_id,
            "case_type": self.case_type,
            "filed_date": self.created_at,
            "status": self.status,
        }


class Representative(BaseModel):
    model_config = ConfigDict(extra="allow")

    rep_name: str
    relationship: str
    buyer_name: str
    buyer_party_id: str


class FixtureRepo:
    """Read-only access to the fixture files in one directory."""

    def __init__(self, fixtures_dir: Path):
        self.dir = fixtures_dir
        self.policyholders = [Policyholder(**p) for p in self._load("policyholders.json", [])]
        self.claims = [Claim(**c) for c in self._load("claims.json", [])]
        self.representatives = [Representative(**r) for r in self._load("representatives.json", [])]
        self.guideline: dict[str, Any] = self._load("required_document_guideline.json", {})
        self.claim_schema: dict[str, Any] = self._load("claim_schema.json", {})
        self.consent_scenarios: dict[str, Any] = self._load("consent_scenarios.json", {})

    def _load(self, name: str, default: Any) -> Any:
        path = self.dir / name
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def policyholder(self, party_id: str) -> Policyholder | None:
        return next((p for p in self.policyholders if p.party_id == party_id), None)

    def claims_for(self, party_id: str) -> list[Claim]:
        """A party's claims, newest first."""
        own = [c for c in self.claims if c.party_id == party_id]
        return sorted(own, key=lambda c: c.created_at, reverse=True)

    def claim(self, case_id: str) -> Claim | None:
        return next((c for c in self.claims if c.case_id.upper() == case_id.upper()), None)

    def representatives_for(self, party_id: str) -> list[Representative]:
        return [r for r in self.representatives if r.buyer_party_id == party_id]
