"""Adapter for any OpenAI-compatible /chat/completions endpoint. This covers Groq, Gemini
(through Google's OpenAI-compatible route), OpenAI, DeepSeek, OpenRouter, Ollama, and
custom endpoints."""
from __future__ import annotations

import httpx

from . import LLMError, LLMProvider, LLMRateLimited, LLMResult

_RATE_HEADERS = (
    "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests", "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens",
    "retry-after",
)


class OpenAICompatible(LLMProvider):
    def __init__(self, name: str, base_url: str, model: str, api_key: str | None = None,
                 timeout: float = 180.0, json_mode: bool = True):
        super().__init__(name, model)
        if not base_url:
            raise LLMError("No base URL is set for this provider")
        if not model:
            raise LLMError("No model name is set for this provider")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.json_mode = json_mode

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def complete(self, system: str, user: str, max_tokens: int = 3000) -> LLMResult:
        body: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        if "gpt-oss" in self.model:
            # Keep reasoning short, so most of the output budget is left for the JSON answer.
            body["reasoning_effort"] = "low"
        url = f"{self.base_url}/chat/completions"
        try:
            resp = httpx.post(url, json=body, headers=self._headers(), timeout=self.timeout)
            if resp.status_code == 400 and self.json_mode:
                # Some endpoints reject response_format. Retry once without it.
                body.pop("response_format", None)
                resp = httpx.post(url, json=body, headers=self._headers(), timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise LLMError(f"Network error: {exc.__class__.__name__}") from exc

        rate = {k: resp.headers[k] for k in _RATE_HEADERS if k in resp.headers}
        if resp.status_code == 429:
            raise LLMRateLimited(f"Rate limit reached (HTTP 429)")
        if resp.status_code in (401, 403):
            raise LLMError("The API key was rejected (HTTP %d)" % resp.status_code)
        if resp.status_code >= 400:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("The provider returned an unexpected response shape") from exc
        usage = data.get("usage") or {}
        tokens = int(usage.get("total_tokens") or 0)
        return LLMResult(content=content, tokens=tokens, rate_limit=rate)
