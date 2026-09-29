"""Session state for one conversation. Everything the engine knows lives here."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from ..harness.events import Event
from .resolver import CaseHints


class SlotValue(BaseModel):
    value: str  # normalized
    turn: int


class IdentityState(BaseModel):
    provided: dict[str, SlotValue] = {}
    refused: list[str] = []
    unusable: list[str] = []  # factors given this turn in a form we can't use
    failed_attempts: int = 0
    locked: bool = False
    verified_party_id: str | None = None
    verified_via: list[str] = []
    policy_number_hint: str | None = None


class MemoryItem(BaseModel):
    kind: str  # intent | case_hint | doc_status | email_pref | contact_pref | context
    value: str
    turn: int
    phase_said: str
    used_in: list[str] = []


class Counters(BaseModel):
    oos_consecutive: int = 0
    oos_total: int = 0
    gate_pushbacks: int = 0
    email_unclear: int = 0


class EmailState(BaseModel):
    offered: bool = False
    decision: str | None = None  # send | skip | not_sent
    consent_turn: int | None = None
    summary: dict[str, Any] | None = None
    message: dict[str, Any] | None = None  # the outbox record, once sent


class ConsentState(BaseModel):
    """Policyholder authorization for a representative caller (simulated with consent_scenarios.json)."""

    scenario: str = "default"
    status: str | None = None  # pending | approved | timeout | not_on_file
    checks: int = 0
    account_party_id: str | None = None  # account located from the policyholder's details; this is not access
    representative_on_file: bool = False
    representative: str | None = None  # the listed representative this authorization is bound to
    explained: bool = False


class Session(BaseModel):
    id: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    phase: str = "VERIFY_ID"
    phase_history: list[dict[str, Any]] = []
    turn: int = 0
    caller_role: str = "unknown"  # self | representative | unknown
    caller_name: str | None = None  # as the caller gave it; used to address them after verification
    representative_name: str | None = None
    representative_relationship: str | None = None
    identity: IdentityState = Field(default_factory=IdentityState)
    intent: str | None = None
    reason_for_call: str | None = None
    case_hints: CaseHints = Field(default_factory=CaseHints)
    memory: list[MemoryItem] = []
    candidate_case_ids: list[str] = []
    active_case_id: str | None = None
    left_case_id: str | None = None  # the claim the caller just asked to move away from
    discussed: dict[str, list[str]] = {}  # case_id -> topics covered, in order
    deadline_caveat_given: list[str] = []
    counters: Counters = Field(default_factory=Counters)
    sentiment: str = "neutral"
    pending_question: str | None = None
    email: EmailState = Field(default_factory=EmailState)
    handoff: dict[str, Any] | None = None
    consent: ConsentState = Field(default_factory=ConsentState)
    transcript: list[dict[str, str]] = []
    events: list[Event] = []
