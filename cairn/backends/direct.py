"""Direct HTTP call with a strict JSON schema. The interactive path.

Used by the save hotkey and single-item tagging, where latency is the point.
Batch work goes through the codex backend instead.
"""

from __future__ import annotations

import time
from typing import Any

from .base import BackendError, BaseBackend, parse_json_object

_JSON_INSTRUCTION = (
    "Reply with a single JSON object and nothing else. No prose, no code fence."
)


class DirectBackend(BaseBackend):
    name = "direct"

    def __init__(self, model: str, *, attempts: int = 3, **options: Any) -> None:
        super().__init__(model, **options)
        self.attempts = attempts
        self._client = None
        # Some model ids do not accept output_config; the first rejection flips
        # this off for the life of the process instead of per request.
        self._structured_output = True

    @property
    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - install-time error
                raise BackendError(
                    "the direct backend needs the Anthropic SDK: "
                    "pip install -e '.[api]'"
                ) from exc
            from .. import config

            key = config.agent_api_key()  # settings key wins over the env var
            self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        return self._client

    def _invoke_json(
        self,
        prompt: str,
        *,
        schema: dict | None = None,
        system: str | None = None,
        max_tokens: int = 2048,
        timeout: int | None = None,
    ) -> dict:
        import anthropic

        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            request: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": self._user_text(prompt, attempt)}],
            }
            if system:
                request["system"] = system
            if schema and self._structured_output:
                request["output_config"] = {
                    "format": {"type": "json_schema", "schema": schema}
                }

            client = self.client
            if timeout is not None:
                client = client.with_options(timeout=float(timeout))

            try:
                response = client.messages.create(**request)
            except anthropic.BadRequestError as exc:
                if self._structured_output and "output_config" in str(exc):
                    # Model does not support structured outputs -- ask for JSON in
                    # the prompt instead and retry immediately.
                    self._structured_output = False
                    continue
                raise BackendError(str(exc)) from exc
            except anthropic.APIError as exc:
                last_error = exc
                if attempt == self.attempts:
                    break
                time.sleep(0.5 * attempt)
                continue

            usage = getattr(response, "usage", None)
            if usage is not None:
                self._add_usage(
                    getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0)
                )

            if getattr(response, "stop_reason", None) == "refusal":
                raise BackendError("model declined the request")

            text = "".join(
                block.text for block in response.content if getattr(block, "type", "") == "text"
            )
            try:
                return parse_json_object(text)
            except BackendError as exc:
                last_error = exc
                if attempt == self.attempts:
                    break

        raise BackendError(f"direct backend failed after {self.attempts} attempts: {last_error}")

    def _user_text(self, prompt: str, attempt: int) -> str:
        if self._structured_output and attempt == 1:
            return prompt
        # Retries and unstructured models get progressively blunter instructions.
        return f"{prompt}\n\n{_JSON_INSTRUCTION}"
