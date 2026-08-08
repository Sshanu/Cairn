"""Typer commands. The CLI is useful without the UI and without the agent."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer

from . import (
    ask as ask_mod,
    backup as backup_mod,
    bibtex as bibtex_mod,
    capture,
    chrome,
    config,
    db,
    enrich as enrich_mod,
    ingest as ingest_mod,
    organize as organize_mod,
)
from .backends.base import BackendError, get_backend

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Track, search and tag the papers and blogs you read in Chrome.",
)


def _db():
    return db.connect()


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _format_row(conn, row) -> str:
    tags = db.item_tags(conn, row["id"])
    age = db.age_days(row["first_seen"])
    bits = [b for b in (row["source"], str(row["year"]) if row["year"] else None) if b]
    if age is not None:
        bits.append(f"carried {age}d" if age >= 30 else f"{age}d ago")
    if row["status"] and row["status"] != "unread":
        bits.append(row["status"])
    line = f"{row['id']:>5}  {row['title'] or row['canonical_url']}"
    detail = "       " + " · ".join(bits)
    if tags:
        detail += "   " + " ".join(tags)
    return line + "\n" + detail


@app.command()
def save(
    tags: list[str] = typer.Option([], "--tag", "-t", help="Manual tags to apply."),
    window: bool = typer.Option(False, "--window", help="Save every tab in the front window."),
    url: Optional[str] = typer.Option(None, "--url", help="Save a URL directly."),
    no_enrich: bool = typer.Option(False, "--no-enrich", help="Skip model tagging."),
    no_meta: bool = typer.Option(False, "--no-meta", help="Skip metadata lookup."),
) -> None:
    """Save the front tab, a window, or a URL."""
    conn = _db()

    if url:
        targets = [{"url": url, "title": None}]
    elif window:
        targets = chrome.front_window_tabs()
        # Tagging forty tabs interactively is slow, so a window save is never
        # enriched inline. Batch it later with `tt enrich`.
        no_enrich = True
    else:
        tab = chrome.front_tab()
        if not tab:
            _fail("no front tab (is Chrome running?)")
        targets = [tab]

    saved = []
    for target in targets:
        result = capture.save_url(
            conn,
            target["url"],
            title=target.get("title"),
            tags=tuple(tags),
            fetch_meta=not no_meta,
        )
        if result:
            saved.append(result)

    if not saved:
        _fail("nothing saved (blocked, or not an http URL)")

    for result in saved:
        marker = "+" if result.created else "="
        typer.echo(f"{marker} {result.item_id:>5}  {result.title or result.canonical_url}")
        if result.meta_error:
            typer.secho(f"        metadata lookup failed: {result.meta_error}",
                        fg=typer.colors.YELLOW, err=True)
    typer.echo(f"Saved {len(saved)} item{'s' if len(saved) != 1 else ''}.")

    if no_enrich:
        typer.echo("Not enriched. Run `tt enrich` to tag them in batch.")
        return

    try:
        backend = get_backend()
    except Exception as exc:  # config or import problem, not a save failure
        typer.secho(f"Saved, but not enriched: {exc}", fg=typer.colors.YELLOW, err=True)
        return

    for result in saved:
        try:
            outcome = enrich_mod.enrich_item(conn, backend, result.item_id)
        except BackendError as exc:
            typer.secho(f"  enrich failed: {exc}", fg=typer.colors.YELLOW, err=True)
            continue
        if outcome.applied:
            typer.echo(f"  tagged: {' '.join(outcome.applied)}")
        if outcome.proposed:
            typer.echo(f"  proposed (needs --accept-new): {' '.join(outcome.proposed)}")


@app.command()
def find(
    query: list[str] = typer.Argument(None, help="Search terms."),
    tag: Optional[str] = typer.Option(None, "--tag", help="Filter by tag or tag prefix."),
    status: Optional[str] = typer.Option(None, "--status", help="unread|reading|read|archived"),
    since: Optional[str] = typer.Option(None, "--since", help="First seen on or after, e.g. 2026-01."),
    limit: int = typer.Option(20, "--limit"),
    urls: bool = typer.Option(False, "--urls", help="Print URLs only."),
) -> None:
    """FTS5 search. Deterministic, sub-millisecond, no model."""
    conn = _db()
    terms = [t for t in (query or []) if t.strip()]
    rows = db.search(conn, terms, tag=tag, status=status, since=since, limit=limit)

    if not rows:
        typer.echo("No matches.")
        return
    for row in rows:
        typer.echo(row["canonical_url"] if urls else _format_row(conn, row))


@app.command()
def show(item_id: int = typer.Argument(..., metavar="ID")) -> None:
    """Print everything known about one item."""
    conn = _db()
    row = db.get_item(conn, item_id)
    if row is None:
        _fail(f"no item {item_id}")
    typer.echo(row["title"] or "(untitled)")
    typer.echo(row["canonical_url"])
    for label in ("authors", "venue", "year", "status", "first_seen", "saved_at"):
        if row[label]:
            typer.echo(f"{label:>11}: {row[label]}")
    tags = db.item_tags(conn, item_id)
    if tags:
        typer.echo(f"{'tags':>11}: {' '.join(tags)}")
    if row["summary"]:
        typer.echo(f"\n{row['summary']}")
    if row["abstract"]:
        typer.echo(f"\n{row['abstract']}")
    if row["notes"]:
        typer.echo(f"\nNotes: {row['notes']}")


@app.command()
def tag(
    item_id: int = typer.Argument(..., metavar="ID"),
    names: list[str] = typer.Argument(..., metavar="NAME..."),
) -> None:
    """Add manual tags. Manual always outranks the model."""
    conn = _db()
    if db.get_item(conn, item_id) is None:
        _fail(f"no item {item_id}")
    applied = db.add_tags(conn, item_id, names, origin="manual")
    typer.echo(f"Tagged {item_id}: {' '.join(db.item_tags(conn, item_id))}")
    if not applied:
        typer.echo("(no change)")


@app.command()
def status(
    item_id: int = typer.Argument(..., metavar="ID"),
    value: str = typer.Argument(..., metavar="unread|reading|read|archived"),
) -> None:
    """Set reading status."""
    allowed = {"unread", "reading", "read", "archived"}
    if value not in allowed:
        _fail(f"status must be one of {', '.join(sorted(allowed))}")
    conn = _db()
    if db.get_item(conn, item_id) is None:
        _fail(f"no item {item_id}")
    db.set_status(conn, item_id, value)
    typer.echo(f"{item_id} is now {value}.")


@app.command()
def note(
    item_id: int = typer.Argument(..., metavar="ID"),
    text: str = typer.Argument(..., metavar="TEXT"),
) -> None:
    """Replace the notes field on an item."""
    conn = _db()
    if db.get_item(conn, item_id) is None:
        _fail(f"no item {item_id}")
    db.set_notes(conn, item_id, text)
    typer.echo(f"Noted on {item_id}.")


@app.command(name="open")
def open_item(item_id: int = typer.Argument(..., metavar="ID")) -> None:
    """Open an item in the browser."""
    conn = _db()
    row = db.get_item(conn, item_id)
    if row is None:
        _fail(f"no item {item_id}")
    subprocess.run(["open", row["canonical_url"]], check=False)


@app.command()
def poll() -> None:
    """Ledger tick. Run from launchd, not by hand."""
    conn = _db()
    if not config.ledger_enabled():
        typer.echo("Ledger disabled (CAIRN_LEDGER=0).")
        return
    count = capture.poll(conn)
    typer.echo(f"Recorded {count} tabs.")
    _drain_tag_queue(conn)
    _heal_titles(conn)
    # Fully truncate the WAL each tick. A bloated WAL is what made the UI crawl; a
    # TRUNCATE (unlike the passive auto-checkpoint) reclaims frames even after a reader
    # briefly pinned it, keeping every read fast.
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as exc:
        typer.echo(f"wal checkpoint skipped: {exc}")


def _heal_titles(conn) -> None:
    """Re-fetch a few bad titles each tick -- a URL-as-title from a PDF/unloaded tab,
    or a site-suffixed one. Bounded and retry-capped in ingest.heal_titles; best
    effort here, so a fetch failure never breaks the poll's ledger job."""
    try:
        healed = ingest_mod.heal_titles(conn, limit=10)
        if healed:
            typer.echo(f"Healed {healed} title(s).")
    except Exception as exc:
        typer.echo(f"Title self-heal skipped: {exc}")


