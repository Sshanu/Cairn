"""JSON API for the local UI, plus static hosting for the built front end.

doc.md section 12 specified server-rendered Jinja with no build step. That was
overridden deliberately: the UI is a real React application now, so the Python
side is a plain JSON API and the front end owns rendering.

Everything stays local: bound to 127.0.0.1, no auth, no deployment.
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ask as ask_mod
from . import backup as backup_mod
from . import bibtex, capture, config, db, ingest as ingest_mod, organize as organize_mod
from .backends.base import get_backend

DIST = Path(__file__).parent / "static"

app = FastAPI(title="cairn", docs_url="/api/docs", redoc_url=None)


@app.middleware("http")
async def _log_requests(request, call_next):
    """Log every /api/ request with its duration, so the extension's and UI's calls are
    visible and timed in /tmp/cairn.serve.err -- the way to see WHICH request an action
    actually spent time on, on any machine. Slow ones (>=1s) are warnings so they stand
    out. This is the extension's request log."""
    import time

    from . import logs

    start = time.monotonic()
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/"):
        ms = (time.monotonic() - start) * 1000
        q = ("?" + request.url.query.split("&")[0]) if request.url.query else ""
        line = ("%s %s%s -> %s in %.0fms", request.method, path, q, response.status_code, ms)
        (logs.get("http").warning if ms >= 1000 else logs.get("http").info)(*line)
    return response


@app.exception_handler(Exception)
async def _log_unhandled(request, exc):
    """Any error that escapes an endpoint is logged with its FULL traceback (so it
    shows up in /tmp/cairn.serve.err or the terminal), and the real type/message is
    returned to the caller instead of a blank 'Internal Server Error'. This is what
    lets anyone read the log and see exactly what broke, on any machine."""
    from . import logs

    logs.get("api").exception(
        "unhandled error on %s %s", request.method, request.url.path
    )
    return JSONResponse(
        {"error": f"{type(exc).__name__}: {exc}", "path": request.url.path},
        status_code=500,
    )


_conn_local = threading.local()


def _conn() -> sqlite3.Connection:
    # One connection PER THREAD, reused across requests -- deliberately NOT one per
    # request. FastAPI runs sync endpoints in a bounded threadpool, so caching the
    # connection on the thread caps open database handles at the pool size. The old
    # "new connection every request" leaked a file handle each time an endpoint
    # forgot to close it, until the process hit its open-file limit and every query
    # died with "unable to open database file" (the whole UI went blank). A
    # per-thread cache makes that impossible, and is correct because sqlite objects
    # belong to their creating thread.
    conn = getattr(_conn_local, "conn", None)
    if conn is not None:
        try:
            conn.rollback()  # drop any transaction a prior failed request left open
            return conn
        except sqlite3.Error:
            try:
                conn.close()
            except Exception:
                pass
    conn = _conn_local.conn = db.connect()
    return conn


_fingerprinting: set[int] = set()
_fingerprint_lock = threading.Lock()


def _fingerprint_async(item_id: int) -> None:
    """Fetch an item's Semantic Scholar fingerprint off the request thread, so
    opening a paper never waits on the network. Deduped and best-effort."""
    with _fingerprint_lock:
        if item_id in _fingerprinting:
            return
        _fingerprinting.add(item_id)

    def run() -> None:
        from . import citations

        try:
            with db.session() as conn:  # always closed, even if the fetch raises
                citations.ensure_fingerprint(conn, item_id)
        except Exception:
            pass
        finally:
            with _fingerprint_lock:
                _fingerprinting.discard(item_id)

    threading.Thread(target=run, daemon=True).start()


def _serialize(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    tags: list[str] | None = None,
) -> dict:
    record = dict(row)
    record.pop("body", None)
    # The full BibTeX is heavy and only ever needed one item at a time; the list
    # keeps the light provenance so a row can show a "has BibTeX" hint, and the
    # detail endpoint adds the text back.
    record["has_bibtex"] = bool(record.pop("bibtex", None))
    record.pop("ref_ids", None)  # heavy, and only used server-side for "related"
    record.pop("embedding", None)  # raw float32 bytes -- not JSON, server-side only
    record["tags"] = db.item_tags(conn, row["id"]) if tags is None else tags
    record["age_days"] = db.age_days(row["first_seen"])
    return record


def _serialize_many(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    """Serialize a result page with one batched tag lookup, not one per row."""
    tags_by_item = db.item_tags_many(conn, [row["id"] for row in rows])
    return [
        _serialize(conn, row, tags=tags_by_item.get(row["id"], [])) for row in rows
    ]


def _serialize_history(row: sqlite3.Row) -> dict:
    """The timeline only needs row-label fields, not abstracts or tag arrays."""
    return {
        "id": row["id"],
        "canonical_url": row["canonical_url"],
        "title": row["title"],
        "source": row["source"],
        "venue": row["venue"],
        "year": row["year"],
    }


# --- library ----------------------------------------------------------------


@app.get("/api/build")
def build() -> dict:
    """Identity of the bundle on disk, so a stale tab can notice and reload.

    Every rebuild during this project left the browser running old JavaScript
    against a new database, which looked like "the fix did not work". The app
    polls this and reloads itself instead.
    """
    index = DIST / "index.html"
    return {
        "build": str(int(index.stat().st_mtime)) if index.exists() else "dev",
    }


@app.get("/api/revision")
def revision() -> JSONResponse:
    """A stat-only library change token for the UI's one lightweight poll."""
    return JSONResponse(
        {"revision": db.revision_token()},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/stats")
def stats() -> dict:
    conn = _conn()
    total = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
    untagged = conn.execute(
        "SELECT COUNT(*) AS n FROM items i WHERE " + "NOT EXISTS (SELECT 1 FROM item_tags it JOIN tags t ON t.id = it.tag_id WHERE it.item_id = i.id AND (t.name LIKE 'topic/%' OR t.name LIKE 'method/%' OR t.name LIKE 'task/%' OR t.name LIKE 'contribution/%'))"
    ).fetchone()["n"]
    no_topic = conn.execute(
        "SELECT COUNT(*) AS n FROM items i WHERE NOT EXISTS "
        "(SELECT 1 FROM item_tags it JOIN tags t ON t.id = it.tag_id "
        "WHERE it.item_id = i.id AND t.name LIKE 'topic/%')"
    ).fetchone()["n"]
    stale = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE first_seen < ?", (db.days_ago(90),)
    ).fetchone()["n"]
    return {
        "total": total,
        "untagged": untagged,
        "no_topic": no_topic,
        "queued": db.tag_queue_count(conn),
        "stale": stale,
        "database": str(config.db_path()),
        "status": {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM items GROUP BY status"
            )
        },
        "sources": {
            row["source"]: row["n"]
            for row in conn.execute(
                # COALESCE in the GROUP BY, so NULL and 'web' are one bucket -- else the
                # two collide on the same dict key and one silently overwrites the other.
                "SELECT COALESCE(source, 'web') AS source, COUNT(*) AS n FROM items "
                "GROUP BY COALESCE(source, 'web') ORDER BY n DESC"
            )
        },
        "buckets": {
            (row["bucket"] or "library"): row["n"]
            for row in conn.execute(
                "SELECT bucket, COUNT(*) AS n FROM items GROUP BY bucket"
            )
        },
        "tags": [{"name": n, "count": c} for n, c in db.tag_counts(conn)],
    }


