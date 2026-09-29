"""Provider adapters. The rest of the code sees exactly two calls:

- structured(): output that is guaranteed to match a JSON schema (used for understanding)
- text():       a natural-language reply (used for responding)
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import Settings

T = TypeVar("T", bound=BaseModel)
log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """The model call failed or returned something unusable."""


class LLMClient(Protocol):
    provider: str
    model: str
    nlu_model: str

    def structured(self, *, system: str, user: str, schema: type[T], name: str) -> T: ...

    def text(self, *, system: str, messages: list[dict[str, str]]) -> str: ...


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema -> strict schema both providers accept.

    $refs are inlined, and every object is closed (additionalProperties: false) with all of
    its properties required; optional values are expressed as nullable types instead.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(copy.deepcopy(defs[node["$ref"].rsplit("/", 1)[-1]]))
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("title", "default"):
                continue
            out[key] = {k: walk(v) for k, v in value.items()} if key == "properties" else walk(value)
        if out.get("type") == "object":
            out["additionalProperties"] = False
            out["required"] = list(out.get("properties", {}))
        return out

    return walk(schema)


class OpenAIClient:
    provider = "openai"

    def __init__(self, *, api_key: str, model: str, nlu_model: str, base_url: str | None, effort: str | None):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=60, max_retries=2)
        self.model = model
        self.nlu_model = nlu_model
        self.effort = effort

    def _options(self, model: str) -> dict[str, Any]:
        reasoning = model.startswith(("gpt-5", "o1", "o3", "o4")) and "chat" not in model
        return {"reasoning_effort": self.effort} if (reasoning and self.effort) else {}

    def structured(self, *, system: str, user: str, schema: type[T], name: str) -> T:
        import openai

        try:
            response = self.client.chat.completions.create(
                model=self.nlu_model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": name, "schema": strict_json_schema(schema), "strict": True},
                },
                **self._options(self.nlu_model),
            )
        except openai.OpenAIError as exc:
            raise LLMError(f"OpenAI request failed: {exc}") from exc
        message = response.choices[0].message
        if getattr(message, "refusal", None):
            raise LLMError("model refused the request")
        try:
            return schema.model_validate_json(message.content or "")
        except ValidationError as exc:
            raise LLMError(f"structured output did not validate: {exc}") from exc

    def text(self, *, system: str, messages: list[dict[str, str]]) -> str:
        import openai

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, *messages],
                **self._options(self.model),
            )
        except openai.OpenAIError as exc:
            raise LLMError(f"OpenAI request failed: {exc}") from exc
        content = (response.choices[0].message.content or "").strip()
        if not content:
            raise LLMError("empty reply")
        return content


class AnthropicClient:
    provider = "anthropic"
    # Server-side refusal fallback, recommended for these models (re-runs a declined request).
    FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5-1")
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, *, api_key: str, model: str, nlu_model: str, base_url: str | None, effort: str | None):
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key, base_url=base_url, timeout=60, max_retries=2)
        self.model = model
        self.nlu_model = nlu_model
        self.effort = effort

    def _create(self, *, model: str, system: str, messages: list[dict[str, str]], output_config: dict[str, Any]) -> str:
        import anthropic

        if self.effort and not model.startswith("claude-haiku"):
            output_config = {**output_config, "effort": self.effort}
        kwargs: dict[str, Any] = {"model": model, "max_tokens": 16000, "system": system, "messages": messages}
        if output_config:
            kwargs["output_config"] = output_config
        try:
            if model in self.FALLBACK_MODELS:
                try:
                    response = self.client.beta.messages.create(
                        betas=[self.FALLBACK_BETA], fallbacks="default", **kwargs
                    )
                except anthropic.BadRequestError as exc:
                    log.warning("Fallback-enabled request rejected (%s); retrying without it", exc)
                    response = self.client.messages.create(**kwargs)
            else:
                response = self.client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            raise LLMError(f"Anthropic request failed: {exc}") from exc
        if response.stop_reason == "refusal":
            raise LLMError("model declined the request")
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if not text:
            raise LLMError("empty response")
        return text

    def structured(self, *, system: str, user: str, schema: type[T], name: str) -> T:
        output_config = {"format": {"type": "json_schema", "schema": strict_json_schema(schema)}}
        text = self._create(
            model=self.nlu_model, system=system, messages=[{"role": "user", "content": user}], output_config=output_config
        )
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            raise LLMError(f"structured output did not validate: {exc}") from exc

    def text(self, *, system: str, messages: list[dict[str, str]]) -> str:
        # The Messages API requires the conversation to start with a user turn.
        trimmed = list(messages)
        while trimmed and trimmed[0]["role"] != "user":
            trimmed.pop(0)
        return self._create(model=self.model, system=system, messages=trimmed, output_config={})


def build_client(settings: Settings) -> LLMClient | None:
    if not settings.llm_ready or not settings.model:
        return None
    args = dict(
        api_key=settings.api_key,
        model=settings.model,
        nlu_model=settings.nlu_model or settings.model,
        base_url=settings.base_url,
        effort=settings.reasoning_effort,
    )
    if settings.provider == "anthropic":
        return AnthropicClient(**args)
    if settings.provider == "openai":
        return OpenAIClient(**args)
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.provider}")
