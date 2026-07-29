"""Import a Zotero library, read-only.

Zotero keeps everything in one SQLite file. We never open the original: it is
copied first, then read through a `mode=ro` URI, so a running Zotero cannot be
disturbed and nothing can be written back by accident.

Two things come across that nothing else can supply:

  dateAdded    when you actually filed the paper, which becomes first_seen --
               far better than "today", and it is what the age spine reads.
  collections  your hand-made folder tree, imported as `zotero/<path>` tags, so
               years of manual organisation is preserved rather than re-derived.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import buckets, db
from .canonical import canonicalize, source_of

# Where Zotero keeps its data, in order of likelihood.
CANDIDATE_DIRS = (
    Path.home() / "Zotero",
    Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Documents/Zotero",
    Path.home() / "Documents/Zotero",
)

# Item types that are readings. Attachments, notes and annotations are children
# of these and would otherwise import as hundreds of untitled rows.
PAPER_TYPES = frozenset(
    {
        "journalArticle", "conferencePaper", "preprint", "bookSection", "book",
        "thesis", "report", "manuscript", "webpage", "blogPost", "presentation",
        "computerProgram", "dataset", "document",
    }
)

WANTED_FIELDS = frozenset(
    {
        "title", "abstractNote", "DOI", "url", "date", "publicationTitle",
        "proceedingsTitle", "bookTitle", "repository", "archiveID",
    }
)


@dataclass
class ZoteroItem:
    key: str
    item_type: str
    fields: dict[str, str]
    creators: list[str]
    collections: list[str]
    date_added: str | None = None

    @property
    def title(self) -> str | None:
        return self.fields.get("title")

    @property
    def year(self) -> int | None:
        raw = self.fields.get("date") or ""
        for chunk in raw.replace("/", "-").split("-"):
            if len(chunk) == 4 and chunk.isdigit():
                return int(chunk)
        return None

    @property
    def venue(self) -> str | None:
        return (
            self.fields.get("proceedingsTitle")
            or self.fields.get("publicationTitle")
            or self.fields.get("bookTitle")
            or self.fields.get("repository")
        )

    def best_url(self) -> str | None:
        """Prefer a DOI, then arXiv, then whatever URL was saved.

        A DOI is stable and canonicalizes to the same identity cairn already
        uses, so a paper imported here merges with the same paper captured from
        Chrome instead of becoming a duplicate.
        """
        doi = (self.fields.get("DOI") or "").strip()
        if doi:
            return f"https://doi.org/{doi.removeprefix('https://doi.org/')}"

        archive = (self.fields.get("archiveID") or "").strip()
        if archive.lower().startswith("arxiv:"):
            return f"https://arxiv.org/abs/{archive.split(':', 1)[1]}"

        url = (self.fields.get("url") or "").strip()
        return url or None


@dataclass
class ImportReport:
    seen: int = 0
    created: int = 0
    merged: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    collections_applied: int = 0

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def find_database() -> Path | None:
    for directory in CANDIDATE_DIRS:
        candidate = directory / "zotero.sqlite"
        if candidate.is_file():
            return candidate
    return None


def _open_readonly(path: Path) -> tuple[sqlite3.Connection, Path]:
    """Copy, then open read-only. Zotero may be running and holding a lock."""
    tmp = Path(tempfile.mkdtemp(prefix="cairn-zotero-")) / "zotero.sqlite"
    shutil.copy2(path, tmp)
    conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn, tmp


def _collection_paths(conn: sqlite3.Connection) -> dict[int, str]:
    """Full slash-separated path for every collection, e.g. `VLM/Hallucination`."""
    rows = list(conn.execute("SELECT collectionID, collectionName, parentCollectionID FROM collections"))
    by_id = {r["collectionID"]: r for r in rows}

    def path_of(cid: int, seen: frozenset[int] = frozenset()) -> str:
        row = by_id.get(cid)
        if row is None or cid in seen:
            return ""
        parent = row["parentCollectionID"]
        prefix = path_of(parent, seen | {cid}) if parent else ""
        name = row["collectionName"]
        return f"{prefix}/{name}" if prefix else name

    return {cid: path_of(cid) for cid in by_id}


def read_items(path: Path) -> list[ZoteroItem]:
    conn, tmp = _open_readonly(path)
    try:
        trashed = {r["itemID"] for r in conn.execute("SELECT itemID FROM deletedItems")}
        paths = _collection_paths(conn)

        collections: dict[int, list[str]] = {}
        for row in conn.execute("SELECT collectionID, itemID FROM collectionItems"):
            name = paths.get(row["collectionID"])
            if name:
                collections.setdefault(row["itemID"], []).append(name)

        creators: dict[int, list[str]] = {}
        for row in conn.execute(
            """SELECT ic.itemID, c.firstName, c.lastName FROM itemCreators ic
               JOIN creators c ON c.creatorID = ic.creatorID
               ORDER BY ic.itemID, ic.orderIndex"""
        ):
            name = " ".join(filter(None, [row["firstName"], row["lastName"]])).strip()
            if name:
                creators.setdefault(row["itemID"], []).append(name)

        fields: dict[int, dict[str, str]] = {}
        for row in conn.execute(
            """SELECT id.itemID, f.fieldName, v.value FROM itemData id
               JOIN fields f ON f.fieldID = id.fieldID
               JOIN itemDataValues v ON v.valueID = id.valueID"""
        ):
            if row["fieldName"] in WANTED_FIELDS:
                fields.setdefault(row["itemID"], {})[row["fieldName"]] = str(row["value"])

        out = []
        for row in conn.execute(
            """SELECT i.itemID, i.key, i.dateAdded, it.typeName FROM items i
               JOIN itemTypes it ON it.itemTypeID = i.itemTypeID"""
        ):
            if row["itemID"] in trashed or row["typeName"] not in PAPER_TYPES:
                continue
            out.append(
                ZoteroItem(
                    key=row["key"],
                    item_type=row["typeName"],
                    fields=fields.get(row["itemID"], {}),
                    creators=creators.get(row["itemID"], []),
                    collections=collections.get(row["itemID"], []),
                    date_added=row["dateAdded"],
                )
            )
        return out
    finally:
        conn.close()
        shutil.rmtree(tmp.parent, ignore_errors=True)


def collection_tag(path: str) -> str:
    """`VLM/Hallucination` -> `zotero/vlm/hallucination`.

    Kept in its own facet rather than merged into `topic/`: this is what you
    filed by hand, and it should stay distinguishable from what a model decided.
    """
    parts = [db.normalize_tag(p) for p in path.split("/") if p.strip()]
    return "zotero/" + "/".join(p for p in parts if p)


def import_library(
    conn: sqlite3.Connection,
    path: Path | None = None,
    *,
    limit: int | None = None,
    include_collections: bool = True,
    progress=None,
) -> ImportReport:
    path = path or find_database()
    report = ImportReport()
    if path is None:
        raise FileNotFoundError("no zotero.sqlite found")

    items = read_items(path)
    if limit:
        items = items[:limit]
    if progress:
        progress(f"{len(items)} readings in Zotero")

    for entry in items:
        report.seen += 1
        url = entry.best_url()
        if not url:
            report.skip("no DOI or URL")
            continue

        decision = buckets.classify(url)
        if not decision.stored:
            report.skip(decision.reason)
            continue

        canonical = canonicalize(url)
        if not canonical.startswith("http"):
            report.skip("unusable URL")
            continue

        source, source_id = source_of(canonical)
        existed = db.get_item_by_url(conn, canonical) is not None

        item_id, created = db.upsert_item(
            conn,
            canonical,
            raw_url=url,
            title=entry.title,
            abstract=entry.fields.get("abstractNote"),
            authors=", ".join(entry.creators) or None,
            venue=entry.venue,
            year=entry.year,
            source=source,
            source_id=source_id,
            kind="paper",
            bucket=decision.bucket,
            # Zotero knows when you filed it; that is the real first-seen date.
            first_seen=_iso(entry.date_added),
        )
        report.created += 1 if created else 0
        report.merged += 0 if created else 1

        if include_collections and entry.collections:
            tags = [collection_tag(c) for c in entry.collections]
            applied = db.add_tags(conn, item_id, [t for t in tags if t != "zotero"], origin="manual")
            report.collections_applied += len(applied)

        if progress and report.seen % 250 == 0:
            progress(f"{report.seen}/{len(items)}")

    return report


def _iso(date_added: str | None) -> str | None:
    """Zotero stores UTC as `2024-03-01 12:00:00`; cairn wants ISO-8601."""
    if not date_added:
        return None
    text = date_added.strip().replace(" ", "T")
    return text + "+00:00" if len(text) == 19 else text