class SettingsPatch(BaseModel):
    ingest_interval_min: int | None = None
    poll_interval_min: int | None = None
    backup_interval_hours: int | None = None
    backup_destination: str | None = None
    backup_folder: str | None = None
    github_repo: str | None = None
    blocklist: list[str] | None = None
    auto_organize: bool | None = None
    auto_file_projects: bool | None = None
    auto_summary: bool | None = None
    related_citations: bool | None = None
    weekly_digest: bool | None = None
    organization_guidance: str | None = None
    classify_prompt: str | None = None
    tag_proposal_prompt: str | None = None
    hierarchy_prompt: str | None = None
    facets: list[str] | None = None
    agent_provider: str | None = None
    agent_model: str | None = None
    agent_base_url: str | None = None
    agent_api_key: str | None = None
    agent_codex_path: str | None = None
    reasoning_effort: str | None = None
    tracking_paused: bool | None = None


def _settings_view() -> dict:
    from . import agents
    from . import buckets as buckets_mod
    from . import topics as topics_mod

    return {
        "ingest_interval_min": config.ingest_interval_min(),
        "poll_interval_min": config.poll_interval_min(),
        "backup_interval_hours": config.backup_interval_hours(),
        "backup_destination": config.backup_destination(),
        "backup_folder": config.backup_folder() or "",
        "github_repo": config.github_repo() or "",
        "blocklist": list(config.blocklist()),
        # The built-in never-store lists, shown read-only so it's clear what's
        # always excluded on top of the user's own domains. Full transparency.
        "builtin_blocklist": buckets_mod.builtin_never_groups(),
        "backup_here": str(backup_mod.resolve_destination()),
        "cloud_options": [name for _, name in backup_mod.cloud_candidates()],
        "auto_organize": config.auto_organize(),
        "auto_file_projects": config.auto_file_projects(),
        "auto_summary": config.auto_summary(),
        "related_citations": config.related_citations(),
        "weekly_digest": config.weekly_digest(),
        # Standing rules (override-or-default) + the default, so the box is never
        # empty and offers "Reset to default" like the prompts.
        "organization_guidance": config.organization_guidance(),
        "organization_guidance_default": config.DEFAULT_GUIDANCE,
        # The three organization prompts (override-or-default) + their defaults,
        # so the UI can show each and offer "Reset to default". Fully transparent.
        "tag_proposal_prompt": config.tag_proposal_prompt() or topics_mod.PROPOSE_SYSTEM,
        "tag_proposal_prompt_default": topics_mod.PROPOSE_SYSTEM,
        "hierarchy_prompt": config.hierarchy_prompt() or topics_mod.HIERARCHY_SYSTEM,
        "hierarchy_prompt_default": topics_mod.HIERARCHY_SYSTEM,
        "classify_prompt": config.tagging_prompt() or organize_mod.CLASSIFY_SYSTEM,
        "classify_prompt_default": organize_mod.CLASSIFY_SYSTEM,
        "facets": list(config.facets()),
        "facets_default": list(topics_mod.DEFAULT_FACETS),
        "agent_provider": config.backend_name(),
        "agent_model": config.load().get("model") or "",
        "agent_base_url": config.agent_base_url() or "",
        "agent_codex_path": config.codex_path() or "",
        "agent_api_key_set": bool(config.agent_api_key()),  # never return the key itself
        "reasoning_effort": config.reasoning_effort() or "",
        "tracking_paused": config.tracking_paused(),
        "agents": agents.status(),
    }


@app.post("/api/capture")
def capture_tab(
    background: BackgroundTasks,
    url: str, title: str = "", abstract: str = "", authors: str = "",
    bibtex: str = "", tags: str = "",
) -> dict:
    """Save one tab straight from the browser extension's "Save to Cairn" popup.

    The url + title come from the page itself, so -- unlike the old macOS Quick Action
    -- there is NO Automation permission to control Chrome and nothing to bind in System
    Settings. `tags` (comma-separated) is the collection/branch the user filed it into,
    plus any extra tags, applied as manual tags. Query params keep it a simple
    cross-origin request. Blocked/never-store URLs are rejected like any other save.
    """
    from . import capture as capture_mod, logs, taxonomy

    log = logs.get("capture")
    conn = _conn()
    chosen = tuple(t.strip().rstrip("/") for t in tags.split(",") if t.strip())
    # fetch_meta=False: the extension already supplied the title (and usually the
    # abstract/authors it scraped or resolved), so DON'T block the popup while the server
    # re-fetches metadata over the network -- that round-trip is exactly what left the
    # popup stuck on "Saving…". Save instantly; full metadata (abstract, body, venue,
    # year) is filled in by a background task right after we respond.
    saved = capture_mod.save_url(
        conn, url, title=title or None, abstract=abstract or None,
        authors=authors or None, fetch_meta=False, tags=chosen,
    )
    if saved is None:
        log.info("rejected (blocked / not a web page): %s", url)
        return {"saved": False, "reason": "blocked or not a web page"}
    log.info(
        "saved item %d (%s) <- %s%s",
        saved.item_id, "new" if saved.created else "update", url,
        (" filed: " + ", ".join(chosen)) if chosen else "",
    )
    # URL-computed facets now, so it's browsable immediately; model tagging happens on
    # the next organize cycle.
    row = db.get_item(conn, saved.item_id)
    if row is not None:
        facets = taxonomy.computed_facets(
            row["canonical_url"], row["source"], row["venue"], row["year"]
        )
        if facets:
            db.add_tags(conn, saved.item_id, facets, origin="model")
        # OpenReview publishes each paper's BibTeX in the note, but the server is
        # Cloudflare-blocked from fetching it -- so the extension grabs it from the
        # browser and hands it over here. Store it only when we have none already
        # (never over an entry resolved from a published source or typed by hand).
        entry = bibtex.strip()
        if entry and not (row["bibtex"] or "").strip():
            published = not entry.lstrip().lower().startswith(("@misc", "@unpublished"))
            db.set_bibtex(
                conn, saved.item_id, bibtex=entry, source="openreview",
                venue=None, published=published,
            )
    # NOTE: do NOT resolve metadata here. It used to be a FastAPI BackgroundTask, but
    # those share the SAME anyio threadpool that serves every sync request -- a blocking
    # network meta.resolve() there starves the threads that serve /api/items,
    # /api/settings, etc., making the WHOLE app crawl. The item keeps the metadata the
    # extension already supplied; the poll process (off the web server) tags it and heals
    # any missing title. Full metadata backfill is `tt backfill`, also off the server.
    db.enqueue_for_tagging(conn, saved.item_id)
    return {"saved": True, "title": saved.title or title, "created": saved.created}


