"""Bulk ingest: turn every open tab into a library item, accurately.

The doc's capture path is one tab at a time. Organising a browser that already
holds hundreds of tabs needs a different shape: batch the metadata lookups,
fetch concurrently, and be explicit about what is deliberately skipped.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import buckets, chrome, db, meta
from .canonical import canonicalize, is_paper, source_of


def is_placeholder_title(title: str | None, url: str) -> bool:
    """An unloaded tab often carries the URL (or nothing) as its title.

    Chrome reports a discarded tab's URL faithfully but its title may never have
    been set, so a placeholder must not be allowed to beat real metadata fetched
    over HTTP.
    """
    text = (title or "").strip()
    if not text:
        return True
    host = (urlsplit(url).hostname or "").lower()
    normalized = text.lower().rstrip("/")
    if normalized in (url.lower().rstrip("/"), host, host.removeprefix("www.")):
        return True
    # "arxiv.org/abs/2401.12345" or a bare filename: no words, just a locator.
    if "/" in text and " " not in text:
        return True
    return text.lower() in ("untitled", "new tab", "loading...", "...")


@dataclass
class IngestReport:
    created: int = 0
    updated: int = 0
    skipped: Counter = field(default_factory=Counter)
    enriched_meta: int = 0
    unloaded_recovered: int = 0
    needs_title: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def total_saved(self) -> int:
        return self.created + self.updated


def skip_reason(url: str, title: str | None = None, include_private: bool = False) -> str | None:
    """Why this tab is not stored at all, or None if it belongs somewhere."""
    decision = buckets.classify(url, include_private=include_private)
    return None if decision.stored else decision.reason


def collect_tabs(include_private: bool = False) -> tuple[list[dict], Counter]:
    """Read Chrome, canonicalize, dedupe and filter. Returns (keep, skipped)."""
    skipped: Counter = Counter()
    keep: dict[str, dict] = {}

    for tab in chrome.all_tabs():
        url = tab.get("url") or ""
        decision = buckets.classify(url, include_private=include_private)
        if not decision.stored:
            skipped[decision.reason] += 1
            continue
        canonical = canonicalize(url)
        title = tab.get("title")
        if is_placeholder_title(title, url):
            title = None  # unloaded tab: let the HTTP fetch supply the title

        # First tab wins; later duplicates only contribute a title if missing.
        entry = keep.setdefault(
            canonical,
            {
                "canonical_url": canonical,
                "raw_url": url,
                "title": title,
                "bucket": decision.bucket,
                "tabs": 0,
            },
        )
        entry["tabs"] += 1
        if not entry["title"]:
            entry["title"] = title

    return list(keep.values()), skipped


def fetch_metadata(entries: list[dict], workers: int = 8) -> int:
    """Fill in title/authors/abstract, batching arXiv and parallelising the rest."""
    resolved = 0

    arxiv_ids = {}
    others = []
    for entry in entries:
        source, source_id = source_of(entry["canonical_url"])
        entry["source"] = source
        entry["source_id"] = source_id
        entry["kind"] = "paper" if is_paper(source) else "blog"
        if source == "arxiv" and source_id:
            arxiv_ids[source_id] = entry
        elif source == "openreview" and entry.get("title"):
            # API is behind bot-protection, and a loaded forum tab already
            # carries the paper title. Only pay for a request when we have none.
            continue
        else:
            others.append(entry)

    if arxiv_ids:
        for source_id, fields in meta.arxiv_batch(list(arxiv_ids)).items():
            entry = arxiv_ids.get(source_id)
            if entry:
                entry.update({k: v for k, v in fields.items() if v})
                resolved += 1

    def resolve_one(entry: dict) -> bool:
        try:
            fields = meta.resolve(
                entry["canonical_url"], entry.get("raw_url"), fetch_body=False
            )
        except Exception:
            return False
        fields.pop("_error", None)
        # Never let a failed lookup blank out the tab's own title.
        entry.update({k: v for k, v in fields.items() if v})
        return bool(fields.get("title"))

    if others:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            resolved += sum(1 for ok in pool.map(resolve_one, others) if ok)

    return resolved


def recategorize(
    conn: sqlite3.Connection, *, include_private: bool = False, purge: bool = False
) -> dict:
    """Re-apply the bucket rules to items already in the database.

    Rules change -- a host gets added to the never-list, or a documents site is
    recognised for the first time. Existing rows should follow, and anything now
    on the never-list should be removable, since the point of that list is that
    those pages are not stored.
    """
    moved: Counter = Counter()
    removals: list[tuple[int, str]] = []

    for row in conn.execute("SELECT id, canonical_url, bucket FROM items"):
        decision = buckets.classify(row["canonical_url"], include_private=include_private)
        if not decision.stored:
            removals.append((row["id"], decision.reason))
            continue
        if decision.bucket != (row["bucket"] or "library"):
            conn.execute(
                "UPDATE items SET bucket = ? WHERE id = ?", (decision.bucket, row["id"])
            )
            moved[f"{row['bucket'] or 'library'} -> {decision.bucket}"] += 1
    conn.commit()

    purged: Counter = Counter()
    if purge:
        for item_id, reason in removals:
            conn.execute("DELETE FROM item_tags WHERE item_id = ?", (item_id,))
            conn.execute("DELETE FROM search_index WHERE item_id = ?", (str(item_id),))
            conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
            purged[reason] += 1
        conn.commit()

    return {
        "moved": dict(moved),
        "removable": Counter(reason for _id, reason in removals),
        "purged": dict(purged),
    }


# A stored title that is really the site's chrome, not the paper's. These come
# from a tab captured before its metadata resolved -- the browser tab for a CVF
# page says "CVPR 2025 Open Access Repository" for every paper on the site, which
# then makes ten distinct papers look like one duplicate.
GENERIC_TITLE_MARKERS = (
    "open access repository",
    "openreview",
    "verifying your browser",
    "just a moment",
    "papers with code",
    "google search",
    "redirecting",
    "access denied",
    "are you a robot",
    "attention required",
    "page not found",
)


def looks_generic(title: str | None) -> bool:
    text = (title or "").strip().lower()
    return not text or any(marker in text for marker in GENERIC_TITLE_MARKERS)


# A tab title is often the real title plus a site suffix: "DoRA: ... | OpenReview".
# Stripping that recovers the title with no network at all -- the reliable fix for
# a bot-protected host like OpenReview, where a re-fetch can never succeed.
_SITE_SUFFIX = re.compile(
    r"\s*[|–—-]\s*(openreview(\.net)?|arxiv(\.org)?|papers\s*with\s*code|"
    r"proceedings.*|neurips.*|semantic\s*scholar|acl\s*anthology|github|"
    r"cvpr\s*\d{4}\s*open\s*access\s*repository)\s*$",
    re.IGNORECASE,
)


def strip_site_suffix(title: str | None) -> str:
    text = (title or "").strip()
    prev = None
    while text and text != prev:
        prev = text
        text = _SITE_SUFFIX.sub("", text).strip()
    return text


def apply_facets(conn: sqlite3.Connection, *, progress=None) -> int:
    """Attach the URL-derived facets (type/, venue/, site/) every item should carry.

    These lapsed out of the pipeline once, leaving papers with a written venue
    but no `venue/` tag to browse them by. This recomputes them for every row and
    adds whatever is missing, so venue organisation is complete and stays that
    way. Idempotent: a tag already present is skipped.
    """
    from . import taxonomy

    rows = conn.execute("SELECT id, canonical_url, source, venue, year FROM items").fetchall()
    added = fixed = 0
    for row in rows:
        tags = taxonomy.computed_facets(
            row["canonical_url"], row["source"], row["venue"], row["year"]
        )
        existing = set(db.item_tags(conn, row["id"]))
        fresh = [t for t in tags if t not in existing]
        if fresh:
            db.add_tags(conn, row["id"], fresh, origin="model")
            added += len(fresh)
        # site/ and type/ are computed strictly from the URL, so anything the agent
        # wrote there (a site/github from a link in the abstract, a second type/other
        # on a paper) is wrong -- remove any that the fresh computation doesn't justify.
        # venue/ is left add-only: computing it can legitimately come up empty and we
        # must never drop a real venue.
        computed = set(tags)
        for t in existing:
            if (t.startswith("site/") or t.startswith("type/")) and t not in computed:
                db.remove_tag_from_item(conn, row["id"], t)
                fixed += 1
    # A site branch is only worth keeping for a recurring domain, so drop the
    # one-off ones every time -- otherwise each new single-domain tab leaves a
    # stray site/ tag behind.
    removed = db.remove_thin_site_tags(conn)
    if progress:
        progress(
            f"applied {added} computed facets; removed {fixed} wrong site/type tags; "
            f"pruned {removed} one-off site tags"
        )
    return added


def repair_titles(
    conn: sqlite3.Connection,
    *,
    workers: int = 6,
    limit: int | None = None,
    progress=None,
) -> tuple[int, int]:
    """Re-resolve items whose stored title is really the site's chrome.

    Unlike backfill, this overwrites a title that is present but wrong -- the
    common case for a conference tab captured before its page metadata loaded.
    A re-fetch that comes back empty (a site behind bot-protection) leaves the
    row untouched rather than replacing a bad title with a blank one.

    Returns (examined, fixed).
    """
    # Two kinds of bad title: a generic one ("Untitled", "... | OpenReview"), and one
    # that is really the URL itself (a PDF or unloaded tab). looks_generic sees only
    # the text, so it misses the URL case; is_placeholder_title compares against the
    # URL and catches it. Repair both, for arXiv and every conference/source alike.
    rows = [
        row
        for row in conn.execute("SELECT * FROM items ORDER BY id")
        if looks_generic(row["title"]) or is_placeholder_title(row["title"], row["raw_url"])
    ]
    if limit:
        rows = rows[:limit]
    if not rows:
        return 0, 0

    # Phase 1, no network: a title that is really "Real Title | OpenReview" just
    # needs the suffix removed. This alone fixes the whole bot-protected slice and
    # cuts what has to be fetched (and thus the load on a rate-limited host).
    fixed = 0
    remaining = []
    for row in rows:
        stripped = strip_site_suffix(row["title"])
        if stripped and stripped != (row["title"] or "") and not looks_generic(stripped):
            db.set_fields(conn, row["id"], title=stripped)
            fixed += 1
        else:
            remaining.append(row)

    if not remaining:
        if progress:
            progress(f"recovered {fixed} titles by stripping site suffixes")
        return len(rows), fixed

    # Phase 2: fetch the rest (a conference page's real title lives in its meta).
    entries = [
        {"canonical_url": row["canonical_url"], "raw_url": row["raw_url"], "title": row["title"]}
        for row in remaining
    ]
    if progress:
        progress(f"stripped {fixed}; fetching {len(entries)} generic-titled items")
    fetch_metadata(entries, workers=workers)

    for row, entry in zip(remaining, entries):
        new_title = (entry.get("title") or "").strip()
        if not new_title or looks_generic(new_title) or new_title == row["title"]:
            continue
        fields = {
            key: entry.get(key)
            for key in ("title", "authors", "venue", "year", "abstract")
            if entry.get(key)
        }
        db.upsert_item(conn, row["canonical_url"], **fields)
        fixed += 1
    return len(rows), fixed


_MAX_TITLE_TRIES = 3


def heal_titles(conn: sqlite3.Connection, *, limit: int = 10, progress=None) -> int:
    """Fix a few bad titles per call -- the self-healing half of repair_titles, meant
    for the poll timer.

    Two guards keep it cheap and polite: it takes at most `limit` items per run (so a
    tick never scans/fetches the whole library), and it skips anything already tried
    `_MAX_TITLE_TRIES` times, incrementing the counter on every fetch. So a real title
    that failed to load once heals on the next tick, while one that can never be
    fetched (OpenReview's bot wall, a 404) is attempted a few times and then left
    alone -- not re-hammered every few minutes forever. Returns how many were fixed.
    """
    rows = [
        row
        for row in conn.execute(
            "SELECT * FROM items WHERE title_tries < ? ORDER BY title_tries, id",
            (_MAX_TITLE_TRIES,),
        )
        if looks_generic(row["title"]) or is_placeholder_title(row["title"], row["raw_url"])
    ][:limit]
    if not rows:
        return 0

    fixed = 0
    remaining = []
    # Phase 1 is free: a title that is really "Real Title | OpenReview" just needs the
    # suffix stripped -- no fetch, no try spent.
    for row in rows:
        stripped = strip_site_suffix(row["title"])
        if stripped and stripped != (row["title"] or "") and not looks_generic(stripped):
            db.set_fields(conn, row["id"], title=stripped)
            fixed += 1
        else:
            remaining.append(row)

    # Phase 2 fetches. Every fetched row spends a try, so the unfetchable ones age out.
    if remaining:
        entries = [
            {"canonical_url": r["canonical_url"], "raw_url": r["raw_url"], "title": r["title"]}
            for r in remaining
        ]
        fetch_metadata(entries, workers=4)
        for row, entry in zip(remaining, entries):
            conn.execute(
                "UPDATE items SET title_tries = title_tries + 1 WHERE id = ?", (row["id"],)
            )
            new_title = (entry.get("title") or "").strip()
            if not new_title or looks_generic(new_title) or new_title == row["title"]:
                continue
            db.upsert_item(
                conn,
                row["canonical_url"],
                **{k: entry.get(k) for k in ("title", "authors", "venue", "year", "abstract") if entry.get(k)},
            )
            fixed += 1
        conn.commit()

    if progress:
        progress(f"healed {fixed} title(s) of {len(rows)} tried")
    return fixed


def backfill(
    conn: sqlite3.Connection,
    *,
    workers: int = 8,
    limit: int | None = None,
    force: bool = False,
    progress=None,
) -> tuple[int, int]:
    """Re-fetch metadata over HTTP for items that are missing it.

    A tab that was discarded when it was first seen has only a URL, and a site
    that was down or slow yields nothing. Both are recoverable later from the
    URL alone, so this is a standing repair pass rather than a one-off step of
    ingest: run it any time, as often as you like.

    Returns (attempted, repaired).
    """
    sql = "SELECT * FROM items"
    if not force:
        sql += " WHERE title IS NULL OR title = '' OR abstract IS NULL OR abstract = ''"
    sql += " ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"

    rows = list(conn.execute(sql))
    if not rows:
        return 0, 0

    entries = [
        {
            "canonical_url": row["canonical_url"],
            "raw_url": row["raw_url"],
            "title": row["title"],
        }
        for row in rows
    ]
    if progress:
        progress(f"re-fetching {len(entries)} items by URL")

    fetch_metadata(entries, workers=workers)

    repaired = 0
    for row, entry in zip(rows, entries):
        fields = {
            key: value
            for key, value in entry.items()
            if key not in ("canonical_url", "tabs", "raw_url") and value
        }
        if not fields:
            continue
        gained_title = bool(fields.get("title")) and not row["title"]
        gained_abstract = bool(fields.get("abstract")) and not row["abstract"]
        db.upsert_item(conn, row["canonical_url"], **fields)
        if gained_title or gained_abstract:
            repaired += 1

    return len(rows), repaired


def ingest(
    conn: sqlite3.Connection,
    *,
    include_private: bool = False,
    fetch_meta: bool = True,
    workers: int = 8,
    progress=None,
) -> IngestReport:
    report = IngestReport()
    entries, report.skipped = collect_tabs(include_private)
    unloaded = {e["canonical_url"] for e in entries if not e.get("title")}

    if progress:
        progress(f"{len(entries)} unique pages to ingest")
        if unloaded:
            progress(f"{len(unloaded)} have no usable tab title -- fetching by URL")

    if fetch_meta and entries:
        if progress:
            progress("fetching metadata...")
        report.enriched_meta = fetch_metadata(entries, workers=workers)

    for entry in entries:
        if entry["canonical_url"] in unloaded and entry.get("title"):
            report.unloaded_recovered += 1
        elif not entry.get("title"):
            report.needs_title.append(entry["canonical_url"])

    for entry in entries:
        # A URL the user deleted must not come back just because its tab is open.
        if db.is_dismissed(conn, entry["canonical_url"]):
            report.skipped["deleted"] += 1
            continue
        fields = {
            key: value
            for key, value in entry.items()
            if key not in ("canonical_url", "tabs") and value
        }
        try:
            _item_id, created = db.upsert_item(conn, entry["canonical_url"], **fields)
        except sqlite3.Error as exc:
            report.failures.append(f"{entry['canonical_url']}: {exc}")
            continue
        if created:
            report.created += 1
        else:
            report.updated += 1

    # The pile never forms: every item gets its URL-derived venue/type facets
    # immediately. This is deterministic and cheap -- no agent, always on.
    try:
        apply_facets(conn)
    except Exception as exc:  # facets are a nicety; ingest itself must not fail
        report.failures.append(f"facets: {exc}")

    # Agent tagging (topic/method/task) is opt-in and best-effort: a headless
    # ingest must still succeed if the backend is unset or the model is down.
    from . import config

    if config.auto_organize() and report.created:
        try:
            auto_organize_new(conn, progress=progress)
        except Exception as exc:
            report.failures.append(f"auto-organize: {exc}")

    return report


def auto_organize_new(conn: sqlite3.Connection, *, progress=None) -> int:
    """Tag freshly-ingested, still-unenriched items against the taxonomy.

    Kept small and resilient: it runs the enrichment agent over the handful of
    items a poll cycle adds, files them into described custom branches when that
    is switched on, and never raises past the caller's guard.
    """
    from . import config, enrich as enrich_mod
    from .backends.base import get_backend

    pending = db.unenriched(conn, limit=40)
    if not pending:
        return 0
    backend = get_backend()
    if progress:
        progress(f"auto-organizing {len(pending)} new items")
    results = enrich_mod.enrich_batch(conn, backend, limit=len(pending))
    tagged = sum(1 for r in results if getattr(r, "ok", False))

    if config.auto_file_projects():
        from . import projects

        for item in pending:
            try:
                projects.file_into_projects(conn, backend, item["id"])
            except Exception:
                continue

    if config.auto_summary():
        from . import summarize

        for item in pending:
            try:
                summarize.summarize_item(conn, backend, item["id"])
            except Exception:
                continue
    return tagged
