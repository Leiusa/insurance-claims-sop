"""Audit events: an append-only record of what the harness decided and why.

Events never contain raw PII values; the operator panel renders them as-is. Free text that
comes from the caller or from a provider error goes through redact() first.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from pydantic import BaseModel

_SECRET = re.compile(r"sk-[A-Za-z0-9_*-]{6,}")
_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
_PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")


def redact(text: str) -> str:
    """Mask API-key-like tokens, email addresses and phone-like numbers in free text."""
    text = _SECRET.sub("sk-…", text)
    text = _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
    return _PHONE.sub(lambda m: "•••" + re.sub(r"\D", "", m.group())[-2:], text)


class Event(BaseModel):
    turn: int
    phase: str
    kind: str
    detail: str
    data: dict[str, Any] = {}


class Auditable(Protocol):
    turn: int
    phase: str
    events: list[Event]


def emit(session: Auditable, kind: str, detail: str, **data: Any) -> Event:
    event = Event(turn=session.turn, phase=session.phase, kind=kind, detail=detail, data=data)
    session.events.append(event)
    return event
