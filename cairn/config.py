"""Environment configuration. Every knob from doc.md section 10 lives here."""

from __future__ import annotations

import json
import os
from pathlib import Path


class ConfigError(RuntimeError):
    pass


def db_path() -> Path:
    raw = os.environ.get("CAIRN_DB", "~/.cairn/cairn.db")
    return Path(raw).expanduser()


def config_path() -> Path:
    return db_path().parent / "config.json"


def load() -> dict:
    """Settings on disk. The environment always wins over the file.

    A settings file matters because the UI is a long-lived server: launching it
    from a shell that happened not to export CAIRN_BACKEND is not a reason
    for the Ask screen to be broken.
    """
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save(**values) -> dict:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load()
    current.update({k: v for k, v in values.items() if v is not None})
    path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    return current


def _setting(env: str, key: str, default: str | None = None) -> str | None:
    value = os.environ.get(env, "").strip()
    if value:
        return value
    stored = load().get(key)
    if stored:
        return str(stored)
    return default


def model() -> str:
    """Required, so it cannot silently rot -- but settable once, not per shell."""
    value = _setting("CAIRN_MODEL", "model")
    if not value:
        raise ConfigError(
            "No model configured. Set it once with:\n"
            "  tt config --backend codex --model default\n"
            "or export CAIRN_MODEL for this shell only."
        )
    return value


def backend_name() -> str:
    return (_setting("CAIRN_BACKEND", "backend", "direct") or "direct").lower()


def agent_api_key() -> str | None:
    """API key for the chosen provider (OpenAI/Claude). Ollama and codex need none."""
    return _setting("CAIRN_API_KEY", "api_key")


def agent_base_url() -> str | None:
    """Override the provider endpoint -- e.g. a local Ollama or an OpenAI proxy."""
    return _setting("CAIRN_AGENT_BASE_URL", "agent_base_url")


def codex_path() -> str | None:
    """Full path to the `codex` binary, for when it isn't on the default PATH (a
    non-standard install, nvm/volta/npm-global, etc.). Set with CAIRN_CODEX_PATH or
    `tt config --codex-path /full/path/to/codex`. Empty -> found on PATH by name."""
    return _setting("CAIRN_CODEX_PATH", "codex_path")


def reasoning_effort() -> str | None:
    """Passed through to codex as -c model_reasoning_effort=<value>."""
    return _setting("CAIRN_REASONING", "reasoning_effort")


def ask_model() -> str:
    """The Ask screen may use a different model than batch organising."""
    return _setting("CAIRN_ASK_MODEL", "ask_model") or model()


def ask_reasoning_effort() -> str | None:
    return _setting("CAIRN_ASK_REASONING", "ask_reasoning_effort") or reasoning_effort()


def ledger_enabled() -> bool:
    return os.environ.get("CAIRN_LEDGER", "1").strip() not in ("0", "false", "no", "")


def ledger_days() -> int:
    try:
        return int(os.environ.get("CAIRN_LEDGER_DAYS", "90"))
    except ValueError:
        return 90


def _int_setting(env: str, key: str, default: int) -> int:
    value = os.environ.get(env, "").strip()
    if value.isdigit():
        return int(value)
    stored = load().get(key)
    if isinstance(stored, bool):
        return default
    if isinstance(stored, (int, float)):
        return int(stored)
    if isinstance(stored, str) and stored.strip().isdigit():
        return int(stored)
    return default


def ingest_interval_min() -> int:
    """How often the backend files open tabs into the library (autosave)."""
    return max(5, _int_setting("CAIRN_INGEST_MIN", "ingest_interval_min", 60))


def poll_interval_min() -> int:
    """How often the ledger tick records that a tab exists (drives first_seen)."""
    return max(1, _int_setting("CAIRN_POLL_MIN", "poll_interval_min", 5))


def backup_interval_hours() -> int:
    """How often to back up automatically. 0 disables the scheduled backup."""
    return max(0, _int_setting("CAIRN_BACKUP_HOURS", "backup_interval_hours", 24))


def backup_destination() -> str:
    return (_setting("CAIRN_BACKUP_DEST", "backup_destination", "icloud") or "icloud").lower()


def backup_folder() -> str | None:
    return _setting("CAIRN_BACKUP_FOLDER", "backup_folder")


def github_repo() -> str | None:
    """A local git repo path to commit backups into and push."""
    return _setting("CAIRN_GITHUB_REPO", "github_repo")


