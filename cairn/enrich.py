"""Constrained-vocabulary tagging and summaries. The only model caller.

The discipline that keeps the vocabulary from exploding: the model is shown the
tags that already exist and asked to reuse them. Genuinely new tags come back in
a separate field and are only applied with --accept-new.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import db
from .backends.base import BackendError, BaseBackend

SYSTEM = (
    "You tag research papers and blog posts for one researcher's personal reading "
    "library. Tags are hierarchical paths, lowercase and hyphenated, at most two "
    "levels: area/topic (for example interp/attention, vlm/binding, eval/benchmarks).\n"
    "Reuse the existing vocabulary wherever a tag is a reasonable fit. Only propose a "
    "new tag when nothing existing describes a genuinely distinct topic, and put those "
    "in new_tags -- never in tags.\n"
    "Assign two to four tags. Write the summary in two or three sentences describing "
    "what the work does and what is novel about it, in plain prose with no preamble."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Tags drawn from the existing vocabulary, verbatim.",
        },
        "new_tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Proposed tags not present in the vocabulary.",
        },
        "summary": {"type": "string"},
    },
    "required": ["tags", "new_tags", "summary"],
    "additionalProperties": False,
}


@dataclass
class EnrichResult:
    item_id: int
    applied: list[str] = field(default_factory=list)
    proposed: list[str] = field(default_factory=list)
    summary: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def build_prompt(item: sqlite3.Row, vocabulary: list[str]) -> str:
    vocab = "\n".join(f"- {name}" for name in vocabulary) or "(empty -- propose new tags)"
    abstract = (item["abstract"] or item["body"] or "")[:6000]
    lines = [
        "Existing tag vocabulary:",
        vocab,
        "",
        "Item to tag:",
        f"Title: {item['title'] or '(unknown)'}",
    ]
    if item["authors"]:
        lines.append(f"Authors: {item['authors']}")
    if item["venue"] or item["year"]:
        lines.append(f"Venue: {item['venue'] or ''} {item['year'] or ''}".strip())
    lines.append(f"URL: {item['canonical_url']}")
    if abstract:
        lines += ["", "Abstract:", abstract]
    return "\n".join(lines)


def enrich_item(
    conn: sqlite3.Connection,
    backend: BaseBackend,
    item_id: int,
    *,
    accept_new: bool = False,
) -> EnrichResult:
    item = db.get_item(conn, item_id)
    if item is None:
        raise ValueError(f"no item {item_id}")

    vocabulary = db.vocabulary(conn)
    known = {db.normalize_tag(name) for name in vocabulary}

    # Ingest-time tagging obeys the same user controls as a full organise: the
    # free-text guidance, the tags they prefer, and the ones they deleted.
    from .organize import _augment_system

    response = backend.invoke_json(
        build_prompt(item, vocabulary),
        schema=SCHEMA,
        system=_augment_system(conn, SYSTEM),
        max_tokens=1024,
    )

    summary = (response.get("summary") or "").strip() or None
    suggested = [db.normalize_tag(t) for t in _as_list(response.get("tags"))]
    proposed = [db.normalize_tag(t) for t in _as_list(response.get("new_tags"))]

    # A tag the model claimed was existing but is not becomes a proposal.
    reused = [t for t in suggested if t and t in known]
    proposed += [t for t in suggested if t and t not in known]
    proposed = sorted({t for t in proposed if t})

    to_apply = list(reused)
    if accept_new:
        to_apply += proposed

    applied = db.add_tags(conn, item_id, to_apply, origin="model", confidence=None)
    db.mark_enriched(conn, item_id, summary)

    return EnrichResult(
        item_id=item_id,
        applied=applied,
        proposed=[] if accept_new else proposed,
        summary=summary,
    )


def enrich_batch(
    conn: sqlite3.Connection,
    backend: BaseBackend,
    limit: int = 20,
    *,
    accept_new: bool = False,
) -> list[EnrichResult]:
    results = []
    for item in db.unenriched(conn, limit):
        try:
            results.append(enrich_item(conn, backend, item["id"], accept_new=accept_new))
        except BackendError as exc:
            # Keep going through the batch, but never report a failure as a
            # success: enriched_at stays NULL so the item is retried next run.
            results.append(EnrichResult(item_id=item["id"], error=str(exc)))
    return results


def _as_list(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return []
