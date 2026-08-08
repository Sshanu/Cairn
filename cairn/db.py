"""Schema, ledger, library, tags, FTS5 index, search.

One SQLite file at ~/.cairn/cairn.db. Every surface is a client of it.
Nothing in this module calls a model or the network.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    canonical_url TEXT PRIMARY KEY,
    raw_url       TEXT,
    title         TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    times_seen    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    canonical_url TEXT UNIQUE NOT NULL,
    raw_url       TEXT,
    kind          TEXT,
    source        TEXT,
    source_id     TEXT,
    title         TEXT,
    authors       TEXT,
    venue         TEXT,
    year          INTEGER,
    abstract      TEXT,
    body          TEXT,
    summary       TEXT,
    status        TEXT NOT NULL DEFAULT 'unread',
    bucket        TEXT NOT NULL DEFAULT 'library',
    first_seen    TEXT,
    last_seen     TEXT,
    saved_at      TEXT NOT NULL,
    enriched_at   TEXT,
    notes         TEXT
);

-- URLs that were merged into another item. Keeps a re-ingest from recreating a
-- duplicate that was already resolved.
CREATE TABLE IF NOT EXISTS aliases (
    canonical_url TEXT PRIMARY KEY,
    item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    merged_at     TEXT NOT NULL
);

-- URLs the user deleted. A delete must STICK: without this, a still-open Chrome
-- tab is re-ingested on the next autosave and the item comes straight back.
CREATE TABLE IF NOT EXISTS dismissed (
    canonical_url TEXT PRIMARY KEY,
    dismissed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_tags (
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    origin     TEXT NOT NULL DEFAULT 'manual',
    confidence REAL,
    PRIMARY KEY (item_id, tag_id)
);

CREATE TABLE IF NOT EXISTS banned_tags (
    name      TEXT PRIMARY KEY,
    banned_at TEXT NOT NULL
);

-- Items saved (by the extension, autosave, or CLI) that still need a topic tag.
-- We never tag on the save path -- that would put a ~19s agent call in front of
-- every capture. Instead a save enqueues here, and a timer drains the whole queue
-- in one batched pass (see topics.drain_tag_queue). ON DELETE CASCADE so deleting
-- an item can never leave a dangling queue row.
CREATE TABLE IF NOT EXISTS tag_queue (
    item_id   INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    queued_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    title, venue, authors, abstract, body, tags,
    item_id UNINDEXED,
    tokenize='porter unicode61'
);

CREATE INDEX IF NOT EXISTS idx_items_status    ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_first_seen ON items(first_seen);
CREATE INDEX IF NOT EXISTS idx_ledger_last_seen ON ledger(last_seen);
"""

# bm25 takes one weight per column, including the UNINDEXED one.
# title, venue, authors, abstract, body, tags, item_id
_BM25_WEIGHTS = (10.0, 7.0, 4.0, 3.0, 1.0, 6.0, 0.0)

_MUTABLE_FIELDS = (
    "raw_url",
    "bucket",
    "kind",
    "source",
    "source_id",
    "title",
    "authors",
    "venue",
    "year",
    "abstract",
    "body",
    "summary",
    "status",
    "notes",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds"
    )


