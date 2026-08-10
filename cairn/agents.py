"""launchd agents: keep the UI and the poller running without being asked.

Two jobs, both per-user LaunchAgents so they start at login and are restarted if
they die:

  com.cairn.serve   the local UI, KeepAlive, restarted on crash or reboot
  com.cairn.poll    the ledger tick, every CAIRN_POLL_INTERVAL seconds

The poller is what makes first_seen dates real, and first_seen is what the age
spine and the stale queue are built on -- so it has to run whether or not a
terminal is open.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

LAUNCH_AGENTS = Path.home() / "Library/LaunchAgents"
SERVE_LABEL = "com.cairn.serve"
POLL_LABEL = "com.cairn.poll"
AUTOSAVE_LABEL = "com.cairn.autosave"
BACKUP_LABEL = "com.cairn.backup"


def _tt_path() -> Path:
    """The console script next to the running interpreter."""
    candidate = Path(sys.executable).parent / "tt"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(
        "could not find the `tt` script next to the current interpreter; "
        "install the package with `pip install -e .` first"
    )


def _plist_path(label: str) -> Path:
    return LAUNCH_AGENTS / f"{label}.plist"


def serve_plist(port: int = 8765) -> dict:
    return {
        "Label": SERVE_LABEL,
        "ProgramArguments": [str(_tt_path()), "serve", "--port", str(port)],
        "RunAtLoad": True,
        "KeepAlive": True,  # restart if it crashes or is killed
        "StandardErrorPath": "/tmp/cairn.serve.err",
        "StandardOutPath": "/tmp/cairn.serve.out",
        # This process backs the interactive UI, so it MUST be Interactive.
        # As "Background" macOS throttles its CPU and applies App Nap, which under
        # any system load starved the server -- the SAME request that took ~3ms on
        # a directly-run server took 200-3000ms here. Interactive gives it timeshare
        # priority and exempts it from throttling.
        "ProcessType": "Interactive",
        "EnvironmentVariables": {"PATH": _path_for_launchd()},
    }


def autosave_plist(interval: int = 3600) -> dict:
    """Hourly: pull every open tab into the library, with metadata.

    This is the difference between the ledger and the library. The poller only
    notes that a URL existed; this actually files it, so the library keeps up
    with your browsing without you remembering to run anything.
    """
    return {
        "Label": AUTOSAVE_LABEL,
        "ProgramArguments": [str(_tt_path()), "ingest"],
        "RunAtLoad": False,  # do not fight the login rush
        "StartInterval": interval,
        "StandardErrorPath": "/tmp/cairn.autosave.err",
        "StandardOutPath": "/tmp/cairn.autosave.log",
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 5,
        "EnvironmentVariables": {"PATH": _path_for_launchd()},
    }


def poll_plist(interval: int = 300) -> dict:
    return {
        "Label": POLL_LABEL,
        "ProgramArguments": [str(_tt_path()), "poll"],
        "RunAtLoad": True,
        "StartInterval": interval,
        "StandardErrorPath": "/tmp/cairn.poll.err",
        "StandardOutPath": "/dev/null",
        "ProcessType": "Background",
        "EnvironmentVariables": {"PATH": _path_for_launchd()},
    }


def backup_plist(interval_hours: int = 24) -> dict:
    """Scheduled backup to wherever the settings point (iCloud, a folder, git)."""
    return {
        "Label": BACKUP_LABEL,
        "ProgramArguments": [str(_tt_path()), "backup"],
        "RunAtLoad": False,
        "StartInterval": max(1, interval_hours) * 3600,
        "StandardErrorPath": "/tmp/cairn.backup.err",
        "StandardOutPath": "/tmp/cairn.backup.log",
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 5,
        "EnvironmentVariables": {"PATH": _path_for_launchd()},
    }


def apply_schedule() -> dict[str, str]:
    """Rewrite the timed agents from the current settings and reload them.

    Called after the settings change so a new ingest cadence or backup interval
    takes effect without reinstalling anything by hand. serve is left alone -- it
    has no schedule to change.
    """
    from . import config

    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    paused = config.tracking_paused()
    for label, payload in (
        (POLL_LABEL, poll_plist(config.poll_interval_min() * 60)),
        (AUTOSAVE_LABEL, autosave_plist(config.ingest_interval_min() * 60)),
    ):
        path = _plist_path(label)
        _bootout(label)
        if paused:
            # Tracking is off: unload the timed agents entirely so nothing is
            # recorded until the user turns it back on.
            if path.exists():
                path.unlink()
            continue
        path.write_bytes(plistlib.dumps(payload))
        _bootstrap(path)

    hours = config.backup_interval_hours()
    backup_path = _plist_path(BACKUP_LABEL)
    _bootout(BACKUP_LABEL)
    if hours > 0:
        backup_path.write_bytes(plistlib.dumps(backup_plist(hours)))
        _bootstrap(backup_path)
    elif backup_path.exists():
        backup_path.unlink()  # 0 hours means "no scheduled backup"
    return status()


def _path_for_launchd() -> str:
    """launchd starts with a bare PATH, but codex/node may live anywhere (homebrew,
    npm-global, volta, nvm, ~/.local/bin, a custom dir). Build a PATH that finds them:
    the dir of a user-set codex path first, then common install locations, then the
    PATH of whoever ran `tt autostart` (that shell already found codex to run this),
    then the system defaults. De-duplicated, order preserved."""
    import os
    from pathlib import Path

    from . import config

    home = Path.home()
    parts: list[str] = []
    codex = config.codex_path()
    if codex:
        parts.append(str(Path(codex).expanduser().resolve().parent))
    parts += [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(home / ".local/bin"),
        str(home / ".volta/bin"),
        str(home / ".npm-global/bin"),
        str(home / ".cargo/bin"),
        str(home / ".deno/bin"),
    ]
    # Inherit the installing shell's PATH -- the single most reliable way to capture a
    # non-standard codex/node location, since that shell just ran this successfully.
    parts += os.environ.get("PATH", "").split(":")
    parts += ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return ":".join(out)


def install(
    port: int = 8765, interval: int = 300, autosave: int = 3600
) -> list[Path]:
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    written = []
    for label, payload in (
        (SERVE_LABEL, serve_plist(port)),
        (POLL_LABEL, poll_plist(interval)),
        (AUTOSAVE_LABEL, autosave_plist(autosave)),
    ):
        path = _plist_path(label)
        path.write_bytes(plistlib.dumps(payload))
        _bootout(label)          # replace any previous definition
        _bootstrap(path)
        written.append(path)
    return written


def uninstall() -> list[str]:
    removed = []
    for label in (SERVE_LABEL, POLL_LABEL, AUTOSAVE_LABEL):
        _bootout(label)
        path = _plist_path(label)
        if path.exists():
            path.unlink()
            removed.append(label)
    return removed


# /api/settings reads this on every open, and the app used to poll settings on a
# timer, so four *serial* `launchctl list` subprocesses (each able to hang) sat
# directly on the request path -- the real reason "Loading backend settings…"
# stalled. Probe the four labels concurrently, bound each with a timeout, and
# cache the result briefly so repeated reads don't re-spawn subprocesses.
_STATUS_CACHE: dict[str, object] = {"at": 0.0, "val": None}
_STATUS_TTL = 5.0


def _probe_label(label: str) -> str:
    try:
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return "loaded" if result.returncode == 0 else "not loaded"
    except Exception:
        # A timeout or a launchctl hiccup must not wedge the Settings request.
        return "unknown"


def status() -> dict[str, str]:
    import time
    from concurrent.futures import ThreadPoolExecutor

    now = time.monotonic()
    cached = _STATUS_CACHE["val"]
    if isinstance(cached, dict) and now - float(_STATUS_CACHE["at"]) < _STATUS_TTL:
        return cached
    labels = (SERVE_LABEL, POLL_LABEL, AUTOSAVE_LABEL, BACKUP_LABEL)
    with ThreadPoolExecutor(max_workers=len(labels)) as pool:
        out = dict(zip(labels, pool.map(_probe_label, labels)))
    _STATUS_CACHE["val"] = out
    _STATUS_CACHE["at"] = now
    return out


def _domain() -> str:
    import os

    return f"gui/{os.getuid()}"


def _bootstrap(path: Path) -> None:
    subprocess.run(
        ["launchctl", "bootstrap", _domain(), str(path)],
        capture_output=True,
        text=True,
    )


def _bootout(label: str) -> None:
    subprocess.run(
        ["launchctl", "bootout", f"{_domain()}/{label}"],
        capture_output=True,
        text=True,
    )
