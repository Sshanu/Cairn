"""Backend contract.

`invoke_json` is the whole interface: prompt in, validated dict out. Both the
direct HTTP backend and the codex-exec backend implement it, so enrich.py never
learns which one it is talking to.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any


class BackendError(RuntimeError):
    pass


class BaseBackend(ABC):
    name = "base"

    def __init__(self, model: str, **options: Any) -> None:
        self.model = model
        self.options = options
        self.usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self.last_turn_usage: dict[str, int] = {}

    @abstractmethod
    def invoke_json(
        self,
        prompt: str,
        *,
        schema: dict | None = None,
        system: str | None = None,
        max_tokens: int = 2048,
        timeout: int | None = None,
    ) -> dict:
        """Run the prompt and return a parsed JSON object."""

    def close(self) -> None:  # pragma: no cover - most backends are stateless
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- shared helpers ------------------------------------------------------

    def _add_usage(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.usage["input_tokens"] += int(input_tokens or 0)
        self.usage["output_tokens"] += int(output_tokens or 0)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_object(text: str) -> dict:
    """Pull a JSON object out of model text, tolerating fences and preamble."""
    text = (text or "").strip()
    if not text:
        raise BackendError("empty response")

    for candidate in _candidates(text):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise BackendError(f"no JSON object in response: {text[:200]!r}")


def _candidates(text: str):
    yield text
    fenced = _FENCE.search(text)
    if fenced:
        yield fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        yield text[start : end + 1]


def get_backend(
    name: str | None = None,
    model: str | None = None,
    *,
    purpose: str = "batch",
) -> BaseBackend:
    """Resolve the configured backend. `purpose='ask'` uses the Ask settings."""
    from .. import config

    name = (name or config.backend_name()).lower()
    if purpose == "ask":
        model = model or config.ask_model()
        effort = config.ask_reasoning_effort()
    else:
        model = model or config.model()
        effort = config.reasoning_effort()

    if name == "none":
        raise BackendError(
            "No agent is configured. Pick a provider in Settings (or set "
            "CAIRN_BACKEND) to use organising, Ask and summaries."
        )
    if name in ("direct", "claude", "anthropic"):
        from .direct import DirectBackend

        return DirectBackend(model)
    if name == "codex":
        from .codex import CodexExecBackend

        return CodexExecBackend(model, reasoning_effort=effort)
    if name in ("openai", "ollama", "openai-compat"):
        from .openai_compat import OpenAICompatBackend

        base_url = config.agent_base_url() or (
            "http://localhost:11434/v1" if name == "ollama" else "https://api.openai.com/v1"
        )
        # Ollama is local and needs no key; the others take one from settings/env.
        key = config.agent_api_key() if name != "ollama" else config.agent_api_key()
        return OpenAICompatBackend(model, base_url=base_url, api_key=key)
    raise BackendError(
        f"unknown backend {name!r} (expected codex, claude, openai, ollama or none)"
    )