def age_days(timestamp: str | None) -> int | None:
    """Whole days between `timestamp` and now."""
    if not timestamp:
        return None
    try:
        then = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).days


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")  # wait, don't error, when another writer holds the lock
    conn.execute("PRAGMA foreign_keys=ON")
    # Keep the WAL SMALL. The default auto-checkpoint is 1000 pages (~4MB); at that size
    # every read has to scan the WAL (walFindFrame) and queries crawl -- this is what made
    # the whole UI drag once the WAL bloated. Check-point far more often, and sync only at
    # checkpoints (safe under WAL) so writes stay cheap.
    conn.execute("PRAGMA wal_autocheckpoint=200")  # ~800KB, keeps reads fast
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """A connection that always closes, for one-off / background work.

    `sqlite3`'s own `with conn:` only commits -- it does NOT close -- so a
    long-lived server that opens a connection per background job leaks a file
    handle every time. Use this anywhere outside the request path (jobs, worker
    threads) so the handle is released even if the body raises.
    """
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Additive migrations for databases created by an earlier version."""
    applied = []
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "bucket" not in columns:
        conn.execute(
            "ALTER TABLE items ADD COLUMN bucket TEXT NOT NULL DEFAULT 'library'"
        )
        applied.append("items.bucket")
    # Provenance for tags: pruning must be able to tell a name the model
    # proposed and never used from a branch the user made and has not filled.
    tag_cols = {r[1] for r in conn.execute("PRAGMA table_info(tags)")}
    if "origin" not in tag_cols:
        conn.execute(
            "ALTER TABLE tags ADD COLUMN origin TEXT NOT NULL DEFAULT 'model'"
        )
        applied.append("tags.origin")
    # A description on a custom branch tells the agent what belongs there, so it
    # can file new papers into your own hierarchies -- not just its own facets.
    if "description" not in tag_cols:
        conn.execute("ALTER TABLE tags ADD COLUMN description TEXT")
        applied.append("tags.description")

    # Cached BibTeX plus provenance: which source it came from, the venue it
    # cites, and whether that is the published version or an arXiv preprint.
    # `references` holds the paper's reference IDs (JSON), for related-in-library.
    for column, ddl in (
        ("bibtex", "bibtex TEXT"),
        ("bibtex_source", "bibtex_source TEXT"),
        ("bibtex_venue", "bibtex_venue TEXT"),
        ("bibtex_published", "bibtex_published INTEGER"),
        ("bibtex_at", "bibtex_at TEXT"),
        ("ref_ids", "ref_ids TEXT"),
        ("relevance", "relevance TEXT"),
        # A local static embedding of title+abstract, for topic discovery and
        # similarity-based tagging. Float32 bytes; see cairn/embed.py.
        ("embedding", "embedding BLOB"),
        # How many times the background self-heal has tried (and failed) to fetch a
        # real title. Caps retries so a permanently unfetchable one (OpenReview's bot
        # wall, a dead link) ages out instead of being re-hit on every poll.
        ("title_tries", "title_tries INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE items ADD COLUMN {ddl}")
            applied.append(f"items.{column}")

    # Created here rather than in SCHEMA: on an existing database the column
    # does not exist until the ALTER above has run.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_bucket ON items(bucket)")
    conn.commit()

    # The search index gained a `venue` column so a query like "LREC" finds a
    # paper whose venue is "LREC'08" even when the word is nowhere in the title.
    # FTS5 cannot ALTER in a new column, so recreate the index and refill it once.
    fts_cols = {r[1] for r in conn.execute("PRAGMA table_info(search_index)")}
    if fts_cols and "venue" not in fts_cols:
        conn.execute("DROP TABLE IF EXISTS search_index")
        conn.executescript(SCHEMA)
        rebuild_index(conn)
        applied.append("search_index.venue")

    return applied


# --- ledger -----------------------------------------------------------------


def record_ledger(conn: sqlite3.Connection, entries: Iterable[dict]) -> int:
    """Upsert observed tabs. Title and URL only -- no page content, no network."""
    now = utcnow()
    count = 0
    for entry in entries:
        canonical = entry.get("canonical_url")
        if not canonical:
            continue
        conn.execute(
            """
            INSERT INTO ledger (canonical_url, raw_url, title, first_seen, last_seen, times_seen)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(canonical_url) DO UPDATE SET
                last_seen  = excluded.last_seen,
                title      = COALESCE(NULLIF(excluded.title, ''), ledger.title),
                times_seen = ledger.times_seen + 1
            """,
            (canonical, entry.get("raw_url"), entry.get("title"), now, now),
        )
        count += 1
    conn.commit()
    return count


def purge_ledger(conn: sqlite3.Connection, days: int) -> int:
    """Drop rows for tabs not seen in `days`.

    Deliberately keyed on last_seen, not first_seen: a tab carried for 200 days
    is exactly the row the stale queue and the age spine need, so ageing it out
    on first_seen would delete the tool's most valuable data.
    """
    cutoff = days_ago(days)
    cursor = conn.execute("DELETE FROM ledger WHERE last_seen < ?", (cutoff,))
    conn.commit()
    return cursor.rowcount


def ledger_first_seen(conn: sqlite3.Connection, canonical_url: str) -> str | None:
    row = conn.execute(
        "SELECT first_seen FROM ledger WHERE canonical_url = ?", (canonical_url,)
    ).fetchone()
    return row["first_seen"] if row else None


# --- library ----------------------------------------------------------------


def upsert_item(conn: sqlite3.Connection, canonical_url: str, **fields: Any) -> tuple[int, bool]:
    """Insert or update a library item. Returns (item_id, created).

    A new item inherits its first_seen from the ledger, so a paper saved in July
    that the poller saw in May is stamped May.
    """
    now = utcnow()
    # A URL already merged into another item must land on that item, or the
    # duplicate comes straight back on the next ingest.
    alias = conn.execute(
        "SELECT item_id FROM aliases WHERE canonical_url = ?", (canonical_url,)
    ).fetchone()
    if alias:
        canonical_url = conn.execute(
            "SELECT canonical_url FROM items WHERE id = ?", (alias["item_id"],)
        ).fetchone()["canonical_url"]

    row = conn.execute(
        "SELECT id FROM items WHERE canonical_url = ?", (canonical_url,)
    ).fetchone()

    updates = {
        key: value
        for key, value in fields.items()
        if key in _MUTABLE_FIELDS and value not in (None, "")
    }

    if row:
        item_id = row["id"]
        updates["last_seen"] = now
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE items SET {assignments} WHERE id = ?",
            (*updates.values(), item_id),
        )
        conn.commit()
        reindex_item(conn, item_id)
        return item_id, False

    first_seen = fields.get("first_seen") or ledger_first_seen(conn, canonical_url) or now
    columns = {
        "canonical_url": canonical_url,
        "first_seen": first_seen,
        "last_seen": now,
        "saved_at": now,
        "status": fields.get("status") or "unread",
        **updates,
    }
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO items ({', '.join(columns)}) VALUES ({placeholders})",
        tuple(columns.values()),
    )
    # An explicit (re-)create is the user changing their mind: lift any dismissal so
    # this URL is tracked again. Ingest skips dismissed URLs before reaching here, so
    # only a deliberate save clears it.
    conn.execute("DELETE FROM dismissed WHERE canonical_url = ?", (canonical_url,))
    conn.commit()
    item_id = int(cursor.lastrowid)
    reindex_item(conn, item_id)
    # Every new item -- however it was saved -- lands in the tagging queue. This is
    # the one choke point all save paths pass through (extension, autosave, CLI), so
    # nothing can be saved without being queued to be tagged.
    enqueue_for_tagging(conn, item_id)
    return item_id, True


def enqueue_for_tagging(conn: sqlite3.Connection, item_id: int) -> None:
    """Mark an item as needing a topic tag. Idempotent: re-queuing is a no-op."""
    conn.execute(
        "INSERT OR IGNORE INTO tag_queue (item_id, queued_at) VALUES (?, ?)",
        (item_id, utcnow()),
    )
    conn.commit()


def tag_queue_ids(conn: sqlite3.Connection) -> list[int]:
    """Items waiting to be tagged, oldest first."""
    return [
        r["item_id"]
        for r in conn.execute("SELECT item_id FROM tag_queue ORDER BY queued_at")
    ]


def tag_queue_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM tag_queue").fetchone()[0])


def dequeue_from_tagging(conn: sqlite3.Connection, ids: Sequence[int]) -> None:
    """Remove items from the queue once a tagging pass has attempted them -- whether
    or not a topic stuck. A page with no abstract (a login wall, a profile) will never
    get a topic; leaving it queued would retry it forever, so an attempt is terminal."""
    if not ids:
        return
    conn.executemany("DELETE FROM tag_queue WHERE item_id = ?", [(i,) for i in ids])
    conn.commit()


def get_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def get_item_by_url(conn: sqlite3.Connection, canonical_url: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM items WHERE canonical_url = ?", (canonical_url,)
    ).fetchone()


def set_status(conn: sqlite3.Connection, item_id: int, status: str) -> None:
    conn.execute("UPDATE items SET status = ? WHERE id = ?", (status, item_id))
    conn.commit()


def set_notes(conn: sqlite3.Connection, item_id: int, notes: str) -> None:
    conn.execute("UPDATE items SET notes = ? WHERE id = ?", (notes, item_id))
    conn.commit()


def set_bibtex(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    bibtex: str,
    source: str,
    venue: str | None,
    published: bool,
) -> None:
    """Store a resolved BibTeX entry and its provenance on the item."""
    conn.execute(
        "UPDATE items SET bibtex = ?, bibtex_source = ?, bibtex_venue = ?, "
        "bibtex_published = ?, bibtex_at = ? WHERE id = ?",
        (bibtex, source, venue, 1 if published else 0, utcnow(), item_id),
    )
    conn.commit()


def set_tag_description(conn: sqlite3.Connection, name: str, description: str | None) -> None:
    """Describe a branch so the agent can file matching papers into it."""
    name = normalize_tag(name)
    create_tag(conn, name)  # a description can precede any items
    conn.execute(
        "UPDATE tags SET description = ? WHERE name = ?",
        ((description or "").strip() or None, name),
    )
    conn.commit()


def described_tags(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """User branches that carry a description -- the agent's custom filing targets."""
    return [
        (r["name"], r["description"])
        for r in conn.execute(
            "SELECT name, description FROM tags "
            "WHERE description IS NOT NULL AND TRIM(description) != ''"
        )
    ]


