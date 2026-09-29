"""Declarative phase specs.

A phase spec states, for one step of an SOP, what the language model may see, which tools may
run, how much freedom the model has, and what must be true to leave the phase. The engine
enforces the spec; an SOP (here: insurance claims) only declares it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Autonomy(str, Enum):
    SCRIPTED = "scripted"  # code decides what to say next; the model only phrases it
    GUIDED = "guided"  # the model picks from a closed set of options that code provides
    OPEN = "open"  # the model reasons freely over a bounded, grounded context
    CONSTRAINED = "constrained"  # the model phrases; side effects need explicit consent


AUTONOMY_NOTES = {
    Autonomy.SCRIPTED: "Code decides what to ask next; the model only phrases it and adds empathy.",
    Autonomy.GUIDED: "The model maps the caller's words onto options that code provides.",
    Autonomy.OPEN: "The model reasons freely, but only over the grounded facts it is given.",
    Autonomy.CONSTRAINED: "The model phrases; any side effect needs the caller's explicit consent.",
}


@dataclass(frozen=True)
class PhaseSpec:
    name: str
    goal: str
    autonomy: Autonomy
    tools: frozenset[str]
    reply_context: tuple[str, ...]  # context providers the reply model may see
    nlu_context: tuple[str, ...]  # context providers the understanding model may see
    exit_guard: str  # enforced in code by the phase handler; stated here for the operator view
    terminal: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "goal": self.goal,
            "autonomy": self.autonomy.value,
            "autonomy_note": AUTONOMY_NOTES[self.autonomy],
            "tools": sorted(self.tools),
            "reply_context": list(self.reply_context),
            "nlu_context": list(self.nlu_context),
            "exit_guard": self.exit_guard,
            "terminal": self.terminal,
        }