def _drain_tag_queue(conn) -> None:
    """Tag whatever has accumulated in the queue since the last tick, in one batched
    pass. This runs on the poll timer (every few minutes), so a paper saved from the
    extension gets a topic within one cycle without ever blocking the save itself.

    Best effort: a backend hiccup must never break the poll's ledger job, so any
    failure is reported and swallowed (the items stay queued and retry next tick).
    No-op when the queue is empty, which is the common case."""
    from . import topics

    pending = db.tag_queue_count(conn)
    if not pending:
        return
    try:
        backend = get_backend()
    except Exception as exc:
        typer.echo(f"{pending} item(s) queued for tagging; backend unavailable: {exc}")
        return
    typer.echo(f"Tagging {pending} queued item(s)…")
    try:
        tagged = topics.drain_tag_queue(conn, backend)
        typer.echo(f"Tagged {tagged}.")
    except Exception as exc:
        typer.echo(f"Tag-queue drain failed (stays queued, retries next tick): {exc}")


@app.command()
def stale(
    days: int = typer.Option(30, "--days", help="Carried at least this long."),
    close: bool = typer.Option(False, "--close", help="Close the listed tabs in Chrome."),
) -> None:
    """Unsaved tabs carried longer than N days. The triage queue.

    Run this weekly, save what matters, close the rest. The tool enables the
    habit and cannot replace it.
    """
    conn = _db()
    tabs = capture.stale_tabs(conn, days)
    if not tabs:
        typer.echo(f"No tabs older than {days} days. Nothing to review.")
        return

    for item in tabs:
        typer.echo(f"{item.age_days:>4}d  {item.title or item.canonical_url}")
        typer.echo(f"       {item.canonical_url}")
    typer.echo(f"\n{len(tabs)} tabs carried longer than {days} days.")

    if close:
        confirm = typer.confirm(f"Close {len(tabs)} tabs in Chrome?", default=False)
        if not confirm:
            typer.echo("Left open.")
            return
        closed = chrome.close_tabs([item.raw_url for item in tabs])
        typer.echo(f"Closed {closed} tabs.")