def set_fields(conn: sqlite3.Connection, item_id: int, **fields: Any) -> None:
    """Overwrite user-editable metadata on an item, then reindex for search."""
    allowed = {"title", "authors", "venue", "year", "abstract", "summary"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{key} = ?" for key in updates)
    conn.execute(
        f"UPDATE items SET {assignments} WHERE id = ?",
        (*updates.values(), item_id),
    )
    conn.commit()
    reindex_item(conn, item_id)


def clear_bibtex(conn: sqlite3.Connection, item_id: int) -> None:
    """Remove a stored BibTeX -- used when none can be extracted, so a stale
    generated entry never lingers. An empty slot invites the user to paste one."""
    conn.execute(
        "UPDATE items SET bibtex = NULL, bibtex_source = NULL, bibtex_venue = NULL, "
        "bibtex_published = NULL, bibtex_at = ? WHERE id = ?",
        (utcnow(), item_id),
    )
    conn.commit()


def mark_enriched(conn: sqlite3.Connection, item_id: int, summary: str | None) -> None:
    conn.execute(
        "UPDATE items SET enriched_at = ?, summary = COALESCE(NULLIF(?, ''), summary) WHERE id = ?",
        (utcnow(), summary, item_id),
    )
    conn.commit()
    reindex_item(conn, item_id)


def unenriched(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM items WHERE enriched_at IS NULL ORDER BY saved_at ASC LIMIT ?",
            (limit,),
        )
    )


# --- tags -------------------------------------------------------------------


def normalize_tag(name: str) -> str:
    """Lowercase, hyphenated, slash-separated. Any depth of hierarchy is legal."""
    name = (name or "").strip().lower()
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"[^a-z0-9/\-]", "", name)
    name = re.sub(r"-{2,}", "-", name)
    name = re.sub(r"/{2,}", "/", name)
    return name.strip("-/")


