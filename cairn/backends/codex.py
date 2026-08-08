"""codex exec backend. The batch path.

Latency is seconds to tens of seconds, so this is for nightly `tt enrich`, the
organization pass and the ask box -- never the save hotkey.

Three bugs from the evaluator's original CodexExecBackend are fixed here:

1. `_extract_usage` used to fall back to `last_token_usage` when
   `total_token_usage` was missing, recording one turn's usage as the total and
   under-reporting cost. The two are now kept in separate fields.
2. The argument list is built conditionally instead of splicing
   `command[command.index('--output-last-message'):...]` after the fact.
3. `_detect_output_schema_support` shells out to `--help` once per class, not
   once per instance.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from .base import BackendError, BaseBackend, parse_json_object

# CAIRN_MODEL values meaning "let codex use whatever it is configured with".
# Needed because `codex exec -m <id>` is rejected outright on a ChatGPT-account
# login, which is how most people are signed in.
DEFAULT_MODEL_SENTINELS = frozenset({"default", "codex-default", "codex"})


def _strict_schema(schema: dict) -> dict:
    """Make a JSON Schema valid for OpenAI structured output (what codex-cli 0.143+
    enforces): every object must set additionalProperties:false and list ALL of its
    properties in `required`. Applied recursively on a copy, so the caller's schema is
    untouched. Without this, codex rejects the call with 'invalid_json_schema'."""
    import copy

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and isinstance(node.get("properties"), dict):
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    clone = copy.deepcopy(schema)
    walk(clone)
    return clone


class CodexExecBackend(BaseBackend):
    name = "codex"

    # Fix 3: probe the CLI once per process, not once per backend instance.
    _schema_support: ClassVar[bool | None] = None

    def __init__(
        self,
        model: str,
        *,
        executable: str = "codex",
        attempts: int = 3,
        timeout: int = 600,
        reasoning_effort: str | None = None,
        **options: Any,
    ) -> None:
        super().__init__(model, **options)
        self.executable = executable
        self.attempts = attempts
        self.timeout = timeout
        self.reasoning_effort = reasoning_effort

    # -- capability probe -----------------------------------------------------

    @classmethod
    def supports_output_schema(cls, executable: str = "codex") -> bool:
        if cls._schema_support is None:
            try:
                result = subprocess.run(
                    [executable, "exec", "--help"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                cls._schema_support = "--output-schema" in (result.stdout + result.stderr)
            except (OSError, subprocess.SubprocessError):
                cls._schema_support = False
        return cls._schema_support

    # -- main entry point -----------------------------------------------------

    # -- connectivity ---------------------------------------------------------

    OFFLINE_MARKERS = (
        "dns error", "failed to lookup address", "network is unreachable",
        "connection refused", "connection reset", "temporary failure in name resolution",
        "no route to host", "error sending request", "os error 50", "os error 51",
        "timed out", "tls connect error", "connect error",
    )

    @staticmethod
    def online(host: str = "api.openai.com", port: int = 443, timeout: float = 4.0) -> bool:
        try:
            socket.create_connection((host, port), timeout=timeout).close()
            return True
        except OSError:
            return False

    def wait_for_network(self, *, max_wait: int = 86_400, progress=None) -> bool:
        """Block until the network comes back, with capped exponential backoff.

        A long organise run should survive a dropped wifi connection rather than
        failing 300 items in and losing the work already paid for.
        """
        waited, delay = 0, 5
        while waited < max_wait:
            if self.online():
                if progress:
                    progress("network is back, resuming")
                return True
            if progress:
                progress(f"offline, retrying in {delay}s")
            time.sleep(delay)
            waited += delay
            delay = min(delay * 2, 120)
        return False

    def _invoke_json(
        self,
        prompt: str,
        *,
        schema: dict | None = None,
        system: str | None = None,
        max_tokens: int = 2048,
        timeout: int | None = None,
    ) -> dict:
        if shutil.which(self.executable) is None:
            raise BackendError(f"{self.executable!r} is not on PATH")

        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        last_error: Exception | None = None

        for attempt in range(1, self.attempts + 1):
            try:
                text = self._run(
                    self._sharpen(full_prompt, attempt, schema),
                    schema=schema,
                    timeout=timeout or self.timeout,
                )
            except BackendError as exc:
                # A network failure is not a bad answer: wait it out and retry
                # this same item rather than burning an attempt on it.
                if self._looks_offline(str(exc)) and not self.online():
                    self.wait_for_network()
                    continue
                raise
            try:
                return parse_json_object(text)
            except BackendError as exc:
                last_error = exc

        raise BackendError(f"codex backend failed after {self.attempts} attempts: {last_error}")

    def _sharpen(self, prompt: str, attempt: int, schema: dict | None) -> str:
        """Retry ladder: each attempt states the JSON requirement more strictly."""
        if attempt == 1:
            return prompt
        instruction = (
            "Respond with a single JSON object and nothing else. "
            "No prose, no explanation, no markdown code fence."
        )
        if attempt >= 3 and schema:
            instruction += f"\nIt must validate against this JSON Schema:\n{json.dumps(schema)}"
        return f"{prompt}\n\n{instruction}"

    def _run(self, prompt: str, *, schema: dict | None, timeout: int) -> str:
        with tempfile.TemporaryDirectory(prefix="cairn-codex-") as tmp:
            message_path = Path(tmp) / "last-message.txt"

            # Fix 2: build the argument list conditionally. No index splicing, no
            # bracket indented into an `if` body.
            command = [self.executable, "exec", "--json", "--skip-git-repo-check"]
            if self.model and self.model not in DEFAULT_MODEL_SENTINELS:
                # A ChatGPT-account login rejects explicit model ids; the
                # sentinel lets codex pick from its own config instead.
                command += ["--model", self.model]
            if self.reasoning_effort:
                # Overrides model_reasoning_effort from ~/.codex/config.toml for
                # this call only, so the Ask screen can think harder than a
                # bulk-tagging batch needs to.
                command += ["-c", f'model_reasoning_effort="{self.reasoning_effort}"']
            command += ["--output-last-message", str(message_path)]
            if schema and self.supports_output_schema(self.executable):
                schema_path = Path(tmp) / "schema.json"
                schema_path.write_text(json.dumps(_strict_schema(schema)), encoding="utf-8")
                command += ["--output-schema", str(schema_path)]
            command.append(prompt)

            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    # codex exec appends piped stdin to the prompt as a <stdin>
                    # block. Inheriting the caller's stdin makes it swallow
                    # whatever the CLI was reading from and exit 1.
                    stdin=subprocess.DEVNULL,
                    env={**os.environ, "RUST_LOG": "error"},
                )
            except subprocess.TimeoutExpired as exc:
                raise BackendError(f"codex exec timed out after {timeout}s") from exc

            self._extract_usage(result.stdout)

            if message_path.exists():
                text = message_path.read_text(encoding="utf-8").strip()
                if text:
                    return text

            message = self._last_agent_message(result.stdout)
            if message:
                return message

            raise BackendError(
                f"codex exec produced no message "
                f"(exit {result.returncode}): {self._error_detail(result)}"
            )

    @classmethod
    def _looks_offline(cls, message: str) -> bool:
        low = (message or "").lower()
        return any(marker in low for marker in cls.OFFLINE_MARKERS)

    @staticmethod
    def _error_detail(result: subprocess.CompletedProcess) -> str:
        """Surface the real failure, not the first line of stderr noise.

        codex prints 'Reading additional input from stdin...' and model-metadata
        warnings ahead of the actual error, so an unfiltered head of stderr
        reports the wrong cause.
        """
        streams = f"{result.stderr or ''}\n{result.stdout or ''}"
        errors = [
            line.strip()
            for line in streams.splitlines()
            if line.strip().startswith("ERROR") or '"error"' in line
        ]
        if errors:
            return errors[-1][:400]
        noise = ("Reading additional input", "warning:", "--------")
        lines = [
            line.strip()
            for line in (result.stderr or "").splitlines()
            if line.strip() and not any(line.strip().startswith(n) for n in noise)
        ]
        return (lines[-1] if lines else "no output")[:400]

    # -- output parsing -------------------------------------------------------

    @staticmethod
    def _events(stdout: str):
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

    def _last_agent_message(self, stdout: str) -> str:
        """Fallback when --output-last-message produced nothing.

        Handles both event schemas: the older flat `agent_message` and the
        `item.completed` envelope emitted by codex-cli 0.14x.
        """
        message = ""
        for event in self._events(stdout):
            payload = event.get("msg") if isinstance(event.get("msg"), dict) else event
            if payload.get("type") in ("agent_message", "agent-message"):
                message = payload.get("message") or payload.get("text") or message
            elif payload.get("type") == "item.completed":
                item = payload.get("item") or {}
                if item.get("type") in ("agent_message", "assistant_message"):
                    message = item.get("text") or item.get("message") or message
        return message

    def _extract_usage(self, stdout: str) -> None:
        """Fix 1: total and per-turn usage are recorded separately.

        `last_token_usage` describes one turn. Treating it as the total, as the
        original did whenever `total_token_usage` was absent, under-reports cost
        on every multi-turn run.
        """
        total: dict | None = None
        last: dict | None = None
        for event in self._events(stdout):
            payload = event.get("msg") if isinstance(event.get("msg"), dict) else event
            info = payload.get("info") or payload
            if isinstance(info.get("total_token_usage"), dict):
                total = info["total_token_usage"]
            if isinstance(info.get("last_token_usage"), dict):
                last = info["last_token_usage"]
            # codex-cli 0.14x reports per-turn usage on turn.completed and has
            # no cumulative field, so each turn is added as it arrives.
            if payload.get("type") == "turn.completed" and isinstance(
                payload.get("usage"), dict
            ):
                turn = payload["usage"]
                last = turn
                self._add_usage(
                    turn.get("input_tokens") or 0, turn.get("output_tokens") or 0
                )

        if last:
            self.last_turn_usage = {
                "input_tokens": int(last.get("input_tokens") or 0),
                "output_tokens": int(last.get("output_tokens") or 0),
            }
        if total:
            self._add_usage(
                total.get("input_tokens") or 0, total.get("output_tokens") or 0
            )
        # No total reported: leave self.usage untouched rather than substituting
        # a single turn's numbers for the run total.
