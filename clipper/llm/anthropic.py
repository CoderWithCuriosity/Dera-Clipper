"""Adapter for the Anthropic Messages API."""
from __future__ import annotations

import httpx

from . import LLMError, LLMProvider, LLMRateLimited, LLMResult

API_VERSION = "2023-06-01"
_RATE_HEADERS = (
    "anthropic-ratelimit-requests-remaining", "anthropic-ratelimit-requests-reset",
    "anthropic-ratelimit-tokens-remaining", "anthropic-ratelimit-tokens-reset", "retry-after",
)


class AnthropicProvider(LLMProvider):
    def __init__(self, name: str, base_url: str, model: str, api_key: str | None, timeout: float = 180.0):
        super().__init__(name, model)
        if not api_key:
            raise LLMError("The Anthropic API key is missing")
        self.base_url = (base_url or "https://api.anthropic.com/v1").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def complete(self, system: str, user: str, max_tokens: int = 3000) -> LLMResult:
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        try:
            resp = httpx.post(f"{self.base_url}/messages", json=body, headers=headers, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise LLMError(f"Network error: {exc.__class__.__name__}") from exc

        rate = {k: resp.headers[k] for k in _RATE_HEADERS if k in resp.headers}
        if resp.status_code == 429:
            raise LLMRateLimited("Rate limit reached (HTTP 429)")
        if resp.status_code in (401, 403):
            raise LLMError(f"The API key was rejected (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
            text = "".join(block.get("text", "") for block in data["content"] if block.get("type") == "text")
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMError("The provider returned an unexpected response shape") from exc
        usage = data.get("usage") or {}
        tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        return LLMResult(content=text, tokens=tokens, rate_limit=rate)
