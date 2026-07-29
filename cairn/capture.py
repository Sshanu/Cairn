"""Capture and triage: the operations shared by the CLI, the poller and the UI.

Not in the module map in doc.md section 3, but `stale_tabs()` is named in the web
UI brief and the save path is needed by more than one surface, so both live here
rather than inside cli.py.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import buckets, chrome, config, db, meta
from .canonical import canonicalize, source_of


@dataclass
class Saved:
    item_id: int
    created: bool
    canonical_url: str
    title: str | None
    meta_error: str | None = None


@dataclass
class StaleTab:
    canonical_url: str
    raw_url: str
    title: str | None
    first_seen: str | None
    age_days: int | None


def save_url(
    conn: sqlite3.Connection,
    raw_url: str,
    *,
    title: str | None = None,
    abstract: str | None = None,
    authors: str | None = None,
    tags: tuple[str, ...] = (),
    fetch_meta: bool = True,
    include_private: bool = False,
) -> Saved | None:
    """Explicit save. Returns None for blocked or unusable URLs.

    `abstract`/`authors` are a floor scraped from the loaded page by the browser
    extension. The server can't fetch bot-protected sites (OpenReview's Cloudflare
    wall), but the browser already rendered them -- so these fill in what resolve()
    can't reach, while resolve() still overrides them whenever it CAN fetch.
    """
    canonical = canonicalize(raw_url)
    if not canonical or not canonical.startswith("http"):
        return None

    # The same rules the bulk ingest uses. Without this the hotkey is a hole in
    # the never-list: it would happily file a YouTube video, a ChatGPT
    # transcript, or this tool's own localhost UI.
    decision = buckets.classify(raw_url, include_private=include_private)
    if not decision.stored:
        return None

    # A tab title that is really the URL (an unloaded tab, or a PDF whose tab title
    # is the file URL -- e.g. arxiv.org/pdf/2403.13164) or is blank must never be
    # stored, nor overwrite a real title on a re-save. Drop it here so resolve() below
    # (or a later repair pass) supplies the real one, for arXiv and every other source.
    from .ingest import is_placeholder_title

    if title and is_placeholder_title(title, raw_url):
        title = None

    # Source identity is derivable from the URL alone, so it is filled in even
    # when metadata lookup is skipped. The tab's own title is the floor;
    # resolve() only ever overwrites it with something better, because it drops
    # its empty fields before returning.
    source, source_id = source_of(canonical)
    fields: dict = {
        "raw_url": raw_url,
        "title": title,
        # Floor from the loaded page (extension). resolve() overwrites when it can.
        "abstract": abstract or None,
        "authors": authors or None,
        "source": source,
        "source_id": source_id,
        "kind": "blog" if source == "web" else "paper",
        "bucket": decision.bucket,
    }
    meta_error = None
    if fetch_meta:
        resolved = meta.resolve(canonical, raw_url)
        meta_error = resolved.pop("_error", None)
        fields.update(resolved)

    item_id, created = db.upsert_item(conn, canonical, **fields)
    if tags:
        db.add_tags(conn, item_id, tags, origin="manual")

    item = db.get_item(conn, item_id)
    return Saved(
        item_id, created, canonical, item["title"] if item else title, meta_error
    )


def poll(conn: sqlite3.Connection) -> int:
    """One ledger tick: title and URL only, no page content, no network."""
    if not config.ledger_enabled():
        return 0
    entries = []
    for tab in chrome.all_tabs():
        url = tab.get("url") or ""
        canonical = canonicalize(url)
        if not canonical.startswith("http") or config.is_blocked(canonical):
            continue
        entries.append(
            {"canonical_url": canonical, "raw_url": url, "title": tab.get("title")}
        )
    count = db.record_ledger(conn, entries)
    db.purge_ledger(conn, config.ledger_days())
    return count


def stale_tabs(conn: sqlite3.Connection, days: int = 30) -> list[StaleTab]:
    """Open tabs carried longer than `days` that are not in the library.

    Joins the live tab list against the ledger, so it degrades gracefully when
    CAIRN_LEDGER=0: unknown tabs simply have no age and are filtered out.
    """
    out: list[StaleTab] = []
    seen: set[str] = set()

    for tab in chrome.all_tabs():
        url = tab.get("url") or ""
        canonical = canonicalize(url)
        if not canonical.startswith("http") or canonical in seen:
            continue
        if config.is_blocked(canonical):
            continue
        seen.add(canonical)

        if db.get_item_by_url(conn, canonical) is not None:
            continue  # already saved; closing it costs nothing

        first_seen = db.ledger_first_seen(conn, canonical)
        age = db.age_days(first_seen)
        if age is None or age < days:
            continue

        out.append(
            StaleTab(
                canonical_url=canonical,
                raw_url=url,
                title=tab.get("title"),
                first_seen=first_seen,
                age_days=age,
            )
        )

    out.sort(key=lambda t: t.age_days or 0, reverse=True)
    return out