@app.command()
def enrich(
    limit: int = typer.Option(20, "--limit"),
    accept_new: bool = typer.Option(False, "--accept-new", help="Apply proposed new tags."),
    item_id: Optional[int] = typer.Option(None, "--id", help="Enrich one item."),
) -> None:
    """Batch tagging and summaries against the configured backend."""
    conn = _db()
    try:
        backend = get_backend()
    except Exception as exc:
        _fail(str(exc))

    if item_id is not None:
        results = [enrich_mod.enrich_item(conn, backend, item_id, accept_new=accept_new)]
    else:
        results = enrich_mod.enrich_batch(conn, backend, limit, accept_new=accept_new)

    if not results:
        typer.echo("Nothing to enrich.")
        return
    for result in results:
        row = db.get_item(conn, result.item_id)
        typer.echo(f"{result.item_id:>5}  {row['title'] if row else ''}")
        if result.error:
            typer.secho(f"       failed: {result.error}", fg=typer.colors.RED, err=True)
            continue
        if result.applied:
            typer.echo(f"       tagged: {' '.join(result.applied)}")
        if result.proposed:
            typer.echo(f"       proposed: {' '.join(result.proposed)}")

    succeeded = sum(1 for r in results if r.ok)
    failed = len(results) - succeeded
    summary = f"\nEnriched {succeeded} item{'s' if succeeded != 1 else ''}."
    if failed:
        summary += f" {failed} failed (will retry on the next run)."
    typer.echo(summary + f" Backend usage: {backend.usage}")
    if failed and succeeded == 0:
        raise typer.Exit(1)


@app.command()
def bibtex(
    limit: Optional[int] = typer.Option(None, "--limit", help="Cap how many to resolve."),
    workers: int = typer.Option(3, "--workers", help="Concurrent lookups (DBLP throttles bursts)."),
    upgrade: bool = typer.Option(False, "--upgrade", help="Also retry preprint/stub entries."),
    redo: bool = typer.Option(False, "--redo", help="Re-resolve items that already have one."),
    item_id: Optional[int] = typer.Option(None, "--id", help="Resolve one item."),
) -> None:
    """Fetch BibTeX for papers, preferring the published venue over arXiv."""
    conn = _db()
    if item_id is not None:
        rows = [db.get_item(conn, item_id)]
        if rows[0] is None:
            _fail(f"no item {item_id}")
    else:
        rows = bibtex_mod.pending(conn, redo=redo, upgrade=upgrade, limit=limit)
    if not rows:
        typer.echo("Every paper already has a BibTeX entry.")
        return

    published = preprint = failed = 0

    def report(row, result):
        nonlocal published, preprint, failed
        if not result.ok:
            failed += 1
            mark, colour = "fail", typer.colors.RED
        elif result.published:
            published += 1
            mark, colour = f"{result.source}", typer.colors.GREEN
        else:
            preprint += 1
            mark, colour = "preprint", typer.colors.YELLOW
        title = (row["title"] or row["canonical_url"] or "")[:58]
        typer.secho(f"  {mark:<11}", fg=colour, nl=False)
        typer.echo(f"{title}")

    with typer.progressbar(length=len(rows), label=f"Resolving {len(rows)}") as bar:
        bibtex_mod.backfill(
            conn, rows, workers=workers,
            on_result=lambda r, res: (report(r, res), bar.update(1)),
        )

    typer.echo(
        f"\n{published} published, {preprint} preprint-only, {failed} failed."
    )


@app.command()
def tags() -> None:
    """Vocabulary, most used first. Above about sixty entries it has stopped being useful."""
    conn = _db()
    counts = db.tag_counts(conn)
    if not counts:
        typer.echo("No tags yet.")
        return
    width = max(len(name) for name, _ in counts)
    for name, count in counts:
        typer.echo(f"{name:<{width}}  {count}")
    typer.echo(f"\n{len(counts)} tags.")
    if len(counts) > 60:
        typer.secho(
            "Vocabulary is over sixty entries -- look for near-duplicates to merge.",
            fg=typer.colors.YELLOW,
        )


@app.command(name="tag-rename")
def tag_rename(
    old: str = typer.Argument(..., help="Existing tag."),
    new: str = typer.Argument(..., help="New name."),
) -> None:
    """Rename a tag everywhere, including its whole subtree."""
    conn = _db()
    result = db.rename_tag(conn, old, new)
    if not result["renamed"] and not result["merged"]:
        _fail(f"no tag matching {old!r}")
    typer.echo(f"Renamed {result['renamed']}, merged {result['merged']} into existing tags.")


