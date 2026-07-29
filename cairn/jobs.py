"""Detached organize jobs.

Build-hierarchy and Tag-everything take minutes. Running them inside the API server
means a server restart -- a deploy, a crash, launchd -- kills the job mid-run, which
is exactly the "it starts but stops" failure. So these run in their OWN process
(start_new_session) and report progress to a small JSON status file that the API reads.
The job survives any restart of the server; the UI just keeps polling the file.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from . import config

# The long-running kinds that run detached. Short jobs stay in-thread (see api.py).
KINDS = ("build-vocab", "tag-all", "reorganize", "tag-queue")


def _dir() -> Path:
    d = config.db_path().parent / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(job_id: str) -> Path:
    return _dir() / f"{job_id}.json"


def _write(job_id: str, **fields) -> None:
    path = _path(job_id)
    current: dict = {}
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
    current.update(fields)
    path.write_text(json.dumps(current), encoding="utf-8")


def status(job_id: str) -> dict | None:
    """The job's current state, read from its status file (None if unknown)."""
    path = _path(job_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def spawn(kind: str, job_id: str) -> None:
    """Start `tt _run-job <kind> <job_id>` in a detached process and seed its status."""
    _write(job_id, id=job_id, kind=kind, state="running", log=[f"starting {kind}…"], result=None, error=None)
    tt = Path(sys.executable).parent / "tt"
    subprocess.Popen(
        [str(tt), "_run-job", kind, job_id],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # detach: outlives the API server
        cwd=str(config.db_path().parent),
    )


def run(kind: str, job_id: str) -> None:
    """Body executed inside the detached subprocess -- do the work, stream progress."""
    from . import db, topics
    from .backends.base import get_backend

    def log(message: str) -> None:
        current = status(job_id) or {}
        entries = current.get("log", [])
        entries.append(message)
        _write(job_id, log=entries[-300:])  # keep the tail bounded

    try:
        conn = db.connect()
        backend = get_backend()
        if kind == "build-vocab":
            result = topics.build_vocab(conn, backend, progress=log)
        elif kind == "tag-all":
            result = topics.tag_all(conn, backend, progress=log)
        elif kind == "reorganize":
            result = topics.organize_topics(conn, backend, progress=log)
        elif kind == "tag-queue":
            result = {"tagged": topics.drain_tag_queue(conn, backend, progress=log)}
        else:
            raise ValueError(f"unknown detached job kind: {kind}")
        _write(job_id, state="done", result=result)
    except Exception as exc:  # noqa: BLE001 -- record any failure for the UI
        _write(job_id, state="failed", error=str(exc))