def _bool_setting(env: str, key: str, default: bool) -> bool:
    value = os.environ.get(env, "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    stored = load().get(key)
    if isinstance(stored, bool):
        return stored
    return default


# --- feature toggles: every agent-backed nicety is opt-in ------------------


def auto_organize() -> bool:
    """Tag new items (topic/method/task) with the agent as they are ingested."""
    return _bool_setting("CAIRN_AUTO_ORGANIZE", "auto_organize", True)


def auto_file_projects() -> bool:
    """Also let the agent file new items into your described custom branches."""
    return _bool_setting("CAIRN_AUTO_FILE_PROJECTS", "auto_file_projects", False)


def auto_summary() -> bool:
    """Generate a TL;DR and a 'why you saved this' line at ingest."""
    return _bool_setting("CAIRN_AUTO_SUMMARY", "auto_summary", False)


def related_citations() -> bool:
    """Fetch reference IDs so 'related in your library' can be computed."""
    return _bool_setting("CAIRN_RELATED_CITATIONS", "related_citations", False)


def weekly_digest() -> bool:
    return _bool_setting("CAIRN_WEEKLY_DIGEST", "weekly_digest", True)


def tracking_paused() -> bool:
    """When paused, the poller and the hourly ingest do not run at all."""
    return _bool_setting("CAIRN_PAUSED", "tracking_paused", False)


# The built-in "house style": the standing rules that apply to all three
# organization agents. Shown (and editable, and resettable) in Settings so
# nothing is hidden, and appended to every prompt via organize._augment_system.
DEFAULT_GUIDANCE = """\
Goal: organize items so you can find them again and do better research — browse \
straight to the right cluster instead of searching blindly.

- Tag by SUBJECT, not tools. A paper that USES a vision-language model to study \
hallucination is about hallucination, not "vision-language-models".
- Few and precise beats many and loose — add a tag only if you would truly browse to it.
- Every tag must hold at least 3 items; no one-off buckets.
- Keep breadth tight: 10–15 top-level areas at most, then go DEEP, not wide.
- Group by the research PROBLEM, never by modality. text / image / video is a \
trailing leaf at most, never a top-level split.
- Grow the tree to fit an item precisely: invent a new deep path when nothing fits, \
rather than forcing an item into a loose existing tag.
- Not every item is a paper — blog posts, code repos, datasets, tools and docs \
belong too. Tag them by subject the same way."""


def organization_guidance() -> str:
    """The standing rules injected into all three organization prompts.

    Defaults to the built-in house style (DEFAULT_GUIDANCE) so the rules always
    apply and are visible in Settings; the user can edit or extend them.
    """
    return _setting("CAIRN_ORG_GUIDANCE", "organization_guidance", DEFAULT_GUIDANCE) or DEFAULT_GUIDANCE


def classify_prompt() -> str | None:
    """User override for the item-tagging instructions the agent follows when it
    assigns topic/ and contribution/ tags. None = use the built-in default.
    Editable in Settings and resettable to the default."""
    return _setting("CAIRN_CLASSIFY_PROMPT", "classify_prompt")


# The three-agent organization pipeline. Each prompt is user-overridable and
# editable in Settings; None means use the built-in default.
def tag_proposal_prompt() -> str | None:
    """Agent 1: proposes the flat pool of candidate concepts from batches of titles."""
    return _setting("CAIRN_PROPOSE_PROMPT", "tag_proposal_prompt")


def hierarchy_prompt() -> str | None:
    """Agent 2: turns the pool into a hierarchy, one facet at a time."""
    return _setting("CAIRN_HIERARCHY_PROMPT", "hierarchy_prompt")


def tagging_prompt() -> str | None:
    """Agent 3: tags each item into the vocabulary (same key as classify_prompt)."""
    return _setting("CAIRN_CLASSIFY_PROMPT", "classify_prompt")


# The facet hierarchies built by default: just one clean `topic` tree. The user
# can add method / task / any custom facet in Settings. `type`, `venue` and
# `site` are computed from the URL and are never facets.
DEFAULT_FACETS = ("topic",)


def facets() -> tuple[str, ...]:
    """Which facet hierarchies the agent builds and tags into. Defaults to just
    'topic'; the user can add method, task or any custom facet in Settings. From
    the CAIRN_FACETS env (comma-separated) or the 'facets' settings list."""
    env = os.environ.get("CAIRN_FACETS", "").strip()
    if env:
        return tuple(f.strip().rstrip("/") for f in env.split(",") if f.strip())
    stored = load().get("facets")
    if isinstance(stored, list) and stored:
        return tuple(str(f).strip().rstrip("/") for f in stored if str(f).strip())
    return DEFAULT_FACETS


def blocklist() -> tuple[str, ...]:
    """Domains never to track -- from the environment and the settings file both."""
    raw = os.environ.get("CAIRN_BLOCKLIST", "")
    env_hosts = [h.strip().lower() for h in raw.split(",") if h.strip()]
    stored = load().get("blocklist")
    cfg_hosts = (
        [str(h).strip().lower() for h in stored if str(h).strip()]
        if isinstance(stored, list)
        else []
    )
    return tuple(dict.fromkeys(env_hosts + cfg_hosts))


def is_blocked(url: str) -> bool:
    """Host-suffix match against CAIRN_BLOCKLIST."""
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    return any(host == entry or host.endswith("." + entry) for entry in blocklist())