def rename_tag(conn: sqlite3.Connection, old: str, new: str) -> dict:
    """Rename a tag everywhere, folding into the target if it already exists.

    Also rewrites descendants: renaming `vlm` to `multimodal` moves
    `vlm/reasoning` to `multimodal/reasoning` too, so a rename never strands
    half a subtree.
    """
    old, new = normalize_tag(old), normalize_tag(new)
    if not old or not new or old == new:
        return {"renamed": 0, "merged": 0}

    pairs = [(old, new)]
    for row in conn.execute("SELECT name FROM tags WHERE name LIKE ?", (old + "/%",)):
        pairs.append((row["name"], new + row["name"][len(old) :]))

    renamed = merged = 0
    for source, target in pairs:
        source_row = conn.execute(
            "SELECT id FROM tags WHERE name = ?", (source,)
        ).fetchone()
        if not source_row:
            continue
        target_row = conn.execute(
            "SELECT id FROM tags WHERE name = ?", (target,)
        ).fetchone()

        if target_row is None:
            conn.execute(
                "UPDATE tags SET name = ? WHERE id = ?", (target, source_row["id"])
            )
            renamed += 1
        else:
            # Target exists: move the memberships across, keeping manual origin.
            conn.execute(
                "UPDATE OR IGNORE item_tags SET tag_id = ? WHERE tag_id = ?",
                (target_row["id"], source_row["id"]),
            )
            conn.execute("DELETE FROM item_tags WHERE tag_id = ?", (source_row["id"],))
            conn.execute("DELETE FROM tags WHERE id = ?", (source_row["id"],))
            merged += 1
    conn.commit()
    _reindex_tagged(conn, [p[1] for p in pairs])
    return {"renamed": renamed, "merged": merged}


def merge_tags(conn: sqlite3.Connection, sources: Sequence[str], target: str) -> int:
    """Fold several tags into one. The vocabulary-hygiene tool."""
    moved = 0
    for source in sources:
        result = rename_tag(conn, source, target)
        moved += result["renamed"] + result["merged"]
    return moved


def delete_tag(conn: sqlite3.Connection, name: str, *, with_children: bool = False) -> int:
    name = normalize_tag(name)
    names = [name]
    if with_children:
        names += [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM tags WHERE name LIKE ?", (name + "/%",)
            )
        ]
    affected = [
        row["item_id"]
        for row in conn.execute(
            "SELECT DISTINCT it.item_id FROM item_tags it JOIN tags t ON t.id = it.tag_id "
            f"WHERE t.name IN ({','.join('?' * len(names))})",
            names,
        )
    ]
    removed = 0
    for one in names:
        cursor = conn.execute("DELETE FROM tags WHERE name = ?", (one,))
        removed += cursor.rowcount
    # Remember the deletion: the agent must not recreate a tag the user rejected.
    # Computed facets (type/venue/site) are identity, not a choice, so they are
    # allowed to come back; only a chosen organisation is banned.
    now = utcnow()
    conn.executemany(
        "INSERT OR IGNORE INTO banned_tags (name, banned_at) VALUES (?, ?)",
        [(n, now) for n in names if not n.split("/")[0] in ("type", "venue", "site")],
    )
    conn.commit()
    for item_id in affected:
        reindex_item(conn, item_id)
    return removed


def ban_tag(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO banned_tags (name, banned_at) VALUES (?, ?)",
        (normalize_tag(name), utcnow()),
    )
    conn.commit()


