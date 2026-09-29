"""The tool gateway: the only path from the engine to data and side effects.

Every call is checked against the current phase's allowlist and the tool's own precondition
(for example: the claim must be on the verified caller's account). Denied calls are recorded
and never executed. Identity always comes from the session, never from model output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .events import Auditable, emit
from .spec import PhaseSpec


class ToolDenied(Exception):
    def __init__(self, tool: str, reason: str):
        super().__init__(f"{tool}: {reason}")
        self.tool = tool
        self.reason = reason


@dataclass
class Tool:
    name: str
    run: Callable[..., Any]
    # Returns a denial reason, or None to allow.
    precondition: Callable[..., str | None] | None = None
    description: str = ""


class ToolGateway:
    def __init__(self, specs: dict[str, PhaseSpec]):
        self.specs = specs
        self.tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def call(self, name: str, session: Auditable, **kwargs: Any) -> Any:
        tool = self.tools.get(name)
        if tool is None:
            reason = "unknown tool"
        elif name not in self.specs[session.phase].tools:
            reason = f"not allowed in {session.phase}"
        else:
            reason = tool.precondition(session, **kwargs) if tool.precondition else None
        if reason:
            emit(session, "tool_denied", f"{name} denied: {reason}", tool=name)
            raise ToolDenied(name, reason)
        emit(session, "tool_call", f"{name} allowed", tool=name)
        return tool.run(session, **kwargs)
