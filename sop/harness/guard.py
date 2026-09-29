"""Output guard: last-line checks on a reply before it reaches the caller.

Context scoping is the main defence (the model is never given data it must not reveal).
This guard is a backstop that catches identifiers and amounts a reply is not allowed to contain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")


def money_values(text: str) -> set[str]:
    values = set()
    for raw in MONEY.findall(text):
        try:
            values.add(f"{float(raw.replace(',', '')):.2f}")
        except ValueError:
            continue
    return values


@dataclass
class GuardPolicy:
    id_pattern: re.Pattern[str] | None = None
    allowed_ids: set[str] | None = None  # None: any identifier allowed
    allowed_money: set[str] | None = None  # None: any amount allowed
    forbidden_phrases: set[str] = field(default_factory=set)


def check_reply(text: str, policy: GuardPolicy) -> list[str]:
    violations = []
    if policy.id_pattern is not None and policy.allowed_ids is not None:
        for found in sorted({m.upper() for m in policy.id_pattern.findall(text)}):
            if found not in policy.allowed_ids:
                violations.append(f"mentions identifier {found}")
    if policy.allowed_money is not None:
        for amount in sorted(money_values(text) - policy.allowed_money):
            violations.append(f"mentions amount ${amount}, which is not in the facts")
    lowered = text.casefold()
    for phrase in sorted(policy.forbidden_phrases):
        if phrase.casefold() in lowered:
            violations.append(f"mentions '{phrase}'")
    return violations
