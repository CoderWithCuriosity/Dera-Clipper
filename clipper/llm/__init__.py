"""Language-model layer. The model only ever receives text. Providers share one output
contract, checked by the caller's validator. Malformed output is retried once, then the
next provider in priority order is used."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("clipper.llm")


class LLMError(RuntimeError):
    """A provider call failed (network, auth, quota, server). Move to the next provider."""


class LLMRateLimited(LLMError):
    pass


class InvalidOutput(ValueError):
    """The model answered, but not with the JSON contract. Retry once, then move on."""


class AllProvidersFailed(RuntimeError):
    def __init__(self, errors: list[str]):
        super().__init__("All language model providers failed: " + "; ".join(errors))
        self.errors = errors


@dataclass
class LLMResult:
    content: str
    tokens: int = 0
    rate_limit: dict = field(default_factory=dict)


# Presets: a provider is an OpenAI-compatible endpoint plus a model name.
# Free-tier limits are NOT stored here. They change often, so the UI shows the
# docs link and the limits the provider reported on the last response.
PRESETS: dict[str, dict[str, Any]] = {
    "groq": {
        "label": "Groq", "kind": "openai", "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-20b", "key_required": True,
        "docs": "https://console.groq.com/docs/rate-limits",
    },
    "gemini": {
        "label": "Google Gemini", "kind": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash", "key_required": True,
        "docs": "https://ai.google.dev/gemini-api/docs/rate-limits",
    },
    "openai": {
        "label": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini", "key_required": True,
        "docs": "https://platform.openai.com/docs/guides/rate-limits",
    },
    "deepseek": {
        "label": "DeepSeek", "kind": "openai", "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat", "key_required": True,
        "docs": "https://api-docs.deepseek.com/",
    },
    "openrouter": {
        "label": "OpenRouter", "kind": "openai", "base_url": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.3-70b-instruct:free", "key_required": True,
        "docs": "https://openrouter.ai/docs/limits",
    },
    "anthropic": {
        "label": "Anthropic Claude", "kind": "anthropic", "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-5-5", "key_required": True,
        "docs": "https://docs.anthropic.com/en/api/rate-limits",
    },
    "ollama": {
        "label": "Ollama (local)", "kind": "openai", "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b", "key_required": False,
        "docs": "https://github.com/ollama/ollama/blob/main/docs/openai.md",
    },
    "custom": {
        "label": "Any OpenAI-compatible endpoint", "kind": "openai", "base_url": "",
        "model": "", "key_required": False, "docs": "",
    },
}


def extract_json(text: str) -> dict:
    """Parse a JSON object from a model reply. Tolerates code fences and extra prose."""
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body.lower().startswith("json"):
            body = body[4:]
        body = body.strip()
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        start, end = body.find("{"), body.rfind("}")
        if start == -1 or end <= start:
            raise InvalidOutput("The reply contained no JSON object")
        try:
            value = json.loads(body[start:end + 1])
        except json.JSONDecodeError as exc:
            raise InvalidOutput(f"The reply was not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise InvalidOutput("The JSON reply must be an object")
    return value


class LLMProvider:
    """Base class. Subclasses implement complete()."""

    def __init__(self, name: str, model: str):
        self.name = name
        self.model = model

    def complete(self, system: str, user: str, max_tokens: int = 3000) -> LLMResult:
        raise NotImplementedError

    def test(self) -> str:
        # Reasoning models spend output tokens on thinking first, so leave room for the answer.
        result = self.complete("You are a connection test.", "Reply with the JSON object {\"ok\": true}.", 400)
        extract_json(result.content)
        return "Connection OK"


@dataclass
class Attempt:
    provider: str
    error: str


def run_with_fallback(
    providers: list[LLMProvider],
    system: str,
    user: str,
    validate: Callable[[dict], Any],
    on_usage: Optional[Callable[[str, LLMResult], None]] = None,
    on_failure: Optional[Callable[[str, str], None]] = None,
    max_tokens: int = 3000,
) -> tuple[Any, str]:
    """Try providers in priority order. Returns (validated value, provider name)."""
    errors: list[str] = []
    for provider in providers:
        for attempt in (1, 2):  # one retry on malformed output
            try:
                result = provider.complete(system, user, max_tokens=max_tokens)
                if on_usage:
                    on_usage(provider.name, result)
                value = validate(extract_json(result.content))
                return value, provider.name
            except InvalidOutput as exc:
                errors.append(f"{provider.name}: {exc}")
                log.info("Malformed output from %s (attempt %d): %s", provider.name, attempt, exc)
                if attempt == 2 and on_failure:
                    on_failure(provider.name, str(exc))
            except LLMError as exc:
                errors.append(f"{provider.name}: {exc}")
                log.warning("Provider %s failed: %s", provider.name, exc)
                if on_failure:
                    on_failure(provider.name, str(exc))
                break  # move on to the next provider
    raise AllProvidersFailed(errors)
