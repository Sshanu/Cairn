"""Backup the library to a file you can put in iCloud, Dropbox or a git repo.

The database is one SQLite file, but copying it with `cp` while anything holds a
write lock can capture a torn page, and WAL mode means recent commits may live
in a sidecar file that a naive copy misses. `VACUUM INTO` writes a single
consistent, fully-checkpointed file instead -- safe to run while the poller is
going, and safe to sync afterwards.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config, db

ICLOUD = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs"


@dataclass
class BackupResult:
    path: Path
    bytes_written: int
    items: int
    tags: int
    cloud: str | None = None


def default_destination() -> Path:
    """Prefer a cloud-synced folder if one exists, else ~/cairn-backups."""
    for candidate, _name in cloud_candidates():
        return candidate
    return Path.home() / "cairn-backups"


def cloud_candidates() -> list[tuple[Path, str]]:
    found = []
    if ICLOUD.is_dir():
        found.append((ICLOUD / "cairn", "iCloud Drive"))
    for name, label in (
        ("Dropbox", "Dropbox"),
        ("Google Drive", "Google Drive"),
        ("OneDrive", "OneDrive"),
    ):
        path = Path.home() / name
        if path.is_dir():
            found.append((path / "cairn", label))
    return found


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def backup(conn: sqlite3.Connection, destination: Path | None = None) -> BackupResult:
    """Write a consistent single-file copy of the library."""
    destination = Path(destination) if destination else default_destination()

    if destination.suffix == ".db":
        target = destination
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / f"cairn-{_stamp()}.db"

    if target.exists():
        target.unlink()

    # VACUUM INTO takes a read lock, checkpoints the WAL and writes one file.
    conn.execute("VACUUM INTO ?", (str(target),))

    items = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
    tags = conn.execute("SELECT COUNT(*) AS n FROM tags").fetchone()["n"]

    label = None
    for path, name in cloud_candidates():
        if str(target).startswith(str(path.parent)):
            label = name
            break

    return BackupResult(target, target.stat().st_size, items, tags, label)


def resolve_destination() -> Path:
    """The backup folder chosen in settings: iCloud, a custom folder, or a repo."""
    choice = config.backup_destination()
    if choice == "folder" and config.backup_folder():
        return Path(config.backup_folder()).expanduser()
    if choice == "github" and config.github_repo():
        return Path(config.github_repo()).expanduser()
    if choice == "icloud" and ICLOUD.is_dir():
        return ICLOUD / "cairn"
    return default_destination()


def configured_backup(conn: sqlite3.Connection) -> BackupResult:
    """Back up to the configured destination, keeping a JSON sidecar; for a git
    repo, commit and push so the copy actually leaves the machine."""
    dest = resolve_destination()
    result = backup(conn, dest)
    export_json(conn, dest)
    prune(dest)
    if config.backup_destination() == "github" and config.github_repo():
        result.cloud = _git_push(Path(config.github_repo()).expanduser())
    return result


def _git_push(repo: Path) -> str:
    import subprocess

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)

    if not (repo / ".git").is_dir():
        return "not a git repo"
    git("add", "-A")
    git("commit", "-m", f"cairn backup {_stamp()}")
    pushed = git("push")
    return "GitHub (pushed)" if pushed.returncode == 0 else f"git push failed: {pushed.stderr.strip()[:60]}"


def export_json(conn: sqlite3.Connection, destination: Path) -> Path:
    """Plain-text sidecar: survives SQLite itself and diffs well in git."""
    destination = Path(destination)
    if destination.is_dir() or not destination.suffix:
        destination.mkdir(parents=True, exist_ok=True)
        destination = destination / f"cairn-{_stamp()}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(db.export_items(conn), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return destination


def prune(destination: Path, keep: int = 10) -> int:
    """Keep the newest N backups in a directory."""
    destination = Path(destination)
    if not destination.is_dir():
        return 0
    backups = sorted(destination.glob("cairn-*.db"), reverse=True)
    removed = 0
    for stale in backups[keep:]:
        stale.unlink()
        removed += 1
    return removed


def restore(backup_path: Path, target: Path | None = None, *, force: bool = False) -> Path:
    """Copy a backup back over the live database. Refuses to clobber silently."""
    backup_path = Path(backup_path)
    target = Path(target) if target else config.db_path()
    if not backup_path.is_file():
        raise FileNotFoundError(backup_path)
    if target.exists() and not force:
        raise FileExistsError(
            f"{target} already exists -- pass force=True to overwrite it"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_path, target)
    # A restored file must not inherit stale WAL sidecars from the old database.
    for sidecar in (target.with_suffix(target.suffix + "-wal"),
                    target.with_suffix(target.suffix + "-shm")):
        if sidecar.exists():
            sidecar.unlink()
    return target
