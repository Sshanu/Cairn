"""File papers into the user's own described branches, with the agent's judgement.

The taxonomy pipeline only ever assigns its own computed and derived facets
(type/venue/topic/method/task). A researcher's custom hierarchy -- `project/vlm-
survey`, `reading/to-teach` -- is invisible to it, so those tags are applied by
hand. This module closes that gap *when the user asks for it* (the
`auto_file_projects` setting): each custom branch carries a one-line description
of what belongs there, and the agent decides, per paper, which of them fit.

It never invents branches and never touches the computed facets -- it only adds
one of the user's own tags to a paper that clearly belongs there, and says
nothing when none do.
"""

from __future__ import annotations

import sqlite3

from . import db
from .backends.base import BaseBackend

SYSTEM = (
    "You file a research paper into a researcher's OWN project folders. You are "
    "given the paper and a list of folders, each with a description of what "
    "belongs in it. Return only the folders this paper clearly belongs to.\n"
    "RULES\n"
    "- Match on what the paper is actually about vs the folder's description. When "
    "in doubt, leave it out: a wrong file is worse than a missing one.\n"
    "- Return folder names EXACTLY as given. Never invent a new one.\n"
    "- Most papers belong in none of the folders. An empty list is the common, "
    "correct answer."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "folders": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["folders"],
    "additionalProperties": False,
}


def file_into_projects(
    conn: sqlite3.Connection, backend: BaseBackend, item_id: int
) -> list[str]:
    """Tag one item with whichever described branches the agent says it fits."""
    targets = db.described_tags(conn)
    if not targets:
        return []
    item = db.get_item(conn, item_id)
    if item is None:
        return []
    title = (item["title"] or "").strip()
    abstract = (item["abstract"] or item["summary"] or "").strip()
    if not title and not abstract:
        return []  # nothing to judge on

    catalogue = "\n".join(f"- {name}: {desc}" for name, desc in targets)
    prompt = (
        f"Paper:\nTitle: {title}\nAbstract: {abstract[:1500]}\n\n"
        f"Folders:\n{catalogue}"
    )
    try:
        response = backend.invoke_json(prompt, schema=SCHEMA, system=SYSTEM, max_tokens=512)
    except Exception:
        return []

    valid = {name for name, _ in targets}
    chosen = [db.normalize_tag(f) for f in response.get("folders") or []]
    chosen = [c for c in chosen if c in valid]
    if chosen:
        db.add_tags(conn, item_id, chosen, origin="model")
    return chosen