@app.command(name="tag-merge")
def tag_merge(
    target: str = typer.Argument(..., help="Tag to keep."),
    sources: list[str] = typer.Argument(..., help="Tags to fold into it."),
) -> None:
    """Fold several tags into one. Vocabulary hygiene."""
    conn = _db()
    moved = db.merge_tags(conn, sources, target)
    typer.echo(f"Folded {moved} tags into {db.normalize_tag(target)}.")


@app.command(name="tag-delete")
def tag_delete(
    name: str = typer.Argument(...),
    with_children: bool = typer.Option(False, "--with-children", help="Also delete the subtree."),
) -> None:
    """Delete a tag (and optionally everything under it)."""
    conn = _db()
    removed = db.delete_tag(conn, name, with_children=with_children)
    typer.echo(f"Deleted {removed} tags.")


@app.command()
def tree() -> None:
    """The tag vocabulary as a hierarchy."""
    conn = _db()
    nodes = db.tag_tree(conn)
    if not nodes:
        typer.echo("No tags yet. Run `tt organize`.")
        return
    for node in nodes:
        indent = "  " * node["depth"]
        typer.echo(f"{indent}{node['label']:<32} {node['count']:>4}")
    typer.echo(f"\n{len(nodes)} nodes.")


@app.command()
def move(
    item_id: int = typer.Argument(..., metavar="ID"),
    bucket: str = typer.Argument(..., metavar="library|docs"),
) -> None:
    """Move an item between buckets."""
    if bucket not in ("library", "docs"):
        _fail("bucket must be 'library' or 'docs'")
    conn = _db()
    if db.get_item(conn, item_id) is None:
        _fail(f"no item {item_id}")
    db.set_bucket(conn, item_id, bucket)
    typer.echo(f"Moved {item_id} to {bucket}.")


@app.command()
def untag(
    item_id: int = typer.Argument(..., metavar="ID"),
    names: list[str] = typer.Argument(..., metavar="NAME..."),
) -> None:
    """Remove tags from an item."""
    conn = _db()
    removed = sum(1 for n in names if db.remove_tag_from_item(conn, item_id, n))
    typer.echo(f"Removed {removed} tags. Now: {' '.join(db.item_tags(conn, item_id)) or '(none)'}")


@app.command()
def export(path: Optional[Path] = typer.Argument(None, help="Defaults to stdout.")) -> None:
    """JSON dump without body text."""
    conn = _db()
    payload = json.dumps(db.export_items(conn), indent=2, ensure_ascii=False)
    if path:
        path.write_text(payload, encoding="utf-8")
        typer.echo(f"Wrote {path}")
    else:
        sys.stdout.write(payload + "\n")


@app.command()
def reindex() -> None:
    """Rebuild the FTS5 index from the items table."""
    conn = _db()
    typer.echo(f"Reindexed {db.rebuild_index(conn)} items.")


