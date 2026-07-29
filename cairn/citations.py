"""Related-in-your-library, from shared references.

Two papers are related when they cite the same work. We never parse a PDF for
this: Semantic Scholar returns a paper's reference list keyed by a stable id in
one call, so a paper's "fingerprint" is just that set of ids. Related items are
then a set intersection over fingerprints -- cheap, and computed on demand
against whatever is already cached, so there is never a giant bulk fetch.

Populating a fingerprint costs one network call and is opt-in (the
`related_citations` setting), lazily filled the first time you open a paper.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.parse

from . import db, meta
from .canonical import source_of

_MIN_SHARED = 3  # fewer shared references than this is noise, not a relationship


def fingerprint(item: sqlite3.Row | dict) -> list[str] | None:
    """The set of reference ids for a paper, from Semantic Scholar. None on miss."""
    source, source_id = source_of(item["canonical_url"])
    if source == "arxiv" and source_id:
        key = f"ARXIV:{source_id}"
    elif source == "doi" and source_id:
        key = source_id
    else:
        title = (item["title"] or "").strip()
        if not title:
            return None
        key = None  # fall back to a title search below

    try:
        if key:
            data = json.loads(
                meta._get(
                    f"https://api.semanticscholar.org/graph/v1/paper/{key}"
                    "?fields=references.paperId",
                    accept="application/json",
                )
            )
        else:
            found = json.loads(
                meta._get(
                    "https://api.semanticscholar.org/graph/v1/paper/search?"
                    + urllib.parse.urlencode({"query": item["title"], "limit": 1, "fields": "paperId"}),
                    accept="application/json",
                )
            )
            hits = found.get("data") or []
            if not hits:
                return None
            data = json.loads(
                meta._get(
                    f"https://api.semanticscholar.org/graph/v1/paper/{hits[0]['paperId']}"
                    "?fields=references.paperId",
                    accept="application/json",
                )
            )
    except (urllib.error.URLError, OSError, json.JSONDecodeError, meta.MetaError):
        return None

    refs = [
        r["paperId"]
        for r in (data.get("references") or [])
        if isinstance(r, dict) and r.get("paperId")
    ]
    return refs


def ensure_fingerprint(conn: sqlite3.Connection, item_id: int) -> list[str]:
    """Return an item's cached reference ids, fetching and storing them once."""
    row = db.get_item(conn, item_id)
    if row is None:
        return []
    if row["ref_ids"] is not None:
        try:
            return json.loads(row["ref_ids"])
        except json.JSONDecodeError:
            return []
    refs = fingerprint(row)
    # Store even an empty list, as "" -> [], so we do not re-fetch a paper S2
    # has no references for on every open.
    conn.execute(
        "UPDATE items SET ref_ids = ? WHERE id = ?",
        (json.dumps(refs or []), item_id),
    )
    conn.commit()
    return refs or []


def related(
    conn: sqlite3.Connection, item_id: int, limit: int = 8, ensure: bool = False
) -> list[dict]:
    """Items in the library that share the most references with this one.

    Cached-only by default: reads whatever fingerprint is already stored and
    never touches the network, so opening a paper is instant. Pass ensure=True
    (from a background job) to fetch a missing fingerprint first.
    """
    if ensure:
        mine = set(ensure_fingerprint(conn, item_id))
    else:
        row = db.get_item(conn, item_id)
        try:
            mine = set(json.loads(row["ref_ids"])) if row and row["ref_ids"] else set()
        except (json.JSONDecodeError, TypeError):
            mine = set()
    if not mine:
        return []

    scored: list[tuple[int, sqlite3.Row]] = []
    for row in conn.execute(
        "SELECT id, title, canonical_url, source, venue, year, ref_ids FROM items "
        "WHERE ref_ids IS NOT NULL AND id != ?",
        (item_id,),
    ):
        try:
            theirs = set(json.loads(row["ref_ids"]))
        except (json.JSONDecodeError, TypeError):
            continue
        shared = len(mine & theirs)
        if shared >= _MIN_SHARED:
            scored.append((shared, row))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "canonical_url": row["canonical_url"],
            "source": row["source"],
            "venue": row["venue"],
            "year": row["year"],
            "shared": shared,
        }
        for shared, row in scored[:limit]
    ]
