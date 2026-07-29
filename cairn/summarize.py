"""A two-line TL;DR and a 'why you saved this' line, generated once per paper.

The relevance line is what makes this more than a generic abstract: it is written
against the researcher's OWN library -- the topics they already track -- so it
says why *this* library holds the paper, not what the paper is about in the
abstract. Opt-in (the `auto_summary` setting) and cached on the row.
"""

from __future__ import annotations

import sqlite3

from . import db
from .backends.base import BaseBackend

SYSTEM = (
    "You write a compact card for a paper in a machine-learning researcher's "
    "library. Return two things:\n"
    "- tldr: at most two sentences, plain and concrete, on what the paper does "
    "and its key result. No hype, no 'this paper'.\n"
    "- relevance: ONE sentence on why a researcher who already tracks the listed "
    "topics would keep this -- the connection to their work, not a restatement of "
    "the abstract. If there is no clear connection, say what it pairs with.\n"
    "Ground the relevance in the researcher's topics; do not invent interests."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "tldr": {"type": "string"},
        "relevance": {"type": "string"},
    },
    "required": ["tldr", "relevance"],
    "additionalProperties": False,
}


def _library_topics(conn: sqlite3.Connection, limit: int = 12) -> list[str]:
    return [
        r["name"]
        for r in conn.execute(
            "SELECT t.name, COUNT(DISTINCT it.item_id) n FROM tags t "
            "JOIN item_tags it ON it.tag_id = t.id "
            "WHERE t.name LIKE 'topic/%' GROUP BY t.name ORDER BY n DESC LIMIT ?",
            (limit,),
        )
    ]


def summarize_item(conn: sqlite3.Connection, backend: BaseBackend, item_id: int) -> dict:
    """Generate and store the TL;DR + relevance for one item."""
    item = db.get_item(conn, item_id)
    if item is None:
        return {}
    title = (item["title"] or "").strip()
    abstract = (item["abstract"] or "").strip()
    if not title and not abstract:
        return {}

    own_tags = [t for t in db.item_tags(conn, item_id) if t.startswith(("topic/", "method/", "task/"))]
    context = own_tags or _library_topics(conn)
    prompt = (
        f"Paper:\nTitle: {title}\nAbstract: {abstract[:1800]}\n\n"
        f"The researcher's topics: {', '.join(context) or '(none yet)'}"
    )
    result = backend.invoke_json(prompt, schema=SCHEMA, system=SYSTEM, max_tokens=400)
    tldr = (result.get("tldr") or "").strip()
    relevance = (result.get("relevance") or "").strip()
    if tldr or relevance:
        conn.execute(
            "UPDATE items SET summary = COALESCE(NULLIF(?, ''), summary), relevance = ? WHERE id = ?",
            (tldr, relevance or None, item_id),
        )
        conn.commit()
        db.reindex_item(conn, item_id)
    return {"summary": tldr, "relevance": relevance}