@app.command()
def ingest(
    include_private: bool = typer.Option(
        False, "--include-private", help="Also ingest chat, mail, docs and console tabs."
    ),
    no_meta: bool = typer.Option(False, "--no-meta", help="Skip metadata lookup."),
    workers: int = typer.Option(8, "--workers", help="Concurrent metadata fetches."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be ingested."),
) -> None:
    """Pull every open Chrome tab into the library, with metadata."""
    conn = _db()

    if dry_run:
        entries, skipped = ingest_mod.collect_tabs(include_private)
        typer.echo(f"{len(entries)} unique pages would be ingested.")
        for reason, count in skipped.most_common():
            typer.echo(f"  skipped {count:>4}  {reason}")
        return

    report = ingest_mod.ingest(
        conn,
        include_private=include_private,
        fetch_meta=not no_meta,
        workers=workers,
        progress=lambda msg: typer.secho(f"  {msg}", fg=typer.colors.BRIGHT_BLACK),
    )

    typer.echo(
        f"\nIngested {report.total_saved} pages "
        f"({report.created} new, {report.updated} already known)."
    )
    if report.enriched_meta:
        typer.echo(f"Resolved metadata for {report.enriched_meta}.")
    if report.unloaded_recovered:
        typer.echo(
            f"Recovered {report.unloaded_recovered} titles for unloaded tabs by fetching the URL."
        )
    if report.needs_title:
        typer.secho(
            f"{len(report.needs_title)} pages have no title and could not be fetched.",
            fg=typer.colors.YELLOW,
        )
    for reason, count in report.skipped.most_common():
        typer.echo(f"  skipped {count:>4}  {reason}")
    for failure in report.failures[:5]:
        typer.secho(f"  failed: {failure}", fg=typer.colors.RED, err=True)


@app.command()
def recategorize(
    include_private: bool = typer.Option(False, "--include-private"),
    purge: bool = typer.Option(
        False, "--purge", help="Delete items that are now on the never-list."
    ),
) -> None:
    """Re-apply the bucket rules to everything already stored."""
    conn = _db()
    result = ingest_mod.recategorize(conn, include_private=include_private, purge=purge)

    for move, count in result["moved"].items():
        typer.echo(f"  moved {count:>4}  {move}")
    if not result["moved"]:
        typer.echo("  no items changed bucket")

    removable = result["removable"]
    if removable and not purge:
        total = sum(removable.values())
        typer.secho(f"\n{total} stored items are now on the never-list:", fg=typer.colors.YELLOW)
        for reason, count in removable.most_common():
            typer.echo(f"  {count:>4}  {reason}")
        typer.echo("Run again with --purge to delete them.")
    elif purge:
        for reason, count in result["purged"].items():
            typer.echo(f"  purged {count:>4}  {reason}")


@app.command()
def zotero(
    path: Optional[Path] = typer.Option(None, "--db", help="Path to zotero.sqlite."),
    limit: Optional[int] = typer.Option(None, "--limit"),
    no_collections: bool = typer.Option(False, "--no-collections"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report without writing."),
) -> None:
    """Import a Zotero library, read-only.

    The original file is copied and opened read-only, so Zotero may stay open.
    Collections become `zotero/...` tags and dateAdded becomes first_seen.
    """
    from . import zotero as zotero_mod

    found = path or zotero_mod.find_database()
    if found is None:
        _fail("no zotero.sqlite found (pass --db)")
    typer.echo(f"reading {found}")

    if dry_run:
        items = zotero_mod.read_items(found)
        with_url = sum(1 for i in items if i.best_url())
        typer.echo(f"  {len(items)} readings, {with_url} with a DOI or URL")
        from collections import Counter
        for kind, n in Counter(i.item_type for i in items).most_common(6):
            typer.echo(f"    {n:>5}  {kind}")
        collections = Counter(c for i in items for c in i.collections)
        typer.echo(f"  {len(collections)} collections would become zotero/ tags")
        return

    conn = _db()
    report = zotero_mod.import_library(
        conn, found, limit=limit, include_collections=not no_collections,
        progress=lambda m: typer.secho(f"  {m}", fg=typer.colors.BRIGHT_BLACK),
    )
    typer.echo(
        f"\n{report.created} new, {report.merged} merged into existing items "
        f"(of {report.seen} read)."
    )
    if report.collections_applied:
        typer.echo(f"{report.collections_applied} collection tags applied.")
    for reason, n in sorted(report.skipped.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  skipped {n:>5}  {reason}")


@app.command()
def backfill(
    workers: int = typer.Option(8, "--workers", help="Concurrent fetches."),
    limit: Optional[int] = typer.Option(None, "--limit"),
    force: bool = typer.Option(False, "--force", help="Re-fetch everything, not just gaps."),
) -> None:
    """Re-fetch metadata by URL for items that are missing it.

    A tab discarded by Chrome carries only a URL, and a site that was down at
    ingest time yields nothing. Both are recoverable later from the URL alone.
    """
    conn = _db()
    attempted, repaired = ingest_mod.backfill(
        conn,
        workers=workers,
        limit=limit,
        force=force,
        progress=lambda msg: typer.secho(f"  {msg}", fg=typer.colors.BRIGHT_BLACK),
    )
    if not attempted:
        typer.echo("Nothing to backfill -- every item already has metadata.")
        return
    typer.echo(f"Repaired {repaired} of {attempted} items.")
    still = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE title IS NULL OR title = ''"
    ).fetchone()["n"]
    if still:
        typer.secho(f"{still} items still have no title.", fg=typer.colors.YELLOW)


@app.command()
def organize() -> None:
    """Organize the whole library with the three-agent pipeline: propose a pool
    of concepts, build a hierarchy per facet, then tag every item per-item."""
    from . import topics

    conn = _db()
    try:
        backend = get_backend()
    except Exception as exc:
        _fail(str(exc))

    result = topics.organize_topics(
        conn, backend, progress=lambda m: typer.secho(f"  {m}", fg=typer.colors.BRIGHT_BLACK)
    )
    typer.echo(
        f"\nPool: {result['pool']} concepts · vocab: {result['vocab']} tags · "
        f"tagged {result['tagged']}/{result['items']} items · facets: {result.get('facets')}"
    )
    typer.echo(f"Backend usage: {backend.usage}")


@app.command(name="_run-job", hidden=True)
def _run_job(kind: str, job_id: str) -> None:
    """Internal: run a detached organize job, spawned by the API so it survives a
    server restart. Progress goes to the job's status file, not the terminal."""
    from . import jobs

    jobs.run(kind, job_id)


@app.command()
def evolve(
    apply: bool = typer.Option(False, "--apply", help="Apply the proposed changes."),
    limit: int = typer.Option(60, "--limit", help="Badly-filed items to show the model."),
) -> None:
    """Let codex restructure the taxonomy: new branches, deeper levels, merges.

    The vocabulary is frozen during classification so it cannot drift. This is
    the counterpart: a deliberate step where it is allowed to grow, given the
    items the current tree cannot place.
    """
    conn = _db()
    try:
        backend = get_backend()
    except Exception as exc:
        _fail(str(exc))

    unfit = organize_mod.poorly_filed(conn, limit)
    typer.secho(
        f"{len(unfit)} items are filed shallowly or not at all. Asking for structure...",
        fg=typer.colors.BRIGHT_BLACK,
    )
    plan = organize_mod.evolve_taxonomy(conn, backend, limit=limit)

    for name, why in plan["add"]:
        typer.echo(f"  + {name}")
        if why:
            typer.secho(f"      {why}", fg=typer.colors.BRIGHT_BLACK)
    for source, target, why in plan["rename"]:
        typer.echo(f"  ~ {source}  ->  {target}")
        if why:
            typer.secho(f"      {why}", fg=typer.colors.BRIGHT_BLACK)
    for sources, target, why in plan["merge"]:
        typer.echo(f"  = {' + '.join(sources)}  ->  {target}")
    for name, reason in plan["rejected"]:
        typer.secho(f"  x {name}: {reason}", fg=typer.colors.YELLOW)

    if not any(plan[k] for k in ("add", "rename", "merge")):
        typer.echo("No structural changes proposed.")
        return
    if not apply:
        typer.echo("\nDry run. Pass --apply to change the taxonomy.")
        return

    result = organize_mod.apply_plan(conn, plan)
    typer.echo(f"\nAdded {result['added']}, renamed {result['renamed']}, merged {result['merged']}.")
    typer.echo("Run `tt organize` to file items against the new branches.")


@app.command()
def refine(
    min_items: int = typer.Option(8, "--min-items", help="Split branches holding this many."),
    limit: int = typer.Option(10, "--limit", help="Branches to refine per run."),
    apply: bool = typer.Option(False, "--apply"),
) -> None:
    """Deepen crowded branches into sub-areas, driven by how many items sit there.

    Density is the trigger: one paper on continual learning for diffusion is a
    footnote, twelve are a sub-area. Children are added, never substituted, so
    the parent still shows everything beneath it -- and the same work can also
    live under an unrelated branch, because tags cross-cut.
    """
    conn = _db()
    try:
        backend = get_backend()
    except Exception as exc:
        _fail(str(exc))

    crowded = organize_mod.crowded_tags(conn, min_items)
    if not crowded:
        typer.echo(f"No branch holds {min_items}+ items without children.")
        return
    typer.secho(f"{len(crowded)} branches are candidates:", fg=typer.colors.BRIGHT_BLACK)
    for name, count in crowded[:limit]:
        typer.echo(f"    {count:>4}  {name}")

    results = organize_mod.refine(
        conn, backend, min_items=min_items, limit=limit, apply=apply,
        progress=lambda m: typer.secho(f"  {m}", fg=typer.colors.BRIGHT_BLACK),
    )
    typer.echo("")
    total = 0
    for result in results:
        if result.get("error"):
            typer.secho(f"  {result['tag']}: {result['error']}", fg=typer.colors.RED)
            continue
        for child, n in result["children"]:
            typer.echo(f"  + {child}  ({n} items)")
            total += 1
    typer.echo(f"\n{total} new sub-branches" + ("." if apply else " -- dry run, pass --apply."))


@app.command()
def consolidate(
    min_support: int = typer.Option(3, "--min-support", help="Fold leaves below this."),
    apply: bool = typer.Option(False, "--apply", help="Actually change the vocabulary."),
) -> None:
    """Vocabulary hygiene: fold under-used leaves, merge near-duplicate siblings.

    Deterministic and reproducible -- no model is involved, so the same library
    always consolidates the same way.
    """
    conn = _db()
    before = len(db.vocabulary(conn))
    result = organize_mod.consolidate(conn, min_support=min_support, dry_run=not apply)

    for loser, winner in result["merged"].items():
        typer.echo(f"  merge  {loser}  ->  {winner}")
    for leaf, parent in result["folded"].items():
        typer.echo(f"  fold   {leaf}  ->  {parent}")

    if not result["merged"] and not result["folded"]:
        typer.echo("Vocabulary is already clean.")
        return
    if apply:
        typer.echo(f"\n{before} -> {len(db.vocabulary(conn))} tags.")
    else:
        typer.echo("\nDry run. Pass --apply to make these changes.")


@app.command()
def dupes(
    threshold: float = typer.Option(0.93, "--threshold", help="Title similarity, 0-1."),
    merge: bool = typer.Option(False, "--merge", help="Fold each group into one item."),
) -> None:
    """Find (and optionally merge) the same paper stored under different URLs."""
    conn = _db()
    groups = organize_mod.find_duplicates(conn, threshold)
    if not groups:
        typer.echo("No duplicates found.")
        return

    for group in groups:
        typer.echo(f"\n{group[0]['title']}")
        for row in group:
            typer.echo(f"  {row['id']:>5}  {row['source'] or 'web':<10} {row['canonical_url']}")

    if not merge:
        typer.echo(f"\n{len(groups)} groups. Run with --merge to fold them together.")
        return

    removed = 0
    for group in groups:
        result = organize_mod.merge_group(conn, group)
        removed += len(result["merged"])
    typer.echo(f"\nMerged {len(groups)} groups, removed {removed} duplicate rows.")
    typer.echo("Merged URLs are kept as aliases, so re-ingesting will not recreate them.")


@app.command()
def ask(
    question: list[str] = typer.Argument(..., help="Your question."),
    limit: int = typer.Option(24, "--limit", help="Items to retrieve."),
) -> None:
    """Ask a question answered from your library, with citations."""
    conn = _db()
    try:
        backend = get_backend()
    except Exception as exc:
        _fail(str(exc))

    text = " ".join(question)
    typer.secho("Thinking...", fg=typer.colors.BRIGHT_BLACK)
    try:
        answer = ask_mod.ask(conn, backend, text, limit=limit)
    except BackendError as exc:
        _fail(str(exc))

    typer.echo(f"\n{answer.answer}\n")
    if answer.items:
        typer.echo("Cited:")
        for item in answer.items:
            typer.echo(f"  [{item['id']}] {item['title'] or item['canonical_url']}")
            typer.echo(f"        {item['canonical_url']}")
    typer.secho(f"\n(retrieved {answer.retrieved} items)", fg=typer.colors.BRIGHT_BLACK)


@app.command()
def backup(
    destination: Optional[Path] = typer.Argument(
        None, help="File or folder. Defaults to a cloud-synced folder."
    ),
    json_too: bool = typer.Option(True, "--json/--no-json", help="Also write a JSON export."),
    keep: int = typer.Option(10, "--keep", help="Backups to retain in the folder."),
) -> None:
    """Write a consistent copy of the library you can sync to the cloud."""
    conn = _db()
    # No explicit path -> honour the destination chosen in Settings (this is the
    # form the scheduled backup agent runs).
    result = (
        backup_mod.backup(conn, destination)
        if destination
        else backup_mod.configured_backup(conn)
    )
    size_mb = result.bytes_written / 1_000_000
    where = f" ({result.cloud})" if result.cloud else ""
    typer.echo(f"Backed up to {result.path}{where}")
    typer.echo(f"  {result.items} items, {result.tags} tags, {size_mb:.1f} MB")

    if json_too:
        path = backup_mod.export_json(conn, result.path.parent)
        typer.echo(f"  JSON export: {path}")
    removed = backup_mod.prune(result.path.parent, keep)
    if removed:
        typer.echo(f"  pruned {removed} old backups (keeping {keep})")


@app.command()
def restore(
    backup_path: Path = typer.Argument(..., help="Backup .db file to restore from."),
    force: bool = typer.Option(False, "--force", help="Overwrite the existing database."),
) -> None:
    """Restore the library from a backup file."""
    target = config.db_path()
    if target.exists() and not force:
        _fail(f"{target} already exists. Pass --force to overwrite it.")
    path = backup_mod.restore(backup_path, target, force=force)
    typer.echo(f"Restored {path}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port"),
) -> None:
    """Run the local web UI. Bound to localhost, no auth, no deployment."""
    try:
        import uvicorn
    except ImportError:
        _fail("the web UI needs extra deps: pip install -e '.[web]'")

    from .api import DIST, app as web_app

    if not (DIST / "index.html").exists():
        _fail("the UI is not built yet. Run:  cd ui && npm install && npm run build")

    from . import logs

    log = logs.get("serve")
    log.info("starting on http://%s:%d  (db %s)", host, port, config.db_path())
    typer.secho(f"cairn UI on http://{host}:{port}", fg=typer.colors.GREEN)
    uvicorn.run(web_app, host=host, port=port, log_level="warning")


@app.command(name="config")
def config_cmd(
    backend: Optional[str] = typer.Option(None, "--backend", help="direct | codex"),
    model: Optional[str] = typer.Option(None, "--model", help="model id, or 'default' for codex's own"),
    reasoning: Optional[str] = typer.Option(None, "--reasoning", help="low|medium|high|xhigh"),
    ask_model: Optional[str] = typer.Option(None, "--ask-model"),
    ask_reasoning: Optional[str] = typer.Option(None, "--ask-reasoning"),
    show: bool = typer.Option(False, "--show", help="Print the current settings."),
) -> None:
    """Store settings so every surface works without exported env vars."""
    from . import config as cfg

    if show or not any([backend, model, reasoning, ask_model, ask_reasoning]):
        current = cfg.load()
        typer.echo(f"config: {cfg.config_path()}")
        for key, value in (current or {"(empty)": ""}).items():
            typer.echo(f"  {key:<22} {value}")
        typer.echo("\nEnvironment variables override these for a single shell.")
        return

    saved = cfg.save(
        backend=backend, model=model, reasoning_effort=reasoning,
        ask_model=ask_model, ask_reasoning_effort=ask_reasoning,
    )
    typer.echo(f"Saved to {cfg.config_path()}")
    for key, value in saved.items():
        typer.echo(f"  {key:<22} {value}")


@app.command()
def autostart(
    port: int = typer.Option(8765, "--port"),
    interval: int = typer.Option(300, "--interval", help="Poller period in seconds."),
    autosave: int = typer.Option(3600, "--autosave", help="Auto-ingest period in seconds."),
    remove: bool = typer.Option(False, "--remove", help="Uninstall the agents."),
    show: bool = typer.Option(False, "--status", help="Show whether they are loaded."),
) -> None:
    """Start the UI and the poller at login, and keep them running."""
    from . import agents

    if show:
        for label, state in agents.status().items():
            typer.echo(f"  {label:<22} {state}")
        return
    if remove:
        removed = agents.uninstall()
        typer.echo(f"Removed {len(removed)} agents." if removed else "Nothing installed.")
        return

    written = agents.install(port=port, interval=interval, autosave=autosave)
    for path in written:
        typer.echo(f"  installed {path}")
    typer.echo(f"\nUI: http://127.0.0.1:{port}  (starts at login, restarts if it dies)")
    typer.echo(f"Poller: every {interval}s -- this is what builds first-seen dates.")
    typer.echo(f"Autosave: every {autosave // 60} min -- ingests open tabs into the library.")
    typer.secho(
        "launchd needs its own Chrome automation grant: approve the prompt the "
        "first time the poller runs.",
        fg=typer.colors.YELLOW,
    )




@app.command()
def stats() -> None:
    """What is in the library."""
    conn = _db()
    total = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
    if not total:
        typer.echo("Library is empty. Run `tt ingest`.")
        return
    tagged = conn.execute(
        "SELECT COUNT(DISTINCT item_id) AS n FROM item_tags"
    ).fetchone()["n"]
    typer.echo(f"{total} items, {tagged} filed, {total - tagged} untagged")
    typer.echo(f"database: {config.db_path()}")
    typer.echo("\nby source:")
    for row in conn.execute(
        "SELECT COALESCE(source,'web') AS s, COUNT(*) AS n FROM items "
        "GROUP BY s ORDER BY n DESC"
    ):
        typer.echo(f"  {row['n']:>5}  {row['s']}")
    typer.echo("\nby status:")
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM items GROUP BY status ORDER BY n DESC"
    ):
        typer.echo(f"  {row['n']:>5}  {row['status']}")


