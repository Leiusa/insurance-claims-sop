"""Audit events: an append-only record of what the harness decided and why.

Events never contain raw PII values; the operator panel renders them as-is.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel


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