def _enrich_captured(item_id: int) -> None:
    """After a fast capture, fill in metadata the extension didn't supply (abstract,
    body, venue, year) over the network -- best-effort, off the request path so the
    popup never waits. A bot-walled site (OpenReview) resolves to nothing and the
    extension-provided fields simply stand."""
    from . import db as _db, logs, meta as _meta

    log = logs.get("capture")
    try:
        with _db.session() as conn:
            row = _db.get_item(conn, item_id)
            if row is None:
                return
            got = _meta.resolve(row["canonical_url"], row["raw_url"])
            err = got.pop("_error", None)
            fields = {
                key: val
                for key, val in got.items()
                if val and key in ("title", "authors", "venue", "year", "abstract", "body")
            }
            if fields:
                _db.upsert_item(conn, row["canonical_url"], **fields)
                _db.reindex_item(conn, item_id)
                # venue/year may have only just arrived from resolve() -> (re)apply the
                # URL-computed facets so venue/ tags aren't missing (codex review P2).
                from . import taxonomy

                fresh = _db.get_item(conn, item_id) or row
                facets = taxonomy.computed_facets(
                    fresh["canonical_url"], fresh["source"], fresh["venue"], fresh["year"]
                )
                if facets:
                    _db.add_tags(conn, item_id, facets, origin="model")
                log.info("enriched item %d: filled %s", item_id, ", ".join(sorted(fields)))
            else:
                log.info("enrich item %d: nothing to add%s", item_id, f" ({err})" if err else "")
    except Exception as exc:  # the item is already saved; enrichment is a nicety
        log.warning("enrich item %d failed: %s: %s", item_id, type(exc).__name__, exc)


@app.get("/api/lookup")
def lookup_tab(url: str) -> dict:
    """Is this URL already saved? Return its title and the collections/branches it's
    filed into, so the extension popup can say 'already in your library' and show its
    current collections instead of treating a re-save as a brand-new item."""
    from .canonical import canonicalize

    conn = _conn()
    canonical = canonicalize(url)
    row = db.get_item_by_url(conn, canonical)
    if row is None:
        alias = conn.execute(
            "SELECT item_id FROM aliases WHERE canonical_url = ?", (canonical,)
        ).fetchone()
        if alias:
            row = db.get_item(conn, alias["item_id"])
    if row is None:
        return {"exists": False}
    skip = {"venue", "type", "site", "contribution"}
    tags = [t for t in db.item_tags(conn, row["id"]) if t.split("/")[0] not in skip]
    return {"exists": True, "title": row["title"], "tags": sorted(tags)}


@app.get("/api/resolve")
def resolve_preview(url: str) -> dict:
    """Preview the metadata a save WOULD extract for a URL, without saving it -- so the
    extension popup can show the real title on a page it can't scrape itself (a PDF
    viewer has no HTML). The server resolves ACL / arXiv / DOI / conference metadata over
    the web; it can't reach a bot-walled site (OpenReview), which the extension fetches
    from the browser instead."""
    from . import meta
    from .canonical import canonicalize

    canonical = canonicalize(url)
    if not canonical or not canonical.startswith("http"):
        return {"title": "", "authors": "", "abstract": ""}
    got = meta.resolve(canonical, url)
    return {
        "title": got.get("title") or "",
        "authors": got.get("authors") or "",
        "abstract": got.get("abstract") or "",
    }


@app.get("/api/branches")
def list_branches() -> dict:
    """The fileable branches for the extension's collection picker: the topic hierarchy
    and the user's project collections. venue/type/site are computed from the URL and
    contribution is agent-assigned, so none of those are places to file a paper by hand."""
    conn = _conn()
    skip = {"venue", "type", "site", "contribution"}
    return {
        "branches": [
            r["name"]
            for r in conn.execute("SELECT name FROM tags ORDER BY name")
            if r["name"].split("/")[0] not in skip
        ]
    }


