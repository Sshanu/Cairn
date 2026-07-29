"""OpenAI-compatible chat completions over plain HTTP.

One backend covers every provider that speaks the OpenAI `/chat/completions`
shape -- OpenAI itself, Ollama (local, no key), Together, Groq, OpenRouter, and
most others -- by making the base URL and key configurable. No SDK dependency:
it is a single POST, so adding a provider is a settings change, not a new import.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .base import BackendError, BaseBackend, parse_json_object

_JSON_INSTRUCTION = "Reply with a single JSON object and nothing else. No prose, no code fence."


class OpenAICompatBackend(BaseBackend):
    name = "openai-compat"

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str | None = None,
        attempts: int = 3,
        **options: Any,
    ) -> None:
        super().__init__(model, **options)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.attempts = attempts
        # Some servers (older Ollama) reject response_format; flip it off for the
        # process on the first rejection rather than per request.
        self._structured_output = True

    def invoke_json(
        self,
        prompt: str,
        *,
        schema: dict | None = None,
        system: str | None = None,
        max_tokens: int = 2048,
        timeout: int | None = None,
    ) -> dict:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": f"{prompt}\n\n{_JSON_INSTRUCTION}"})

        last: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            body: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0,
            }
            if schema and self._structured_output:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "output", "schema": schema, "strict": True},
                }
            try:
                text, usage = self._post(body, timeout)
                self.usage["input_tokens"] += int(usage.get("prompt_tokens") or 0)
                self.usage["output_tokens"] += int(usage.get("completion_tokens") or 0)
                return parse_json_object(text)
            except _StructuredRejected:
                self._structured_output = False  # retry without response_format
                continue
            except (urllib.error.URLError, OSError, ValueError, BackendError) as exc:
                last = exc
        raise BackendError(f"{self.base_url} failed after {self.attempts} tries: {last}")

    def _post(self, body: dict, timeout: int | None) -> tuple[str, dict]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or 120) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            if exc.code == 400 and "response_format" in detail:
                raise _StructuredRejected() from exc
            raise BackendError(f"HTTP {exc.code}: {detail}") from exc
        choices = payload.get("choices") or []
        if not choices:
            raise BackendError("no choices in response")
        content = choices[0].get("message", {}).get("content") or ""
        return content, payload.get("usage") or {}


class _StructuredRejected(Exception):
    """The server does not accept response_format; retry with a plain instruction."""