def unban_tag(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("DELETE FROM banned_tags WHERE name = ?", (normalize_tag(name),))
    conn.commit()


def banned_tags(conn: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM banned_tags")}


def user_preferred_tags(conn: sqlite3.Connection, limit: int = 100) -> list[str]:
    """Tags the researcher applied or created by hand.

    These are the strongest possible signal of how they want their library
    organised, so the agent is told to prefer them and to keep using them for
    new items -- deletion teaches what NOT to do, this teaches what TO do.
    """
    rows = conn.execute(
        "SELECT DISTINCT t.name FROM tags t WHERE "
        "  t.origin = 'manual' "
        "  OR (t.description IS NOT NULL AND TRIM(t.description) != '') "
        "  OR EXISTS (SELECT 1 FROM item_tags it WHERE it.tag_id = t.id AND it.origin = 'manual') "
        "ORDER BY t.name LIMIT ?",
        (limit,),
    ).fetchall()
    # Computed facets are not a choice the user is expressing; skip them.
    return [r["name"] for r in rows if r["name"].split("/")[0] not in ("type", "venue", "site")]


def is_banned(conn: sqlite3.Connection, name: str) -> bool:
    """A tag is banned if it or any ancestor was deleted by the user."""
    name = normalize_tag(name)
    parts = name.split("/")
    candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    row = conn.execute(
        f"SELECT 1 FROM banned_tags WHERE name IN ({','.join('?' * len(candidates))}) LIMIT 1",
        candidates,
    ).fetchone()
    return row is not None


def clear_organization(conn: sqlite3.Connection, *, keep_custom: bool = True) -> dict:
    """Wipe the tag organisation, leaving items, BibTeX, notes and status intact.

    A fresh start when the taxonomy has drifted too far to fix in place. Computed
    facets (type/venue/site) come back on the next facet pass, so this really
    clears the *chosen* organisation. Custom branches you described are kept by
    default -- those are your structure, not the agent's.
    """
    keep = ("type/", "venue/", "site/")
    described = {name for name, _ in described_tags(conn)} if keep_custom else set()

    removed_links = removed_tags = 0
    for row in conn.execute("SELECT id, name FROM tags").fetchall():
        name = row["name"]
        if name.startswith(keep):
            continue
        if keep_custom and any(name == d or name.startswith(d + "/") or d.startswith(name + "/") for d in described):
            continue
        conn.execute("DELETE FROM item_tags WHERE tag_id = ?", (row["id"],))
        conn.execute("DELETE FROM tags WHERE id = ?", (row["id"],))
        removed_tags += 1
    conn.commit()
    # Reindex everything: the tags column in the search index is now stale.
    for row in conn.execute("SELECT id FROM items").fetchall():
        reindex_item(conn, row["id"])
    return {"removed_tags": removed_tags, "removed_links": removed_links}


def _reindex_tagged(conn: sqlite3.Connection, names: Sequence[str]) -> None:
    if not names:
        return
    ids = [
        row["item_id"]
        for row in conn.execute(
            "SELECT DISTINCT it.item_id FROM item_tags it JOIN tags t ON t.id = it.tag_id "
            f"WHERE t.name IN ({','.join('?' * len(names))})",
            list(names),
        )
    ]
    for item_id in ids:
        reindex_item(conn, item_id)


def remove_tag_from_item(conn: sqlite3.Connection, item_id: int, name: str) -> bool:
    tag = conn.execute(
        "SELECT id FROM tags WHERE name = ?", (normalize_tag(name),)
    ).fetchone()
    if not tag:
        return False
    cursor = conn.execute(
        "DELETE FROM item_tags WHERE item_id = ? AND tag_id = ?", (item_id, tag["id"])
    )
    conn.commit()
    reindex_item(conn, item_id)
    return cursor.rowcount > 0


def set_bucket(conn: sqlite3.Connection, item_id: int, bucket: str) -> None:
    conn.execute("UPDATE items SET bucket = ? WHERE id = ?", (bucket, item_id))
    conn.commit()


def materialize_ancestors(conn: sqlite3.Connection) -> int:
    """Give every implied parent a row of its own.

    The tree view synthesises missing ancestors so browsing always works, but a
    synthesised node has no row to rename, pin or carry a provenance flag. After
    a restructure moves `task/x` to `task/family/x`, `task/family` is implied by
    every child and owned by nothing -- this makes it real.
    """
    names = {r["name"] for r in conn.execute("SELECT name FROM tags")}
    missing = set()
    for name in names:
        parts = name.split("/")
        for depth in range(1, len(parts)):
            parent = "/".join(parts[:depth])
            if parent and parent not in names:
                missing.add(parent)
    if missing:
        now = utcnow()
        conn.executemany(
            "INSERT OR IGNORE INTO tags (name, created_at, origin) VALUES (?, ?, 'model')",
            [(name, now) for name in sorted(missing)],
        )
        conn.commit()
    return len(missing)


def prune_empty_tags(conn: sqlite3.Connection, dry_run: bool = False) -> list[str]:
    """Drop model-proposed tags that ended up holding nothing.

    The derivation pass proposes a vocabulary for the whole library before any
    item is classified, so a name that no item turned out to match is left
    behind as an empty branch. Those are noise. Tags the user created are never
    pruned: an empty branch you made yourself is a plan, not a leftover.
    """
    rows = conn.execute(
        "SELECT name FROM tags WHERE origin != 'manual' AND NOT EXISTS ("
        "  SELECT 1 FROM item_tags it JOIN tags c ON c.id = it.tag_id"
        "  WHERE c.name = tags.name OR c.name LIKE tags.name || '/%')"
        " ORDER BY name"
    ).fetchall()
    doomed = [r["name"] for r in rows]
    if doomed and not dry_run:
        conn.executemany("DELETE FROM tags WHERE name = ?", [(n,) for n in doomed])
        conn.commit()
    return doomed


def create_tag(
    conn: sqlite3.Connection, name: str, origin: str = "manual"
) -> str | None:
    """Create a tag that holds nothing yet.

    A tag has always appeared by being applied to an item, which makes it
    impossible to build a structure first and file into it afterwards -- the
    way a folder tree is normally made. This lets an empty branch exist.
    """
    normalized = normalize_tag(name)
    if not normalized:
        return None
    now = utcnow()
    # Materialise the ancestors too, so a deep path is browsable immediately.
    # They inherit the origin: an ancestor of a hand-made branch is hand-made,
    # and must survive a prune even while it is still empty.
    parts = normalized.split("/")
    for depth in range(len(parts)):
        conn.execute(
            "INSERT OR IGNORE INTO tags (name, created_at, origin) VALUES (?, ?, ?)",
            ("/".join(parts[: depth + 1]), now, origin),
        )
        if origin == "manual":
            conn.execute(
                "UPDATE tags SET origin = 'manual' WHERE name = ?",
                ("/".join(parts[: depth + 1]),),
            )
    conn.commit()
    return normalized


def tag_tree(conn: sqlite3.Connection) -> list[dict]:
    """Tag counts arranged as a hierarchy, for a browsable sidebar.

    Includes tags with no items: an empty branch you just created has to be
    visible, or there is nowhere to drop anything.
    """
    counts = dict(tag_counts(conn))
    for name in vocabulary(conn):
        counts.setdefault(name, 0)
    descriptions = {
        r["name"]: r["description"]
        for r in conn.execute(
            "SELECT name, description FROM tags WHERE description IS NOT NULL AND TRIM(description) != ''"
        )
    }
    nodes: dict[str, dict] = {}
    for name in sorted(counts):
        parts = name.split("/")
        for depth in range(len(parts)):
            path = "/".join(parts[: depth + 1])
            nodes.setdefault(
                path,
                {
                    "name": path,
                    "label": parts[depth],
                    "depth": depth,
                    "count": 0,
                    "own": counts.get(path, 0),
                    "description": descriptions.get(path),
                },
            )
    # A parent's count includes everything filed beneath it, counted as DISTINCT
    # items -- summing per-tag counts double-counts an item tagged at both a parent
    # and a descendant (topic/nlp AND topic/nlp/parsing), so the sidebar showed 2
    # where a click listed 1.
    #
    # This used to run one `COUNT(DISTINCT ...) WHERE name = ? OR name LIKE ?` query
    # PER node -- hundreds of subtree scans per call, ~700ms. Instead, walk every
    # (item, tag) pair once: each item contributes +1 to its tag's path and every
    # ancestor path, deduped per item, which is exactly the distinct-subtree count and
    # matches the `name = ? OR name LIKE ?` item filter. One query, one pass, ~20ms.
    subtree: dict[str, int] = {}
    seen_here: dict[str, set[int]] = {}
    for item_id, name in conn.execute(
        "SELECT it.item_id, t.name FROM item_tags it JOIN tags t ON t.id = it.tag_id"
    ):
        parts = name.split("/")
        for depth in range(len(parts)):
            path = "/".join(parts[: depth + 1])
            bucket = seen_here.setdefault(path, set())
            if item_id not in bucket:
                bucket.add(item_id)
                subtree[path] = subtree.get(path, 0) + 1
    for path, node in nodes.items():
        node["count"] = subtree.get(path, 0)
    return [nodes[k] for k in sorted(nodes)]


def add_tags(
    conn: sqlite3.Connection,
    item_id: int,
    names: Sequence[str],
    origin: str = "manual",
    confidence: float | None = None,
) -> list[str]:
    """Attach tags. A manual tag is never downgraded to a model tag.

    Deletion is remembered: a tag the user removed is banned, and the agent may
    not recreate it. Adding it back by hand is allowed and lifts the ban -- you
    changed your mind, which is different from the model ignoring you.
    """
    applied: list[str] = []
    now = utcnow()
    banned = banned_tags(conn)
    for raw in names:
        name = normalize_tag(raw)
        if not name:
            continue
        # Is this name, or any ancestor, banned?
        parts = name.split("/")
        ancestry = {"/".join(parts[: i + 1]) for i in range(len(parts))}
        if ancestry & banned:
            if origin == "manual":
                conn.executemany(
                    "DELETE FROM banned_tags WHERE name = ?", [(a,) for a in ancestry]
                )
                banned -= ancestry
            else:
                continue  # the agent must respect a deletion
        # Record the tag's own origin to match the link: a brand-new tag the user
        # hand-applied is 'manual', so if its last item is later removed it is NOT
        # swept by prune_empty_tags (which keys on tags.origin). Existing tags keep
        # their origin (INSERT OR IGNORE).
        conn.execute(
            "INSERT OR IGNORE INTO tags (name, created_at, origin) VALUES (?, ?, ?)",
            (name, now, origin),
        )
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
        existing = conn.execute(
            "SELECT origin FROM item_tags WHERE item_id = ? AND tag_id = ?",
            (item_id, tag_id),
        ).fetchone()
        if existing:
            # Your judgment wins over the model's: only upgrade model -> manual.
            if existing["origin"] == "model" and origin == "manual":
                conn.execute(
                    "UPDATE item_tags SET origin = 'manual', confidence = NULL "
                    "WHERE item_id = ? AND tag_id = ?",
                    (item_id, tag_id),
                )
                applied.append(name)
            continue
        conn.execute(
            "INSERT INTO item_tags (item_id, tag_id, origin, confidence) VALUES (?, ?, ?, ?)",
            (item_id, tag_id, origin, confidence),
        )
        applied.append(name)
    conn.commit()
    reindex_item(conn, item_id)
    return applied


def item_tags(conn: sqlite3.Connection, item_id: int) -> list[str]:
    return [
        row["name"]
        for row in conn.execute(
            "SELECT t.name FROM item_tags it JOIN tags t ON t.id = it.tag_id "
            "WHERE it.item_id = ? ORDER BY t.name",
            (item_id,),
        )
    ]


def tag_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [
        (row["name"], row["n"])
        for row in conn.execute(
            "SELECT t.name AS name, COUNT(*) AS n FROM item_tags it "
            "JOIN tags t ON t.id = it.tag_id GROUP BY t.name ORDER BY n DESC, t.name"
        )
    ]


def vocabulary(conn: sqlite3.Connection) -> list[str]:
    return [row["name"] for row in conn.execute("SELECT name FROM tags ORDER BY name")]


def remove_thin_site_tags(conn: sqlite3.Connection, min_items: int = 3) -> int:
    """Drop `site/` tags held by fewer than `min_items` items.

    A site branch is only worth having when a domain recurs; a one-off domain --
    or a mangled URL parsed as a domain (`site/anthl08-1180`) -- is noise, not
    structure. Called after facets are applied so single-domain items never keep
    a site tag.
    """
    thin = [
        r["id"]
        for r in conn.execute(
            "SELECT t.id FROM tags t WHERE t.name LIKE 'site/%' AND ("
            "  SELECT COUNT(*) FROM item_tags it WHERE it.tag_id = t.id) < ?",
            (min_items,),
        )
    ]
    if not thin:
        return 0
    # Reindex the items losing a tag, or the FTS `tags` column keeps the removed
    # site name and a search still matches it until some unrelated edit.
    affected = [
        r["item_id"]
        for r in conn.execute(
            f"SELECT DISTINCT item_id FROM item_tags WHERE tag_id IN ({','.join('?' for _ in thin)})",
            thin,
        )
    ]
    conn.executemany("DELETE FROM item_tags WHERE tag_id = ?", [(i,) for i in thin])
    conn.executemany("DELETE FROM tags WHERE id = ?", [(i,) for i in thin])
    conn.commit()
    for iid in affected:
        reindex_item(conn, iid)
    conn.commit()
    return len(thin)


def delete_item(conn: sqlite3.Connection, item_id: int) -> bool:
    """Permanently remove an item and everything attached to it.

    item_tags and aliases fall away via ON DELETE CASCADE, but the FTS row has
    to be removed by hand -- search_index is a virtual table, not covered by the
    foreign keys.
    """
    row = conn.execute("SELECT canonical_url FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return False
    # Remember every URL that resolved to this item -- its own and any alias -- so a
    # still-open Chrome tab cannot resurrect it on the next ingest. Delete means stop.
    urls = {row["canonical_url"]}
    urls.update(
        r["canonical_url"]
        for r in conn.execute("SELECT canonical_url FROM aliases WHERE item_id = ?", (item_id,))
    )
    now = utcnow()
    conn.executemany(
        "INSERT OR REPLACE INTO dismissed (canonical_url, dismissed_at) VALUES (?, ?)",
        [(u, now) for u in urls if u],
    )
    conn.execute("DELETE FROM search_index WHERE item_id = ?", (str(item_id),))
    conn.execute("DELETE FROM item_tags WHERE item_id = ?", (item_id,))
    conn.execute("DELETE FROM aliases WHERE item_id = ?", (item_id,))
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    conn.commit()
    return True


def is_dismissed(conn: sqlite3.Connection, canonical_url: str) -> bool:
    """Whether this URL was deleted by the user and must not be re-ingested."""
    return (
        conn.execute(
            "SELECT 1 FROM dismissed WHERE canonical_url = ?", (canonical_url,)
        ).fetchone()
        is not None
    )


def undismiss(conn: sqlite3.Connection, canonical_url: str) -> None:
    """Lift a deletion so a deliberate re-save can bring the URL back."""
    conn.execute("DELETE FROM dismissed WHERE canonical_url = ?", (canonical_url,))


# --- search index -----------------------------------------------------------


def reindex_item(conn: sqlite3.Connection, item_id: int) -> None:
    item = get_item(conn, item_id)
    if item is None:
        return
    conn.execute("DELETE FROM search_index WHERE item_id = ?", (str(item_id),))
    conn.execute(
        "INSERT INTO search_index (title, venue, authors, abstract, body, tags, item_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            item["title"] or "",
            item["venue"] or "",
            item["authors"] or "",
            " ".join(filter(None, [item["abstract"] or "", item["summary"] or ""])),
            item["body"] or "",
            " ".join(name.replace("/", " ") for name in item_tags(conn, item_id)),
            str(item_id),
        ),
    )
    conn.commit()


def rebuild_index(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM search_index")
    conn.commit()
    ids = [row["id"] for row in conn.execute("SELECT id FROM items")]
    for item_id in ids:
        reindex_item(conn, item_id)
    return len(ids)


def fts_query(terms: Sequence[str], mode: str = "AND") -> str:
    """Quote every token so user input can never be FTS5 syntax.

    `mode="OR"` lets bm25 rank by how many terms a row matches, which is what a
    natural-language question needs: requiring every word means one stray word
    ("connects") returns nothing at all.
    """
    quoted = []
    for term in terms:
        cleaned = re.sub(r'[^\w\s]', " ", term, flags=re.UNICODE).strip()
        if cleaned:
            quoted.append('"' + cleaned + '"')
    joiner = " OR " if mode.upper() == "OR" else " AND "
    return joiner.join(quoted)


def history(
    conn: sqlite3.Connection,
    *,
    bucket: str | None = None,
    before: str | None = None,
    limit_days: int = 30,
) -> tuple[list[sqlite3.Row], str | None]:
    """Items grouped by the day they were first seen, most recent day first.

    Keyed on first_seen, not saved_at: saved_at is when a row entered this
    database (one bulk moment for an import), while first_seen is when the tab
    or paper was actually first encountered -- inherited from the ledger for a
    Chrome tab and from dateAdded for a Zotero paper, so it spans years and is
    the timeline a researcher recognises as "when did I pick this up".

    Paginates by whole days: returns up to ``limit_days`` distinct days strictly
    older than ``before`` (a YYYY-MM-DD cursor), and the cursor for the next page
    -- None once the last day has been returned, so no day is ever split.
    """
    where = ["first_seen IS NOT NULL"]
    params: list[Any] = []
    if bucket:
        where.append("COALESCE(bucket, 'library') = ?")
        params.append(bucket)
    if before:
        where.append("substr(first_seen, 1, 10) < ?")
        params.append(before)
    clause = " AND ".join(where)

    days = [
        row["d"]
        for row in conn.execute(
            f"SELECT DISTINCT substr(first_seen, 1, 10) AS d FROM items "
            f"WHERE {clause} ORDER BY d DESC LIMIT ?",
            (*params, limit_days),
        )
    ]
    if not days:
        return [], None

    placeholders = ",".join("?" for _ in days)
    item_params: list[Any] = list(days)
    bucket_clause = ""
    if bucket:
        bucket_clause = " AND COALESCE(bucket, 'library') = ?"
        item_params.append(bucket)
    rows = conn.execute(
        f"SELECT * FROM items WHERE substr(first_seen, 1, 10) IN ({placeholders})"
        f"{bucket_clause} ORDER BY first_seen DESC, saved_at DESC",
        item_params,
    ).fetchall()

    # A full page means there may be more; the next cursor is the oldest day we
    # just returned, and the strict `<` above keeps that day from repeating.
    next_before = days[-1] if len(days) == limit_days else None
    return rows, next_before


def since_bound(since: str | None) -> str | None:
    """Accept 2026, 2026-01 or 2026-01-15."""
    if not since:
        return None
    parts = since.strip().split("-")
    year = parts[0].zfill(4)
    month = parts[1].zfill(2) if len(parts) > 1 else "01"
    day = parts[2].zfill(2) if len(parts) > 2 else "01"
    return f"{year}-{month}-{day}T00:00:00+00:00"


def _build_query(
    terms: Sequence[str],
    tag: str | None,
    status: str | None,
    since: str | None,
    bucket: str | None,
    source: str | None,
    untagged: bool,
    no_topic: bool,
    mode: str = "AND",
) -> tuple[str, str, list[Any]]:
    """Return (from_clause, order_by, params) shared by search and count."""
    params: list[Any] = []
    query = fts_query(terms, mode)

    if query:
        from_clause = (
            "FROM search_index s JOIN items i ON i.id = CAST(s.item_id AS INTEGER) "
            "WHERE search_index MATCH ?"
        )
        params.append(query)
        order = "ORDER BY rank ASC"
    else:
        from_clause = "FROM items i WHERE 1=1"
        order = "ORDER BY i.saved_at DESC"

    where: list[str] = []
    if tag:
        normalized = normalize_tag(tag)
        where.append(
            "EXISTS (SELECT 1 FROM item_tags it JOIN tags t ON t.id = it.tag_id "
            "WHERE it.item_id = i.id AND (t.name = ? OR t.name LIKE ?))"
        )
        params.extend([normalized, normalized + "/%"])
    if untagged:
        # Deliberately not "has no tags": every item carries computed type/venue/
        # site tags, so that test matched almost nothing. Unassigned means no
        # facet a model was asked to assign -- the queue of items awaiting a call.
        where.append(
            "NOT EXISTS (SELECT 1 FROM item_tags it JOIN tags t ON t.id = it.tag_id WHERE it.item_id = i.id AND (t.name LIKE 'topic/%' OR t.name LIKE 'method/%' OR t.name LIKE 'task/%' OR t.name LIKE 'contribution/%'))"
        )
    if no_topic:
        # Weaker than `untagged`: these items were filed under some facet, just
        # never under topic/ -- the facet the tree is actually browsed by.
        where.append(
            "NOT EXISTS (SELECT 1 FROM item_tags it JOIN tags t ON t.id = it.tag_id "
            "WHERE it.item_id = i.id AND t.name LIKE 'topic/%')"
        )
    if status:
        where.append("i.status = ?")
        params.append(status)
    if bucket:
        where.append("COALESCE(i.bucket, 'library') = ?")
        params.append(bucket)
    if source:
        where.append("COALESCE(i.source, 'web') = ?")
        params.append(source)
    bound = since_bound(since)
    if bound:
        where.append("i.first_seen >= ?")
        params.append(bound)

    if where:
        from_clause += " AND " + " AND ".join(where)
    return from_clause, order, params


def search(
    conn: sqlite3.Connection,
    terms: Sequence[str] = (),
    tag: str | None = None,
    status: str | None = None,
    since: str | None = None,
    limit: int = 20,
    offset: int = 0,
    bucket: str | None = None,
    source: str | None = None,
    untagged: bool = False,
    no_topic: bool = False,
    sort: str | None = None,
    mode: str = "AND",
) -> list[sqlite3.Row]:
    """FTS5 with BM25 ranking. Sub-millisecond, deterministic, no model."""
    from_clause, order, params = _build_query(
        terms, tag, status, since, bucket, source, untagged, no_topic, mode
    )
    # Ordering must happen in SQL: sorting only the fetched page would put the
    # oldest item on page 2 above the newest on page 1.
    order = {
        "newest": "ORDER BY i.saved_at DESC",
        "oldest": "ORDER BY i.saved_at ASC",
        "age": "ORDER BY i.first_seen ASC",
    }.get(sort or "", order)
    rank = (
        f"bm25(search_index, {', '.join(str(w) for w in _BM25_WEIGHTS)})"
        if fts_query(terms, mode)
        else "NULL"
    )
    sql = f"SELECT i.*, {rank} AS rank {from_clause} {order} LIMIT ? OFFSET ?"
    return list(conn.execute(sql, [*params, limit, offset]))


def count(
    conn: sqlite3.Connection,
    terms: Sequence[str] = (),
    tag: str | None = None,
    status: str | None = None,
    since: str | None = None,
    bucket: str | None = None,
    source: str | None = None,
    untagged: bool = False,
    no_topic: bool = False,
) -> int:
    """How many rows the same filters match, independent of any page size."""
    from_clause, _order, params = _build_query(
        terms, tag, status, since, bucket, source, untagged, no_topic
    )
    return conn.execute(
        f"SELECT COUNT(*) AS n {from_clause}", params
    ).fetchone()["n"]


# --- export -----------------------------------------------------------------


def export_items(conn: sqlite3.Connection) -> list[dict]:
    """JSON-ready dump without body text or the embedding BLOB.

    `embedding` is raw float32 bytes -- leaving it in makes json.dumps raise
    "Object of type bytes is not JSON serializable", which silently broke the
    JSON backup sidecar and `tt export` the moment any item had an embedding.
    """
    out = []
    for row in conn.execute("SELECT * FROM items ORDER BY id"):
        record = {key: row[key] for key in row.keys() if key not in ("body", "embedding")}
        record["tags"] = item_tags(conn, row["id"])
        out.append(record)
    return out
