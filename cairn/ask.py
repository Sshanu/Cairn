"""Ask: free-text questions answered from the library, with citations.

Built on the index, not instead of it. FTS5 does the retrieval -- deterministic
and sub-millisecond -- and the model only reads what retrieval already found and
writes the synthesis. It can never invent an item that is not in your library,
because the only candidates it sees are rows that came out of the index.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from . import db
from .backends.base import BaseBackend

SYSTEM = (
    "You answer questions about one researcher's personal reading library.\n"
    "You are given the items retrieved for the question. Answer only from those "
    "items -- never from your own knowledge of the literature, and never mention a "
    "paper that is not in the list.\n"
    "Cite every claim with the bracketed id of the item it came from, like [42]. "
    "If the retrieved items do not answer the question, say so plainly and name the "
    "closest things that are there. Write prose, not bullets, and be concise."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "item_ids": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["answer", "item_ids"],
    "additionalProperties": False,
}

STOPWORDS = frozenset(
    """a an the of and or to in on for with what which how why me i my we
    that this these those is are was were be been do does did have has had
    read reading papers paper about anything something show find tell
    connects connect connecting relate related relates between versus vs
    give list name explain describe summarise summarize""".split()
)


@dataclass
class Answer:
    question: str
    answer: str
    items: list[sqlite3.Row] = field(default_factory=list)
    retrieved: int = 0


def keywords(question: str) -> list[str]:
    words = re.findall(r"[\w\-]+", (question or "").lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 2]


def retrieve(conn: sqlite3.Connection, question: str, limit: int = 24) -> list[sqlite3.Row]:
    """Precise matches first, then bm25-ranked partial matches.

    A natural-language question is not a boolean query. Requiring every keyword
    means one stray word returns nothing; searching each word separately returns
    whatever matches the commonest word. Neither finds the paper. OR with bm25
    ranks by how many of the terms a row actually matches, which is the thing
    the question is really asking.
    """
    terms = keywords(question)
    seen: dict[int, sqlite3.Row] = {}

    def collect(rows):
        for row in rows:
            seen.setdefault(row["id"], row)

    if terms:
        # Rows containing every term are the best answers when they exist.
        collect(db.search(conn, terms, limit=limit))
        if len(seen) < limit:
            collect(db.search(conn, terms, limit=limit - len(seen), mode="OR"))
    if not seen:
        collect(db.search(conn, [], limit=limit))
    return list(seen.values())[:limit]


def _context(items: list[sqlite3.Row], conn: sqlite3.Connection, abstract_chars: int = 1100) -> str:
    blocks = []
    for item in items:
        header = f"[{item['id']}] {item['title'] or item['canonical_url']}"
        bits = [b for b in (item["venue"], str(item["year"]) if item["year"] else None) if b]
        if bits:
            header += f" ({' '.join(bits)})"
        tags = db.item_tags(conn, item["id"])
        if tags:
            header += f"\n    tags: {' '.join(tags)}"
        # The summary is one sentence; the abstract is where the detail that
        # answers a specific question lives. Showing only one of them was why
        # a paper could be retrieved and still look irrelevant to the model.
        if item["summary"]:
            header += f"\n    {item['summary']}"
        if item["abstract"]:
            header += f"\n    {item['abstract'][:abstract_chars]}"
        blocks.append(header)
    return "\n\n".join(blocks)


def ask(
    conn: sqlite3.Connection,
    backend: BaseBackend,
    question: str,
    *,
    limit: int = 24,
) -> Answer:
    items = retrieve(conn, question, limit)
    if not items:
        return Answer(question, "The library is empty -- nothing to search yet.", [], 0)

    response = backend.invoke_json(
        f"Question: {question}\n\nRetrieved items:\n\n{_context(items, conn)}",
        schema=SCHEMA,
        system=SYSTEM,
        max_tokens=2048,
    )

    answer_text = str(response.get("answer") or "").strip()
    cited_ids = []
    for raw in response.get("item_ids") or []:
        try:
            cited_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    # Also honour ids cited inline, in case the list is incomplete.
    cited_ids += [int(m) for m in re.findall(r"\[(\d+)\]", answer_text)]

    by_id = {item["id"]: item for item in items}
    cited = []
    for item_id in dict.fromkeys(cited_ids):
        if item_id in by_id:
            cited.append(by_id[item_id])

    return Answer(question, answer_text, cited, len(items))