@app.post("/api/agent/test")
def agent_test() -> dict:
    """Live-test the configured model backend (codex / claude / openai / ollama): make a
    tiny real call and report whether it works. Backs the Settings 'Test agent' button,
    and every call is logged so a failure shows up in the terminal / serve log too."""
    from . import logs

    try:
        backend = get_backend()
    except Exception as exc:
        logs.get("agent").warning("no working backend: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "name": None, "model": None, "latency_ms": 0, "error": str(exc)}
    result = backend.health_check()
    # Transparency: for codex, report the detected CLI version, the minimum Cairn
    # needs, and whether structured output is available -- all visible in Settings.
    if getattr(backend, "name", "") == "codex":
        from .backends.codex import CodexExecBackend

        exe = getattr(backend, "executable", "codex")
        result["codex_path"] = shutil.which(exe) or config.codex_path() or exe
        result["codex_version"] = CodexExecBackend.installed_version(exe)
        result["codex_min_version"] = CodexExecBackend.MIN_VERSION
        result["supports_output_schema"] = CodexExecBackend.supports_output_schema(exe)
    return result


@app.get("/api/settings")
def get_settings() -> dict:
    """Everything the backend does on a schedule or a rule, editable from the UI."""
    return _settings_view()


@app.put("/api/settings")
def put_settings(patch: SettingsPatch) -> dict:
    """Persist settings; reload the timed agents if a cadence changed."""
    from . import agents

    values = {k: v for k, v in patch.model_dump().items() if v is not None}
    # The provider fields map onto the underlying config keys.
    for ui_key, cfg_key in (
        ("agent_provider", "backend"),
        ("agent_model", "model"),
        ("agent_api_key", "api_key"),
        ("agent_codex_path", "codex_path"),
    ):
        if ui_key in values:
            values[cfg_key] = values.pop(ui_key).strip()
    # A domain can be pasted as a URL; store just the host.
    if "blocklist" in values:
        values["blocklist"] = sorted({_host_only(d) for d in values["blocklist"] if d.strip()})
    if "backup_destination" in values and values["backup_destination"] not in (
        "icloud", "folder", "github",
    ):
        raise HTTPException(400, "backup_destination must be icloud, folder or github")
    # Never persist a prompt or facet override that just equals the default -- a
    # stored copy of the default would shadow future changes to the default. Clear
    # it instead (empty -> the getter falls back to the built-in default).
    from . import topics as topics_mod

    for key, default in (
        ("classify_prompt", organize_mod.CLASSIFY_SYSTEM),
        ("tag_proposal_prompt", topics_mod.PROPOSE_SYSTEM),
        ("hierarchy_prompt", topics_mod.HIERARCHY_SYSTEM),
        ("organization_guidance", config.DEFAULT_GUIDANCE),
    ):
        if key in values and str(values[key]).strip() == default.strip():
            values[key] = ""
    if "facets" in values and list(values["facets"]) == list(topics_mod.DEFAULT_FACETS):
        values["facets"] = []  # empty -> getter returns DEFAULT_FACETS
    config.save(**values)

    # Blocking a domain removes what's already saved from it -- "never track these"
    # means gone, not merely no-new. delete_item also dismisses the URL so it can't
    # be re-ingested from a still-open tab.
    purged_blocked = 0
    if "blocklist" in values:
        conn = _conn()
        blocked = [
            row["id"]
            for row in conn.execute("SELECT id, canonical_url FROM items")
            if config.is_blocked(row["canonical_url"])
        ]
        purged_blocked = sum(1 for i in blocked if db.delete_item(conn, i))

    rescheduled = bool(
        {"ingest_interval_min", "poll_interval_min", "backup_interval_hours", "tracking_paused"}
        & set(values)
    )
    if rescheduled:
        try:
            agents.apply_schedule()
        except Exception as exc:  # launchctl can fail in odd environments
            return {**_settings_view(), "rescheduled": False, "schedule_error": str(exc), "purged_blocked": purged_blocked}
    return {**_settings_view(), "rescheduled": rescheduled, "purged_blocked": purged_blocked}


def _host_only(value: str) -> str:
    from urllib.parse import urlsplit

    value = value.strip().lower()
    if "//" in value or "/" in value:
        host = urlsplit(value if "//" in value else "//" + value).hostname or value
    else:
        host = value
    return (host or "").removeprefix("www.")


@app.get("/api/items")
def list_items(
    q: str = "",
    tag: str = "",
    status: str = "",
    source: str = "",
    bucket: str = "",  # "" = every bucket; the UI sends "library"/"docs" explicitly
    untagged: bool = False,
    no_topic: bool = False,
    sort: Literal["relevance", "newest", "oldest", "age"] = "relevance",
    limit: int = Query(60, le=500),
    offset: int = 0,
) -> dict:
    conn = _conn()
    terms = [t for t in q.split() if t.strip()]
    filters = {
        "tag": tag or None,
        "status": status or None,
        "bucket": bucket or None,
        "source": source or None,
        "untagged": untagged,
        "no_topic": no_topic,
    }

    tag_strategy = db.tag_filter_strategy(conn, filters["tag"])
    total = db.count(conn, terms, tag_strategy=tag_strategy, **filters)
    rows = db.search(
        conn,
        terms,
        limit=limit,
        offset=offset,
        sort=sort,
        tag_strategy=tag_strategy,
        **filters,
    )

    return {
        "items": _serialize_many(conn, rows),
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@app.get("/api/map")
def map_items(bucket: str = "library") -> dict:
    """Every item with its deepest topic -- the whole library, for the cluster map.

    Deliberately not the filtered/paged item list: a map that only showed 300
    rows looked empty. Kept lightweight (id, title, one topic path) so returning
    the entire library is cheap.
    """
    conn = _conn()
    where = "WHERE COALESCE(i.bucket,'library') = ?" if bucket else ""
    params = [bucket] if bucket else []
    rows = conn.execute(
        "SELECT i.id, i.title, i.source, i.year, "
        "  (SELECT t.name FROM item_tags it JOIN tags t ON t.id = it.tag_id "
        "   WHERE it.item_id = i.id AND t.name LIKE 'topic/%' "
        "   ORDER BY LENGTH(t.name) DESC LIMIT 1) AS topic "
        f"FROM items i {where}",
        params,
    ).fetchall()
    return {
        "items": [
            {"id": r["id"], "title": r["title"], "topic": r["topic"], "source": r["source"]}
            for r in rows
        ]
    }


@app.get("/api/digest")
def digest() -> dict:
    """A weekly rollup: what you added, what's waiting, what's clustering.

    Entirely deterministic -- counts and groupings, no model -- so it is cheap to
    recompute on every visit and honest about your actual reading backlog.
    """
    conn = _conn()
    week = db.days_ago(7)
    month = db.days_ago(30)

    added = conn.execute(
        "SELECT COUNT(*) n FROM items WHERE first_seen >= ?", (week,)
    ).fetchone()["n"]
    read = conn.execute("SELECT COUNT(*) n FROM items WHERE status = 'read'").fetchone()["n"]
    reading = conn.execute("SELECT COUNT(*) n FROM items WHERE status = 'reading'").fetchone()["n"]
    stale = conn.execute(
        "SELECT COUNT(*) n FROM items WHERE status = 'unread' AND first_seen < ? "
        "AND COALESCE(bucket,'library') = 'library'",
        (month,),
    ).fetchone()["n"]

    # Active topics: where you have been adding recently. This is "current
    # focus", not "biggest pile", so the read-next suggestions track the project
    # you are on now rather than one you abandoned.
    active = conn.execute(
        "SELECT t.name, COUNT(*) n FROM tags t "
        "JOIN item_tags it ON it.tag_id = t.id "
        "JOIN items i ON i.id = it.item_id "
        "WHERE t.name LIKE 'topic/%' AND t.name NOT LIKE 'topic/%/%' "
        "AND i.first_seen >= ? GROUP BY t.name ORDER BY n DESC LIMIT 6",
        (month,),
    ).fetchall()
    # Fall back to overall top topics if nothing is recent (e.g. right after an import).
    if not active:
        active = conn.execute(
            "SELECT t.name, COUNT(DISTINCT it.item_id) n FROM tags t "
            "JOIN item_tags it ON it.tag_id = t.id "
            "WHERE t.name LIKE 'topic/%' AND t.name NOT LIKE 'topic/%/%' "
            "GROUP BY t.name ORDER BY n DESC LIMIT 6"
        ).fetchall()
    clusters = [{"name": r["name"], "count": r["n"]} for r in active]

    # Read next: unread papers in those active topics, most recently encountered
    # first. Never the oldest -- a years-old unread paper is one you moved past.
    active_names = [c["name"] for c in clusters]
    read_next = []
    if active_names:
        placeholders = ",".join("?" for _ in active_names)
        like = " OR ".join("t.name = ? OR t.name LIKE ? || '/%'" for _ in active_names)
        params: list = []
        for name in active_names:
            params += [name, name]
        read_next = conn.execute(
            "SELECT DISTINCT i.id, i.title, i.canonical_url, i.first_seen FROM items i "
            "JOIN item_tags it ON it.item_id = i.id JOIN tags t ON t.id = it.tag_id "
            f"WHERE i.status = 'unread' AND COALESCE(i.bucket,'library') = 'library' "
            f"AND ({like}) ORDER BY i.first_seen DESC LIMIT 5",
            params,
        ).fetchall()

    return {
        "added_week": added,
        "read": read,
        "reading": reading,
        "stale_unread": stale,
        "clusters": clusters,
        "read_next": [
            {
                "id": r["id"],
                "title": r["title"],
                "canonical_url": r["canonical_url"],
                "age_days": db.age_days(r["first_seen"]),
            }
            for r in read_next
        ],
    }


@app.get("/api/history")
def history(
    bucket: str = "",
    before: str = "",
    days: int = Query(30, le=120),
) -> dict:
    """Daily activity: which items were first seen on each day, newest first."""
    conn = _conn()
    rows, next_before = db.history(
        conn, bucket=bucket or None, before=before or None, limit_days=days
    )
    groups: list[dict] = []
    for row in rows:
        date = (row["first_seen"] or "")[:10]
        if not groups or groups[-1]["date"] != date:
            groups.append({"date": date, "count": 0, "items": []})
        groups[-1]["items"].append(_serialize_history(row))
        groups[-1]["count"] += 1
    return {"days": groups, "next_before": next_before}


@app.get("/api/items/{item_id}")
def get_item(item_id: int) -> dict:
    conn = _conn()
    row = db.get_item(conn, item_id)
    if row is None:
        raise HTTPException(404, "no such item")
    record = _serialize(conn, row)
    record["body_present"] = bool(row["body"])
    record["bibtex"] = row["bibtex"]
    # Every URL that was merged into this item, so a merge never hides a link.
    record["alt_urls"] = [
        r["canonical_url"]
        for r in conn.execute(
            "SELECT canonical_url FROM aliases WHERE item_id = ? ORDER BY canonical_url",
            (item_id,),
        )
        if r["canonical_url"] != row["canonical_url"]
    ]
    return record


@app.get("/api/items/{item_id}/bibtex")
def get_bibtex(item_id: int) -> dict:
    """Return the STORED BibTeX only -- never resolve on GET.

    Resolving hits the web (CVF/ACL/CrossRef/DBLP/arXiv) and would stall the
    detail panel; the POST endpoint (the Find / re-fetch button) and the backfill
    job do the fetching instead.
    """
    conn = _conn()
    row = db.get_item(conn, item_id)
    if row is None:
        raise HTTPException(404, "no such item")
    if not row["bibtex"]:
        return {"bibtex": None, "source": None, "venue": None, "published": False, "found": False}
    return {
        "bibtex": row["bibtex"],
        "source": row["bibtex_source"],
        "venue": row["bibtex_venue"],
        "published": bool(row["bibtex_published"]),
        "found": True,
    }


@app.get("/api/items/{item_id}/related")
def get_related(item_id: int) -> dict:
    """Items sharing the most references with this one (opt-in feature).

    Never blocks on the network: returns whatever fingerprints are already
    cached and populates a missing one in the background, so opening a paper is
    instant. The panel fills in on the next open once the fingerprint lands.
    """
    if not config.related_citations():
        return {"enabled": False, "related": []}
    from . import citations

    conn = _conn()
    row = db.get_item(conn, item_id)
    if row is None:
        raise HTTPException(404, "no such item")
    rel = citations.related(conn, item_id)
    if row["ref_ids"] is None:
        _fingerprint_async(item_id)
        return {"enabled": True, "related": rel, "computing": True}
    return {"enabled": True, "related": rel}


@app.post("/api/items/{item_id}/summarize")
def summarize_item(item_id: int) -> dict:
    """Generate the TL;DR + why-saved line for this item now."""
    from . import summarize

    conn = _conn()
    if db.get_item(conn, item_id) is None:
        raise HTTPException(404, "no such item")
    try:
        return summarize.summarize_item(conn, get_backend(), item_id)
    except Exception as exc:
        raise HTTPException(502, f"could not summarize: {exc}")


@app.post("/api/items/{item_id}/bibtex")
def refresh_bibtex(item_id: int) -> dict:
    """Re-resolve the BibTeX from scratch (e.g. after enrichment filled authors)."""
    conn = _conn()
    row = db.get_item(conn, item_id)
    if row is None:
        raise HTTPException(404, "no such item")
    return _resolve_and_store(conn, row)


def _resolve_and_store(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    result = bibtex.for_item(row)
    if not result.ok:
        # No real entry online. We never fabricate one -- clear any stale auto entry
        # and tell the UI to offer a paste box. Not an error. But never clear a `manual`
        # entry the user typed, nor an `openreview` one the extension captured from the
        # browser -- the server CAN'T re-fetch OpenReview (Cloudflare), so a re-resolve
        # here always comes back empty and would wrongly wipe a good entry.
        if row["bibtex"] and row["bibtex_source"] not in ("manual", "openreview"):
            db.clear_bibtex(conn, row["id"])
        elif row["bibtex"]:
            # We kept a manual/openreview entry -- return it so the panel keeps showing
            # it instead of flashing "not found".
            return {
                "bibtex": row["bibtex"], "source": row["bibtex_source"],
                "venue": row["bibtex_venue"], "published": bool(row["bibtex_published"]),
                "found": True,
            }
        return {"bibtex": None, "source": None, "venue": None, "published": False, "found": False}
    db.set_bibtex(
        conn, row["id"],
        bibtex=result.bibtex, source=result.source,
        venue=result.venue, published=result.published,
    )
    return {
        "bibtex": result.bibtex,
        "source": result.source,
        "venue": result.venue,
        "published": result.published,
        "found": True,
    }


class ExportBody(BaseModel):
    # Either an explicit selection, or the current filter (same params as the
    # item list) so "export everything under this tag" works without listing ids.
    ids: list[int] | None = None
    q: str = ""
    tag: str = ""
    status: str = ""
    bucket: str = "library"
    untagged: bool = False
    no_topic: bool = False


@app.post("/api/export/bibtex")
def export_bibtex(body: ExportBody) -> dict:
    """Concatenate the stored BibTeX for a selection or the current filter, ready
    to save as a .bib. Never fabricates entries -- items with no extracted BibTeX
    are returned in `missing` so the user can fill them in rather than shipping a
    made-up citation.
    """
    conn = _conn()
    if body.ids:
        rows = [r for r in (db.get_item(conn, i) for i in body.ids) if r is not None]
    else:
        terms = [t for t in body.q.split() if t.strip()]
        rows = db.search(
            conn, terms, limit=10_000, offset=0, sort="newest",
            tag=body.tag or None, status=body.status or None,
            bucket=body.bucket or None, untagged=body.untagged, no_topic=body.no_topic,
        )
    # Resolve any paper missing a stored entry, so a collection/branch exports as a
    # COMPLETE bibliography -- not just the few whose detail panel happened to be
    # opened (BibTeX is extracted lazily). Cached to the DB, so a repeat export is
    # instant; capped so a huge filter can't turn into a very long network run -- those
    # export what's already stored and list the rest in `missing`. Never fabricated:
    # for_item skips non-papers and anything no source can resolve.
    todo = [r for r in rows if not (r["bibtex"] or "").strip()]
    if todo and len(todo) <= 100:
        bibtex.backfill(conn, todo, workers=6)
        rows = [db.get_item(conn, r["id"]) or r for r in rows]
    entries: list[str] = []
    missing: list[dict] = []
    for r in rows:
        bib = (r["bibtex"] or "").strip()
        if bib:
            entries.append(bib)
        else:
            missing.append({"id": r["id"], "title": r["title"]})
    return {
        "bibtex": "\n\n".join(entries),
        "total": len(rows),
        "with_bibtex": len(entries),
        "missing": missing,
    }


class ItemPatch(BaseModel):
    status: str | None = None
    notes: str | None = None
    bucket: str | None = None
    title: str | None = None
    authors: str | None = None
    venue: str | None = None
    year: int | None = None
    bibtex: str | None = None


@app.patch("/api/items/{item_id}")
def patch_item(item_id: int, patch: ItemPatch) -> dict:
    conn = _conn()
    if db.get_item(conn, item_id) is None:
        raise HTTPException(404, "no such item")
    if patch.status is not None:
        if patch.status not in ("unread", "reading", "read", "archived"):
            raise HTTPException(400, "invalid status")
        db.set_status(conn, item_id, patch.status)
    if patch.bucket is not None:
        if patch.bucket not in ("library", "docs"):
            raise HTTPException(400, "invalid bucket")
        db.set_bucket(conn, item_id, patch.bucket)
    # Captured metadata is sometimes wrong; let the user correct it directly.
    edits = {
        field: getattr(patch, field)
        for field in ("title", "authors", "venue", "year")
        if getattr(patch, field) is not None
    }
    if edits:
        db.set_fields(conn, item_id, **edits)
    if patch.bibtex is not None:
        # A user-typed entry is authoritative and never overwritten by a later
        # auto-resolve; an empty string clears it.
        text = patch.bibtex.strip()
        if text:
            db.set_bibtex(
                conn, item_id, bibtex=text, source="manual",
                venue=None, published="@misc" not in text.split("{", 1)[0].lower(),
            )
        else:
            db.clear_bibtex(conn, item_id)
    if patch.notes is not None:
        db.set_notes(conn, item_id, patch.notes)
    return _serialize(conn, db.get_item(conn, item_id))


@app.delete("/api/items/{item_id}")
def delete_item(item_id: int) -> dict:
    """Permanently delete an item -- its tags, aliases and search entry go too.

    A hard delete, not an archive: 'archived' status already exists for hiding
    something you might want back. This is for items that should never have been
    saved. If the tab is still open in Chrome, a later ingest can re-add it.
    """
    conn = _conn()
    if not db.delete_item(conn, item_id):
        raise HTTPException(404, "no such item")
    return {"deleted": item_id}


class TagsBody(BaseModel):
    tags: list[str]


@app.post("/api/items/{item_id}/tags")
def add_item_tags(item_id: int, body: TagsBody) -> dict:
    conn = _conn()
    if db.get_item(conn, item_id) is None:
        raise HTTPException(404, "no such item")
    db.add_tags(conn, item_id, body.tags, origin="manual")
    return _serialize(conn, db.get_item(conn, item_id))


@app.delete("/api/items/{item_id}/tags/{name:path}")
def remove_item_tag(item_id: int, name: str) -> dict:
    conn = _conn()
    db.remove_tag_from_item(conn, item_id, name)
    return _serialize(conn, db.get_item(conn, item_id))


class BulkBody(BaseModel):
    ids: list[int]
    status: str | None = None
    tags: list[str] | None = None
    remove_tags: list[str] | None = None
    bucket: str | None = None


@app.post("/api/items/bulk")
def bulk_update(body: BulkBody) -> dict:
    """Bulk select then one action -- the screen the tool exists to make possible."""
    conn = _conn()
    changed = 0
    for item_id in body.ids:
        if db.get_item(conn, item_id) is None:
            continue
        if body.status in ("unread", "reading", "read", "archived"):
            db.set_status(conn, item_id, body.status)
        if body.bucket in ("library", "docs"):
            db.set_bucket(conn, item_id, body.bucket)
        if body.tags:
            db.add_tags(conn, item_id, body.tags, origin="manual")
        for name in body.remove_tags or []:
            db.remove_tag_from_item(conn, item_id, name)
        changed += 1
    return {"changed": changed}


class DeleteBody(BaseModel):
    ids: list[int]


@app.post("/api/items/bulk/delete")
def bulk_delete(body: DeleteBody) -> dict:
    """Permanently delete every selected item (tags, aliases and index too)."""
    conn = _conn()
    deleted = sum(1 for item_id in body.ids if db.delete_item(conn, item_id))
    return {"deleted": deleted}


@app.post("/api/items/merge")
def merge_items(body: DeleteBody) -> dict:
    """Merge the selected items into one, for near-duplicates the auto-finder missed.

    The most complete item survives and keeps its title/URL; every other item's
    URL becomes an alias (so re-ingest resolves to the survivor), their tags are
    unioned onto it, and any field the survivor lacks -- abstract, authors, venue,
    BibTeX, notes -- is filled from a copy that has it. The earliest first_seen and
    the most-advanced reading status win. The other rows are then deleted.
    """
    conn = _conn()
    rows = [r for r in (db.get_item(conn, i) for i in dict.fromkeys(body.ids)) if r is not None]
    if len(rows) < 2:
        raise HTTPException(400, "select at least two existing items to merge")
    result = organize_mod.merge_group(conn, rows)
    return {"primary": result["primary"], "merged": len(result["merged"])}


# --- vocabulary management --------------------------------------------------


@app.get("/api/tags")
def list_tags() -> dict:
    conn = _conn()
    tree, flat = db.tag_snapshot(conn)
    return {
        "tags": tree,
        "flat": [{"name": name, "count": count} for name, count in flat],
    }


class CreateTagBody(BaseModel):
    name: str


@app.post("/api/tags/prune")
def prune_tags(dry_run: bool = False) -> dict:
    """Drop model-proposed branches that hold nothing. Hand-made ones stay."""
    conn = _conn()
    pruned = db.prune_empty_tags(conn, dry_run=dry_run)
    return {"pruned": pruned, "count": len(pruned), "dry_run": dry_run}


@app.post("/api/tags")
def create_tag(body: CreateTagBody) -> dict:
    """Create an empty tag, so a structure can be built before anything is filed."""
    conn = _conn()
    name = db.create_tag(conn, body.name)
    if not name:
        raise HTTPException(400, "invalid tag name")
    return {"name": name}


class RenameBody(BaseModel):
    name: str


@app.patch("/api/tags/{name:path}")
def rename_tag(name: str, body: RenameBody) -> dict:
    """Rename a tag everywhere, including its whole subtree."""
    conn = _conn()
    return db.rename_tag(conn, name, body.name)


class DescribeBody(BaseModel):
    description: str


@app.put("/api/tags/{name:path}/description")
def describe_tag(name: str, body: DescribeBody) -> dict:
    """Describe a branch so the agent can auto-file matching papers into it."""
    conn = _conn()
    db.set_tag_description(conn, name, body.description)
    return {"name": db.normalize_tag(name), "description": body.description.strip()}


@app.post("/api/organization/clear")
def clear_organization(keep_custom: bool = True) -> dict:
    """Wipe the tag organisation, leaving items, BibTeX, notes and status intact."""
    conn = _conn()
    return db.clear_organization(conn, keep_custom=keep_custom)


@app.get("/api/organization/banned")
def banned_tags() -> dict:
    """Tags the user deleted -- the agent will not recreate these."""
    conn = _conn()
    return {"banned": sorted(db.banned_tags(conn))}


@app.delete("/api/organization/banned/{name:path}")
def unban(name: str) -> dict:
    """Lift a ban, so the agent may use this tag again."""
    conn = _conn()
    db.unban_tag(conn, name)
    return {"unbanned": db.normalize_tag(name)}


@app.delete("/api/tags/{name:path}")
def delete_tag(name: str, with_children: bool = False) -> dict:
    conn = _conn()
    return {"removed": db.delete_tag(conn, name, with_children=with_children)}


class MergeTagsBody(BaseModel):
    sources: list[str]
    target: str


@app.post("/api/tags/merge")
def merge_tags(body: MergeTagsBody) -> dict:
    conn = _conn()
    return {"moved": db.merge_tags(conn, body.sources, body.target)}


# --- triage, duplicates, ask ------------------------------------------------


@app.get("/api/triage")
def triage(days: int = 30) -> dict:
    conn = _conn()
    try:
        tabs = capture.stale_tabs(conn, days)
    except Exception as exc:
        return {"tabs": [], "error": str(exc), "days": days}
    return {"tabs": [asdict(t) for t in tabs], "error": None, "days": days}


class CloseBody(BaseModel):
    urls: list[str]


@app.post("/api/triage/close")
def triage_close(body: CloseBody) -> dict:
    from . import chrome

    return {"closed": chrome.close_tabs(body.urls)}


@app.get("/api/dupes")
def dupes(threshold: float = 0.93) -> dict:
    conn = _conn()
    groups = organize_mod.find_duplicates(conn, threshold)
    return {
        "groups": [
            [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "source": r["source"],
                    "canonical_url": r["canonical_url"],
                }
                for r in group
            ]
            for group in groups
        ]
    }


@app.post("/api/dupes/merge")
def merge_dupes(threshold: float = 0.93) -> dict:
    conn = _conn()
    return organize_mod.merge_all(conn, threshold)


class AskBody(BaseModel):
    question: str
    limit: int = 24


@app.post("/api/ask")
def api_ask(body: AskBody) -> dict:
    conn = _conn()
    try:
        backend = get_backend(purpose="ask")
    except Exception as exc:
        raise HTTPException(400, f"no model backend configured: {exc}")
    try:
        answer = ask_mod.ask(conn, backend, body.question, limit=body.limit)
    except Exception as exc:
        raise HTTPException(500, str(exc))
    return {
        "question": answer.question,
        "answer": answer.answer,
        "retrieved": answer.retrieved,
        "items": [
            {"id": i["id"], "title": i["title"], "canonical_url": i["canonical_url"]}
            for i in answer.items
        ],
    }


# --- long-running jobs ------------------------------------------------------
#
# Ingest, organize and backfill take minutes. The UI starts one and polls, so
# the browser never holds an open request for the whole run.


@dataclass
class Job:
    id: str
    kind: str
    state: str = "running"
    log: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: str = ""


JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()


def _run_job(job: Job, fn) -> None:
    def log(message: str) -> None:
        with _JOBS_LOCK:
            job.log.append(message)

    try:
        job.result = fn(log)
        job.state = "done"
    except Exception as exc:
        job.error = str(exc)
        job.state = "failed"


def _start(kind: str, fn) -> dict:
    job = Job(
        id=uuid.uuid4().hex[:12],
        kind=kind,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    JOBS[job.id] = job
    threading.Thread(target=_run_job, args=(job, fn), daemon=True).start()
    return {"job": job.id}


def _spawn_detached(kind: str) -> dict:
    """Start a long organize job in its own process so a server restart can't kill it."""
    from . import jobs

    job_id = uuid.uuid4().hex[:12]
    jobs.spawn(kind, job_id)
    return {"job": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    from . import jobs

    detached = jobs.status(job_id)  # detached jobs live in a status file, not memory
    if detached is not None:
        return detached
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return asdict(job)


@app.post("/api/jobs/ingest")
def job_ingest(include_private: bool = Body(False, embed=True)) -> dict:
    def run(log):
        with db.session() as conn:
            report = ingest_mod.ingest(conn, include_private=include_private, progress=log)
            return {
                "created": report.created,
                "updated": report.updated,
                "recovered": report.unloaded_recovered,
                "skipped": dict(report.skipped),
            }

    return _start("ingest", run)


@app.post("/api/jobs/backfill")
def job_backfill() -> dict:
    def run(log):
        with db.session() as conn:
            # Three repairs in one pass: fill items that never had metadata, fix the
            # ones whose title is really the site's chrome (a CVF tab reads "Open
            # Access Repository" for every paper), and re-derive identities so
            # arXiv-DOI twins fold together.
            attempted, repaired = ingest_mod.backfill(conn, progress=log)
            examined, retitled = ingest_mod.repair_titles(conn, progress=log)
            ident = organize_mod.recanonicalize_all(conn, progress=log)
            facets = ingest_mod.apply_facets(conn, progress=log)
            tidy = organize_mod.cleanup_taxonomy(conn, progress=log)
            log(f"repaired {repaired} missing, {retitled} wrong titles, "
                f"re-identified {ident['changed']}, merged {ident['merged']}, "
                f"added {facets} facets, tidied {len(tidy['collapsed'])} branches")
            return {
                "attempted": attempted,
                "repaired": repaired,
                "retitled": retitled,
                "reidentified": ident["changed"],
                "merged": ident["merged"],
                "facets": facets,
                "collapsed": len(tidy["collapsed"]),
            }

    return _start("backfill", run)


# These three take minutes, so they run DETACHED (their own process) -- a server
# restart, crash or redeploy can no longer kill a build or a tag pass mid-run.
@app.post("/api/jobs/reorganize")
def job_reorganize() -> dict:
    """Full three-agent organize: propose concepts -> build the hierarchy -> tag every
    item. Resets model topic tags first; manual and computed tags are kept."""
    return _spawn_detached("reorganize")


@app.post("/api/jobs/build-vocab")
def job_build_vocab() -> dict:
    """Step 1: propose concepts and build the hierarchy, for review before tagging."""
    return _spawn_detached("build-vocab")


@app.post("/api/jobs/tag-all")
def job_tag_all() -> dict:
    """Step 2: tag every item into the reviewed vocabulary."""
    return _spawn_detached("tag-all")


@app.post("/api/jobs/tag-queue")
def job_tag_queue() -> dict:
    """Drain the tagging queue now instead of waiting for the poll timer: tag every
    newly-saved item into the existing vocabulary, in one batched pass."""
    return _spawn_detached("tag-queue")


@app.post("/api/jobs/fix-taxonomy")
def job_fix_taxonomy() -> dict:
    def run(log):
        with db.session() as conn:
            return organize_mod.fix_taxonomy(conn, get_backend(), progress=log)

    return _start("fix-taxonomy", run)


@app.post("/api/jobs/backup")
def job_backup() -> dict:
    def run(log):
        with db.session() as conn:
            result = backup_mod.configured_backup(conn)  # honours the chosen destination
            log(f"wrote {result.path}" + (f" · {result.cloud}" if result.cloud else ""))
            return {
                "path": str(result.path),
                "bytes": result.bytes_written,
                "items": result.items,
                "cloud": result.cloud,
            }

    return _start("backup", run)


# --- static front end -------------------------------------------------------

if (DIST / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        """Serve the SPA, letting the client router own every non-API route."""
        candidate = DIST / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        # Asset filenames are content-hashed and safe to cache forever, but the
        # shell must never be: a cached index.html points at a bundle that a
        # rebuild has already replaced, which renders as a blank page.
        return FileResponse(
            DIST / "index.html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

else:

    @app.get("/{path:path}")
    def not_built(path: str):
        return JSONResponse(
            {
                "error": "the UI has not been built",
                "fix": "cd ui && npm install && npm run build",
            },
            status_code=503,
        )
