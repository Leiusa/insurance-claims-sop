"""Runtime configuration, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MODELS = {"openai": "gpt-5.4-mini", "anthropic": "claude-opus-5"}


def _get(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _int(name: str, default: int) -> int:
    value = _get(name)
    return int(value) if value else default


@dataclass(frozen=True)
class Settings:
    provider: str | None
    api_key: str | None
    model: str | None
    nlu_model: str | None
    base_url: str | None
    reasoning_effort: str | None
    fixtures_dir: Path
    as_of_date: date | None  # None: use the real date at request time
    demo_passcode: str | None
    oos_threshold: int
    max_verification_attempts: int
    max_gate_pushbacks: int
    max_consent_checks: int = 3
    consent_scenario: str = "default"  # which consent_scenarios.json entry new chats simulate

    def today(self) -> date:
        return self.as_of_date or date.today()

    @property
    def llm_ready(self) -> bool:
        return bool(self.provider and self.api_key)


def _detect_provider(generic_key: str | None) -> str | None:
    explicit = (_get("LLM_PROVIDER") or "").lower()
    if explicit:
        return explicit
    if generic_key:
        return "anthropic" if generic_key.startswith("sk-ant-") else "openai"
    if _get("OPENAI_API_KEY"):
        return "openai"
    if _get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")  # never overrides variables that are already set
    generic_key = _get("LLM_API_KEY")
    provider = _detect_provider(generic_key)
    provider_key = {"openai": _get("OPENAI_API_KEY"), "anthropic": _get("ANTHROPIC_API_KEY")}.get(provider or "")
    model = _get("LLM_MODEL") or DEFAULT_MODELS.get(provider or "")
    as_of = _get("AS_OF_DATE")
    fixtures = _get("FIXTURES_DIR")
    return Settings(
        provider=provider,
        api_key=generic_key or provider_key,
        model=model,
        nlu_model=_get("NLU_MODEL") or model,
        base_url=_get("LLM_BASE_URL"),
        reasoning_effort=_get("LLM_REASONING_EFFORT") or "low",
        fixtures_dir=Path(fixtures) if fixtures else ROOT / "fixtures",
        as_of_date=date.fromisoformat(as_of) if as_of else None,
        demo_passcode=_get("DEMO_PASSCODE"),
        oos_threshold=_int("OOS_THRESHOLD", 3),
        max_verification_attempts=_int("MAX_VERIFICATION_ATTEMPTS", 3),
        max_gate_pushbacks=_int("MAX_GATE_PUSHBACKS", 3),
        max_consent_checks=_int("MAX_CONSENT_CHECKS", 3),
        consent_scenario=_get("CONSENT_SCENARIO") or "default",
    )