@app.command("prune")
def prune_cmd(
    apply: bool = typer.Option(False, "--apply", help="Actually delete."),
) -> None:
    """Remove model-proposed tags that ended up holding nothing."""
    conn = db.connect()
    doomed = db.prune_empty_tags(conn, dry_run=not apply)
    if not doomed:
        typer.echo("nothing to prune")
        return
    for name in doomed[:20]:
        typer.echo(f"  {name}")
    if len(doomed) > 20:
        typer.echo(f"  ... and {len(doomed) - 20} more")
    verb = "pruned" if apply else "would prune (pass --apply)"
    typer.echo(f"{verb}: {len(doomed)}")


@app.command("restructure")
def restructure_cmd(
    facet: str = typer.Argument(..., help="topic, method or task"),
    apply: bool = typer.Option(False, "--apply", help="Actually move the tags."),
) -> None:
    """Group a flat facet into families, so it can be browsed not scrolled."""
    conn = db.connect()
    backend = get_backend()
    result = organize_mod.restructure_facet(conn, backend, facet, dry_run=not apply)

    for source, target, why in result["moves"]:
        typer.echo(f"  {source}\n    -> {target}   {why}")
    for name, reason in result["rejected"]:
        typer.echo(f"  REJECTED {name}: {reason}")

    verb = "moved" if apply else "would move (pass --apply)"
    typer.echo(
        f"\n{facet}/: {result['before']} tags, {verb} {len(result['moves'])}, "
        f"rejected {len(result['rejected'])}"
    )


@app.command("absorb")
def absorb_cmd(
    facet: str = typer.Argument(..., help="topic, method or task"),
    threshold: int = typer.Option(3, help="Branches at or below this are thin."),
    apply: bool = typer.Option(False, "--apply", help="Actually move the tags."),
) -> None:
    """Fold sparse top-level branches into established families."""
    conn = db.connect()
    result = organize_mod.absorb_thin_branches(
        conn, get_backend(), facet, threshold=threshold, dry_run=not apply
    )
    for source, target, why in result["moves"]:
        typer.echo(f"  {source}\n    -> {target}   {why}")
    for name, reason in result["rejected"]:
        typer.echo(f"  LEFT ALONE {name}: {reason}")
    verb = "moved" if apply else "would move (pass --apply)"
    typer.echo(f"\n{facet}/: {verb} {len(result['moves'])}, kept {len(result['rejected'])}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
