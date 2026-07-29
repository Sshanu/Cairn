"""Bulk organisation: derive a taxonomy, then classify the whole library against it.

Enriching one item at a time cannot organise a backlog of hundreds: with an
empty vocabulary every tag is "new", so the constrained-vocabulary rule holds
everything back and nothing gets filed.

So this runs in two phases.

  1. Propose a taxonomy from every title at once, and freeze it.
  2. Classify in batches against that fixed vocabulary, several codex processes
     at a time.

Phase 2 is the constrained-vocabulary rule from doc.md section 8 applied at
scale: the model reuses the frozen tags and anything genuinely new is reported,
not silently added.
"""

from __future__ import annotations

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from . import db, taxonomy
from .backends.base import BackendError, BaseBackend

CLASSIFY_SYSTEM = (
    "GOAL: you are filing an item into a researcher's library so they can later "
    "find it by browsing its research areas and see the shape of a field. A tag "
    "is only useful if it is where a researcher would actually look for THIS "
    "item -- so precision matters far more than coverage.\n\n"
    "NOT EVERYTHING IS A PAPER. Items are papers, but also blog posts, code "
    "repositories, datasets, benchmarks, tools, documentation, talks and threads. "
    "Tag each for what it is ABOUT whatever its form -- a GitHub repo, a blog "
    "post or a dataset still has a subject and belongs somewhere.\n\n"
    "You file one item at a time into a faceted tag vocabulary. Prefer paths "
    "copied verbatim from the vocabulary you are given.\n\n"
    "SUBJECT, NOT TOOLS. Tag what the item is genuinely, centrally ABOUT and what "
    "it CONTRIBUTES -- never the machinery it uses or the era it sits in. A "
    "survey of audio description built on LLMs and VLMs is about accessibility / "
    "audio-description; it is NOT 'text-generation' and NOT 'vision-language-"
    "models' -- those are the tools and the framing. Do not tag a topic just "
    "because the item mentions or is built on it.\n\n"
    "FEW AND PRECISE BEATS MANY AND LOOSE. Aim for one to three tags at the "
    "DEEPEST node that truly fits. A fourth, looser tag that is only 'related' is "
    "wrong -- a tag that does not describe the work hides it. Tag two topics only "
    "when the item is genuinely about BOTH; if one is merely context, drop it.\n\n"
    "GROW THE TREE TO FIT THE ITEM. You are NOT confined to the existing "
    "hierarchy. If no vocabulary path fits the item precisely, do NOT force it "
    "into a loose existing tag -- propose a NEW full deep path in new_tags "
    "(topic/area/subarea/specific, nested under an existing top-level area when "
    "one fits, otherwise a new precise branch). A precise new path always beats a "
    "loose reused one; the tree is meant to grow to fit real work.\n\n"
    "NEVER assign a bare modality (vlm, llm, multimodal) or a vague process word "
    "(evaluation, architecture, analysis, methods, models) as a topic. Prefer the "
    "deepest tag that applies; use its parent only when the specifics are not "
    "evidenced.\n\n"
    "EVIDENCE. Some items have only a title, no abstract. Judge those "
    "conservatively: a title supports a broad area, rarely a specific claim.\n\n"
    "If nothing fits even after considering a new path, assign fewer tags -- or "
    "none, leaving the item for the researcher.\n\n"
    "Write each summary as one sentence describing what the work does, or leave "
    "it empty if only a title was given."
)

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "new_tags": {"type": "array", "items": {"type": "string"}},
                    "summary": {"type": "string"},
                },
                "required": ["id", "tags", "new_tags", "summary"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


@dataclass
class OrganizeReport:
    classified: int = 0
    tagged: int = 0
    unmatched: list[int] = field(default_factory=list)
    proposed: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)


def pending(conn: sqlite3.Connection, limit: int | None = None, redo: bool = False) -> list[sqlite3.Row]:
    """Items with no model-assigned tag yet (or everything, with redo).

    Deliberately ignores the computed facets. `type/`, `venue/` and `site/` are
    derived from the URL, so every ingested item already has tags; treating
    "has any tag" as "already filed" made organize a silent no-op that reported
    success having classified nothing.
    """
    sql = (
        "SELECT * FROM items i WHERE 1=1 "
        if redo
        else "SELECT * FROM items i WHERE NOT EXISTS ("
        " SELECT 1 FROM item_tags it JOIN tags t ON t.id = it.tag_id"
        " WHERE it.item_id = i.id"
        " AND (t.name LIKE 'topic/%' OR t.name LIKE 'method/%'"
        "      OR t.name LIKE 'task/%' OR t.name LIKE 'contribution/%')) "
    )
    sql += "ORDER BY i.id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql))


def _vocabulary_tree(conn: sqlite3.Connection) -> str:
    """Show the vocabulary as a tree, not a flat list.

    A flat list hides that `topic/vlm/efficiency/token-pruning` sits beside
    `topic/llm/efficiency/token-pruning`; indented, the choice is obvious.
    """
    lines = []
    for node in db.tag_tree(conn):
        indent = "  " * node["depth"]
        suffix = f"  ({node['count']})" if node["own"] else ""
        lines.append(f"{indent}{node['label']}{suffix}")
    return "\n".join(lines)


def _describe(item: sqlite3.Row, abstract_chars: int = 1200) -> str:
    parts = [f"[{item['id']}] {item['title'] or item['canonical_url']}"]
    meta_bits = [b for b in (item["venue"], str(item["year"]) if item["year"] else None) if b]
    if meta_bits:
        parts.append(f"    ({' '.join(meta_bits)})")
    abstract = (item["abstract"] or "")[:abstract_chars]
    if not abstract:
        # 41% of a real library has no abstract (paywalled venues, blocked APIs).
        # Saying so explicitly is what makes "judge conservatively" actionable.
        parts.append("    (TITLE ONLY -- no abstract available; tag conservatively)")
    if abstract:
        parts.append(f"    {abstract}")
    return "\n".join(parts)


# --- phase 1: taxonomy ------------------------------------------------------


def install_taxonomy(conn: sqlite3.Connection, taxonomy: list[tuple[str, str]]) -> int:
    """Create the tags so they exist as vocabulary before anything is filed."""
    now = db.utcnow()
    added = 0
    for name, _description in taxonomy:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO tags (name, created_at) VALUES (?, ?)", (name, now)
        )
        added += cursor.rowcount
    conn.commit()
    return added


# --- phase 2: classification ------------------------------------------------


def _augment_system(conn: sqlite3.Connection, base: str) -> str:
    """Fold the user's own guidance and their deleted tags into a system prompt.

    Two forms of user control over the agent: a free-text template ('keep
    diffusion separate from generation') and the memory of what they deleted (a
    tag they removed must not come back). Both are appended so every organising
    call obeys them.
    """
    from . import config

    extra: list[str] = []
    guidance = config.organization_guidance()
    if guidance and guidance.strip():
        extra.append(
            "THE RESEARCHER'S OWN ORGANISATION GUIDANCE -- follow it over any "
            "default instinct:\n" + guidance.strip()
        )
    preferred = db.user_preferred_tags(conn)
    if preferred:
        extra.append(
            "TAGS THE RESEARCHER USES BY HAND -- these express how they want the "
            "library organised. Prefer them whenever an item fits, and keep using "
            "them for new items instead of inventing a near-duplicate:\n"
            + "\n".join(f"- {name}" for name in preferred)
        )
    banned = db.banned_tags(conn)
    if banned:
        extra.append(
            "NEVER propose, reuse or recreate these tags -- the researcher deleted "
            "them on purpose:\n" + "\n".join(f"- {name}" for name in sorted(banned))
        )
    return base + "\n\n" + "\n\n".join(extra) if extra else base


def _classify_batch(
    backend: BaseBackend,
    batch: list[sqlite3.Row],
    vocabulary: list[str],
    tree: str = "",
    system: str = CLASSIFY_SYSTEM,
) -> dict:
    contributions = "\n".join(
        f"- {name}: {desc}" for name, desc in taxonomy.CONTRIBUTIONS.items()
    )
    prompt = "\n".join(
        [
            "For any item that is a paper, also assign exactly one contribution "
            "tag from this fixed list, describing what the paper ADDS (which is "
            "independent of what it is about -- a benchmark for hallucination and "
            "a method for hallucination share a topic and are used differently):",
            contributions,
            "",
            "Tag vocabulary, shown as a hierarchy. Use the full slash-separated "
            "path of a leaf, copied verbatim:",
            tree or "\n".join(f"- {name}" for name in vocabulary),
            "",
            "Full paths available:",
            "\n".join(f"- {name}" for name in vocabulary),
            "",
            f"File each of these {len(batch)} items. Return one object per item, "
            "keyed by the id in brackets.",
            "If an item needs a concept this vocabulary genuinely lacks, put the "
            "full proposed path in new_tags -- proposals that recur across the "
            "library are promoted into the vocabulary and applied.",
            "",
            "\n\n".join(_describe(item) for item in batch),
        ]
    )
    return backend.invoke_json(
        prompt,
        schema=CLASSIFY_SCHEMA,
        system=system,
        max_tokens=4096,
    )


EVOLVE_SYSTEM = (
    "You maintain a living faceted taxonomy for a researcher's library.\n"
    "You are shown the current taxonomy as a tree, and the items that it files "
    "badly -- untagged, filed only at a shallow node, or repeatedly needing a "
    "concept the vocabulary lacks.\n\n"
    "Propose STRUCTURAL changes so those items can be filed precisely:\n"
    "  add     a new tag, at any depth. Use this to open a new research area, "
    "and to deepen an existing one -- adding topic/vlm/efficiency/kv-cache under "
    "an existing topic/vlm/efficiency IS the way you add a level.\n"
    "  rename  a tag, moving its whole subtree with it.\n"
    "  merge   near-duplicate tags into the one that should survive.\n\n"
    "Rules that still hold: same facets (topic/, method/, task/); topic tags are "
    "at least 3 segments with NO upper limit; a broad word may be an intermediate "
    "node but never a leaf; name technique families, not individual variants; a "
    "new tag should fit at least two of the items shown.\n"
    "Change as little as possible. An existing tag that already fits is not "
    "improved by being renamed. Explain each change in one short clause."
)

EVOLVE_SCHEMA = {
    "type": "object",
    "properties": {
        "add": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["name", "description", "why"],
                "additionalProperties": False,
            },
        },
        "rename": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["from", "to", "why"],
                "additionalProperties": False,
            },
        },
        "merge": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sources": {"type": "array", "items": {"type": "string"}},
                    "target": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["sources", "target", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["add", "rename", "merge"],
    "additionalProperties": False,
}


def poorly_filed(conn: sqlite3.Connection, limit: int = 60) -> list[sqlite3.Row]:
    """Items the current taxonomy cannot place precisely.

    Untagged items, and items whose only topic tag is a shallow one -- both are
    signals that the vocabulary is missing a branch rather than that the item is
    unusual.
    """
    return list(
        conn.execute(
            """
            SELECT i.* FROM items i
            WHERE NOT EXISTS (
                    SELECT 1 FROM item_tags it JOIN tags t ON t.id = it.tag_id
                    WHERE it.item_id = i.id AND t.name LIKE 'topic/%/%/%'
                  )
            ORDER BY i.id LIMIT ?
            """,
            (limit,),
        )
    )


def evolve_taxonomy(
    conn: sqlite3.Connection,
    backend: BaseBackend,
    *,
    proposals: dict[str, int] | None = None,
    limit: int = 60,
) -> dict:
    """Ask for structural changes to the taxonomy. Returns a reviewable plan."""
    items = poorly_filed(conn, limit)
    lines = [
        "Current taxonomy:",
        _vocabulary_tree(conn) or "(empty)",
        "",
    ]
    if items:
        lines += [
            f"{len(items)} items the taxonomy files badly:",
            "\n\n".join(_describe(item, 600) for item in items),
            "",
        ]
    if proposals:
        lines += [
            "Concepts the classifier reached for but could not express:",
            "\n".join(f"- {name} (x{count})" for name, count in proposals.items()),
        ]

    response = backend.invoke_json(
        "\n".join(lines), schema=EVOLVE_SCHEMA, system=EVOLVE_SYSTEM, max_tokens=8192
    )

    vocabulary = set(db.vocabulary(conn))
    plan: dict[str, list] = {"add": [], "rename": [], "merge": [], "rejected": []}

    for entry in response.get("add") or []:
        name = db.normalize_tag(str(entry.get("name") or ""))
        reason = taxonomy.reject_reason(name) if name else "empty"
        if reason:
            plan["rejected"].append((name, reason))
        elif name in vocabulary:
            plan["rejected"].append((name, "already exists"))
        else:
            plan["add"].append((name, str(entry.get("why") or "")))

    for entry in response.get("rename") or []:
        source = db.normalize_tag(str(entry.get("from") or ""))
        target = db.normalize_tag(str(entry.get("to") or ""))
        reason = taxonomy.reject_reason(target) if target else "empty"
        if source not in vocabulary:
            plan["rejected"].append((source, "no such tag"))
        elif reason:
            plan["rejected"].append((target, reason))
        else:
            plan["rename"].append((source, target, str(entry.get("why") or "")))

    for entry in response.get("merge") or []:
        target = db.normalize_tag(str(entry.get("target") or ""))
        sources = [db.normalize_tag(str(s)) for s in entry.get("sources") or []]
        sources = [s for s in sources if s and s in vocabulary and s != target]
        if target and sources:
            plan["merge"].append((sources, target, str(entry.get("why") or "")))

    return plan


def apply_plan(conn: sqlite3.Connection, plan: dict) -> dict:
    """Apply an evolve plan. Additions first, so renames can target them."""
    now = db.utcnow()
    for name, _why in plan.get("add", []):
        conn.execute(
            "INSERT OR IGNORE INTO tags (name, created_at) VALUES (?, ?)", (name, now)
        )
    conn.commit()

    for source, target, _why in plan.get("rename", []):
        db.rename_tag(conn, source, target)
    for sources, target, _why in plan.get("merge", []):
        db.merge_tags(conn, sources, target)

    return {
        "added": len(plan.get("add", [])),
        "renamed": len(plan.get("rename", [])),
        "merged": sum(len(s) for s, _t, _w in plan.get("merge", [])),
    }


REFINE_SYSTEM = (
    "You deepen one branch of a research taxonomy.\n"
    "You are given a tag and every item filed under it. Decide whether those "
    "items contain coherent sub-groups that deserve their own child tags.\n\n"
    "Split only where the split is real. A sub-group must:\n"
    "  - contain at least 3 of the items shown,\n"
    "  - be a genuine KIND OF the parent (reading '<child> is a kind of "
    "<parent>' aloud must be true),\n"
    "  - be something a researcher would deliberately navigate to.\n"
    "If the items are already homogeneous, return no children. A branch that "
    "does not need splitting is a good branch.\n\n"
    "A modality (vlm, llm, diffusion, video) IS a valid child here -- "
    "'continual learning for diffusion models' is a real sub-area of continual "
    "learning. That same work may also live under its own diffusion branch "
    "elsewhere in the taxonomy; tags are not exclusive, and cross-cutting is "
    "expected rather than a conflict.\n\n"
    "Assign every item you list to exactly one proposed child, by id."
)

REFINE_SCHEMA = {
    "type": "object",
    "properties": {
        "children": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "why": {"type": "string"},
                    "item_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["name", "why", "item_ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["children"],
    "additionalProperties": False,
}


def crowded_tags(
    conn: sqlite3.Connection, min_items: int = 8, facets: tuple[str, ...] = ("topic/",)
) -> list[tuple[str, int]]:
    """Leaf tags holding enough items that a split would help.

    Density is the signal for when to deepen. One paper on continual learning
    for diffusion is a footnote; twelve of them are a sub-area, and the
    taxonomy should grow a branch to match rather than leaving them in a pile.
    """
    counts = dict(db.tag_counts(conn))
    out = []
    for name, count in counts.items():
        if not name.startswith(facets) or count < min_items:
            continue
        has_children = any(o.startswith(name + "/") for o in counts if o != name)
        if not has_children:
            out.append((name, count))
    return sorted(out, key=lambda kv: -kv[1])


def refine_tag(
    conn: sqlite3.Connection,
    backend: BaseBackend,
    tag: str,
    *,
    min_child: int = 3,
    apply: bool = True,
) -> dict:
    """Split one crowded leaf into children, and re-file its items."""
    rows = list(
        conn.execute(
            "SELECT i.* FROM items i JOIN item_tags it ON it.item_id = i.id "
            "JOIN tags t ON t.id = it.tag_id WHERE t.name = ? ORDER BY i.id",
            (tag,),
        )
    )
    if len(rows) < min_child * 2:
        return {"tag": tag, "children": [], "reason": "too few items to split"}

    prompt = "\n".join(
        [
            f"Tag: {tag}",
            f"{len(rows)} items are filed here:",
            "",
            "\n\n".join(_describe(row, 500) for row in rows),
        ]
    )
    response = backend.invoke_json(
        prompt, schema=REFINE_SCHEMA, system=REFINE_SYSTEM, max_tokens=6144
    )

    created: list[tuple[str, int]] = []
    for entry in response.get("children") or []:
        if not isinstance(entry, dict):
            continue
        raw = db.normalize_tag(str(entry.get("name") or ""))
        if not raw:
            continue
        # Accept a bare leaf name or a full path, but always hang it off `tag`.
        child = raw if raw.startswith(tag + "/") else f"{tag}/{raw.split('/')[-1]}"
        ids = [int(i) for i in entry.get("item_ids") or [] if isinstance(i, int)]
        ids = [i for i in ids if any(r["id"] == i for r in rows)]
        if len(ids) < min_child or taxonomy.reject_reason(child):
            continue

        if apply:
            for item_id in ids:
                db.add_tags(conn, item_id, [child], origin="model")
                # The parent stays: a child is a refinement, not a replacement,
                # so browsing the parent still shows everything beneath it.
        created.append((child, len(ids)))

    return {"tag": tag, "children": created}


def refine(
    conn: sqlite3.Connection,
    backend: BaseBackend,
    *,
    min_items: int = 8,
    limit: int = 10,
    apply: bool = True,
    progress=None,
) -> list[dict]:
    """Deepen every branch that has grown crowded enough to warrant it."""
    results = []
    for tag, count in crowded_tags(conn, min_items)[:limit]:
        if progress:
            progress(f"refining {tag} ({count} items)")
        try:
            results.append(refine_tag(conn, backend, tag, apply=apply))
        except BackendError as exc:
            results.append({"tag": tag, "children": [], "error": str(exc)})
    return results


def grow_vocabulary(
    conn: sqlite3.Connection,
    proposed: dict[str, int],
    *,
    min_support: int = 3,
) -> list[str]:
    """Promote recurring proposals into the vocabulary.

    The constrained-vocabulary rule exists so a taxonomy cannot drift, not so it
    can never learn. A concept the classifier reached for once is noise; one it
    reached for across several independent batches is a genuine gap, and the
    support threshold is what tells them apart without a human in the loop.
    """
    promoted = []
    for name, count in proposed.items():
        if count < min_support:
            continue
        if taxonomy.reject_reason(name):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO tags (name, created_at) VALUES (?, ?)",
            (name, db.utcnow()),
        )
        promoted.append(name)
    conn.commit()
    return promoted


# --- duplicate detection (deterministic, no model) --------------------------


def _near_duplicate(a: str, b: str, threshold: float) -> bool:
    """Two leaf names that mean the same thing, not merely look alike.

    Raw string similarity is dangerous here, because the differing part is
    usually the whole point: `medical-text-understanding` and
    `legal-text-understanding` score 0.86 and are opposites, as do
    `causal-representation-learning` and `visual-representation-learning`.

    So compare word sets. A merge is only allowed when the words are identical
    up to ordering, or differ solely by a plural or a filler word -- which is
    what an actual near-duplicate looks like.
    """
    filler = {"of", "for", "and", "the", "in", "on", "with", "based"}

    def words(name: str) -> set[str]:
        out = set()
        for token in name.split("-"):
            token = token.strip()
            if not token or token in filler:
                continue
            out.add(token[:-1] if len(token) > 3 and token.endswith("s") else token)
        return out

    wa, wb = words(a), words(b)
    if not wa or not wb:
        return False
    if wa == wb:
        return True

    # One extra filler-ish word is tolerable; a different content word is not.
    difference = wa.symmetric_difference(wb)
    if len(difference) > 1:
        return False
    # The single differing word must itself be a near-miss spelling of one it
    # sits beside, e.g. `visualisation` vs `visualization`.
    odd = difference.pop()
    return any(
        SequenceMatcher(None, odd, other).ratio() >= threshold
        for other in (wa | wb) - {odd}
    )



RESTRUCTURE_SYSTEM = (
    "You regroup one facet of a research library's tag taxonomy so it can be "
    "browsed instead of scrolled.\n\n"
    "You are given every tag currently in one facet, flat, with the number of "
    "items each holds. Your job is to impose a hierarchy on them: choose a small "
    "set of families, and place every existing tag underneath one.\n\n"
    "RULES\n"
    "- Aim for 8-15 families. Fewer means a family that means nothing; more means "
    "you are still flat.\n"
    "- Every tag must end up at least 3 segments deep: facet/family/specific.\n"
    "- PRESERVE THE MEANING. You are moving a tag, not renaming the concept. The "
    "words of the original leaf must survive in the new path. "
    "task/visual-question-answering may become task/question-answering/visual; it "
    "may NOT become task/vision/understanding. If you cannot place a tag without "
    "changing what it means, leave it out of your answer entirely.\n"
    "- Every child must be a genuine KIND OF its parent, read aloud: 'visual "
    "question answering is a kind of question answering' is true; 'counting is a "
    "kind of detection' is not.\n"
    "- Tags that share a word almost always share a family. Group them.\n"
    "- A family may itself be an existing tag: if task/question-answering already "
    "exists and holds items, it is the natural parent for the -question-answering "
    "siblings, and those items stay where they are.\n"
    "- If a tag is already the natural family for others (task/question-answering, "
    "with several -question-answering siblings), LEAVE IT OUT of your answer and "
    "move the siblings under it. Never emit task/summarization -> "
    "task/summarization/summarization; a family does not nest inside itself.\n"
    "- A family name is ONE concept, not two welded together. 'agent-navigation' "
    "is two families pretending to be one -- pick navigation, or pick agent, and "
    "put the tags that do not fit somewhere else.\n"
    "- Drop the redundant repetition when you nest: task/visual-question-answering "
    "under task/question-answering becomes task/question-answering/visual, not "
    "task/question-answering/visual-question-answering.\n"
    "- Leave a tag out rather than forcing it. Omission is a valid answer."
)

RESTRUCTURE_SCHEMA = {
    "type": "object",
    "properties": {
        "moves": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["from", "to", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moves"],
    "additionalProperties": False,
}


def _preserves_meaning(source: str, target: str) -> bool:
    """Does the target path still say what the source tag said?

    The failure this guards against is the model quietly relabelling rather
    than regrouping -- turning task/counting into task/detection/objects, which
    looks tidy and files the papers under something they are not about. Every
    content word of the original leaf has to reappear somewhere in the new path.
    """
    stop = {"of", "and", "the", "for", "in", "to", "a", "based", "with"}
    leaf_words = {w for w in source.split("/")[-1].split("-") if w and w not in stop}
    target_words = {
        w for segment in target.split("/") for w in segment.split("-") if w
    }
    return leaf_words <= target_words


ABSORB_SYSTEM = (
    "A research library's tag facet has a handful of thin top-level branches -- "
    "each holding only a couple of items -- sitting beside a dozen well-populated "
    "families. The thin ones make the facet harder to browse without earning "
    "their place.\n\n"
    "For each thin branch, say which established family it belongs under, or "
    "leave it alone.\n\n"
    "RULES\n"
    "- You may only move a thin branch UNDER one of the established families "
    "listed. Do not invent new families.\n"
    "- The is-a test must hold: '<thin> is a kind of <family>' has to be true to "
    "a researcher. If no family fits, LEAVE IT OUT. A thin branch standing alone "
    "is better than one filed under something it is not.\n"
    "- Preserve meaning: the words of the thin branch must survive in the new "
    "path. You are moving it, not renaming it.\n"
    "- Moving a branch takes its whole subtree with it, so judge the branch by "
    "what it covers, not just its name."
)


def absorb_thin_branches(
    conn: sqlite3.Connection,
    backend: BaseBackend,
    facet: str,
    *,
    threshold: int = 3,
    dry_run: bool = True,
) -> dict:
    """Fold sparse top-level branches into established families.

    Restructuring groups the bulk of a facet, but always leaves a tail of
    branches too small to have attracted siblings. They are the ones that make a
    sidebar look cluttered while holding almost nothing.
    """
    rows = conn.execute(
        "SELECT t.name, ("
        "  SELECT COUNT(DISTINCT it.item_id) FROM item_tags it"
        "  JOIN tags c ON c.id = it.tag_id"
        "  WHERE c.name = t.name OR c.name LIKE t.name || '/%'"
        ") AS n FROM tags t WHERE t.name LIKE ? AND t.name NOT LIKE ? ORDER BY n DESC",
        (facet + "/%", facet + "/%/%"),
    ).fetchall()
    thin = [r for r in rows if r["n"] <= threshold]
    families = [r for r in rows if r["n"] > threshold]
    if not thin or not families:
        return {"facet": facet, "moves": [], "applied": [], "rejected": [], "before": len(rows)}

    prompt = (
        f"Facet: {facet}/\n\nEstablished families:\n"
        + "\n".join(f"{r['name']}  ({r['n']} items)" for r in families)
        + f"\n\nThin branches to place ({len(thin)}):\n"
        + "\n".join(f"{r['name']}  ({r['n']} items)" for r in thin)
    )
    response = backend.invoke_json(
        prompt, schema=RESTRUCTURE_SCHEMA, system=_augment_system(conn, ABSORB_SYSTEM), max_tokens=8192
    )

    thin_names = {r["name"] for r in thin}
    family_names = {r["name"] for r in families}
    moves: list[tuple[str, str, str]] = []
    rejected: list[tuple[str, str]] = []

    for entry in response.get("moves") or []:
        source = db.normalize_tag(str(entry.get("from") or ""))
        target = db.normalize_tag(str(entry.get("to") or ""))
        why = str(entry.get("why") or "")
        if not source or not target or source == target:
            continue
        parent = "/".join(target.split("/")[:-1])
        if source not in thin_names:
            rejected.append((source, "not a thin branch"))
        elif parent not in family_names:
            rejected.append((target, f"{parent!r} is not an established family"))
        elif not _preserves_meaning(source, target):
            rejected.append((target, f"changes what {source!r} means"))
        elif (reason := taxonomy.reject_reason(target)) is not None:
            rejected.append((target, reason))
        else:
            moves.append((source, target, why))

    applied = []
    if not dry_run:
        for source, target, why in moves:
            db.rename_tag(conn, source, target)
            applied.append((source, target, why))
        db.materialize_ancestors(conn)

    return {
        "facet": facet,
        "moves": moves,
        "applied": applied,
        "rejected": rejected,
        "before": len(rows),
    }


def restructure_facet(
    conn: sqlite3.Connection,
    backend: BaseBackend,
    facet: str,
    *,
    dry_run: bool = True,
) -> dict:
    """Group a flat facet into families, as renames the DB already understands.

    The model only proposes the grouping; every move is checked against the
    shape rules and against meaning-preservation before anything is applied.
    """
    rows = conn.execute(
        "SELECT t.name, ("
        "  SELECT COUNT(DISTINCT it.item_id) FROM item_tags it"
        "  JOIN tags c ON c.id = it.tag_id"
        "  WHERE c.name = t.name OR c.name LIKE t.name || '/%'"
        ") AS n FROM tags t WHERE t.name LIKE ? ORDER BY n DESC",
        (facet + "/%",),
    ).fetchall()
    if not rows:
        return {"moves": [], "rejected": [], "facet": facet}

    listing = "\n".join(f"{r['name']}  ({r['n']} items)" for r in rows)
    prompt = (
        f"Facet: {facet}/\n"
        f"{len(rows)} tags, {sum(1 for r in rows if r['name'].count('/') == 1)} of "
        f"them at the top level.\n\n{listing}"
    )
    response = backend.invoke_json(
        prompt,
        schema=RESTRUCTURE_SCHEMA,
        system=_augment_system(conn, RESTRUCTURE_SYSTEM),
        max_tokens=16384,
    )

    existing = {r["name"] for r in rows}
    moves: list[tuple[str, str, str]] = []
    rejected: list[tuple[str, str]] = []
    seen_targets: set[str] = set()

    for entry in response.get("moves") or []:
        source = db.normalize_tag(str(entry.get("from") or ""))
        target = db.normalize_tag(str(entry.get("to") or ""))
        why = str(entry.get("why") or "")
        if not source or not target or source == target:
            continue
        if source not in existing:
            rejected.append((source, "no such tag"))
        elif not target.startswith(facet + "/"):
            rejected.append((target, f"leaves the {facet}/ facet"))
        elif (reason := taxonomy.reject_reason(target)) is not None:
            rejected.append((target, reason))
        elif not _preserves_meaning(source, target):
            rejected.append((target, f"changes what {source!r} means"))
        elif target.split("/")[-1] == target.split("/")[-2]:
            # task/summarization/summarization: the tag was already the family.
            # Leaving it alone lets its siblings nest under it instead.
            rejected.append((target, "a family cannot nest inside itself"))
        elif target in seen_targets:
            rejected.append((target, "two tags moved to one place"))
        else:
            seen_targets.add(target)
            moves.append((source, target, why))

    applied = []
    if not dry_run:
        # Deepest first: moving a parent before its children would strand them.
        for source, target, why in sorted(moves, key=lambda m: -m[0].count("/")):
            db.rename_tag(conn, source, target)
            applied.append((source, target, why))
        # The new families are implied by their children; give them real rows so
        # they can be renamed, pinned and deleted like anything else.
        db.materialize_ancestors(conn)

    return {
        "facet": facet,
        "moves": moves,
        "applied": applied,
        "rejected": rejected,
        "before": len(rows),
    }


def consolidate(
    conn: sqlite3.Connection,
    *,
    min_support: int = 3,
    sibling_similarity: float = 0.86,
    dry_run: bool = False,
) -> dict:
    """Deterministic vocabulary hygiene. No model, so it is reproducible.

    Two rules, both applied bottom-up so a fold can cascade:

    1. Minimum support -- a leaf covering fewer than `min_support` items is
       folded into its parent. This is what stops one-off variant names:
       `method/peft/dora` on a single paper becomes `method/peft`, where it
       belongs, instead of standing beside `method/peft/lora` as if it were a
       peer.
    2. Sibling near-duplicates -- two tags under the same parent whose leaf
       names are nearly the same string are merged into the more common one.

    A top-level facet is never folded away, and a leaf with no parent to fold
    into is left alone rather than deleted: losing the tag entirely would lose
    information, which is worse than an over-specific tag.
    """
    folded: dict[str, str] = {}
    merged: dict[str, str] = {}

    # Only model-assigned facets are consolidated. type/, venue/ and site/ are
    # computed from the URL and are correct by construction: `venue/cvpr/2018`
    # holding two papers is not an over-specific tag, it is the fact that you
    # read two CVPR 2018 papers, and folding it away would destroy the axis.
    editable = ("topic/", "method/", "task/", "contribution/")

    counts = {
        name: n
        for name, n in db.tag_counts(conn)
        if name.startswith(editable)
    }
    names = sorted(counts, key=lambda n: n.count("/"), reverse=True)

    # Rule 2 first: merging siblings can lift one of them over the support bar.
    for name in list(names):
        if name in merged or name.count("/") < 1:
            continue
        parent = name.rsplit("/", 1)[0]
        leaf = name.rsplit("/", 1)[1]
        for other in names:
            if other == name or other in merged or not other.startswith(parent + "/"):
                continue
            if other.count("/") != name.count("/"):
                continue
            other_leaf = other.rsplit("/", 1)[1]
            if not _near_duplicate(leaf, other_leaf, sibling_similarity):
                continue
            loser, winner = sorted(
                (name, other), key=lambda n: (counts.get(n, 0), n), reverse=True
            )[1], sorted(
                (name, other), key=lambda n: (counts.get(n, 0), n), reverse=True
            )[0]
            merged[loser] = winner
            break

    if not dry_run:
        for loser, winner in merged.items():
            db.rename_tag(conn, loser, winner)

    # Rule 1, recomputed after any merges.
    counts = {n: c for n, c in db.tag_counts(conn) if n.startswith(editable)}
    for name in sorted(counts, key=lambda n: n.count("/"), reverse=True):
        if counts.get(name, 0) >= min_support or name.count("/") < 2:
            continue  # keep facets and second-level tags regardless of support
        has_children = any(
            other.startswith(name + "/") for other in counts if other != name
        )
        if has_children:
            continue
        # A modality leaf carries a distinction that support alone cannot judge:
        # token pruning in a VLM and in an LLM are different problems even when
        # only one paper sits under each, so folding them together loses the
        # separation rather than tidying it.
        if name.rsplit("/", 1)[1] in taxonomy.MODALITIES:
            continue
        parent = name.rsplit("/", 1)[0]
        folded[name] = parent
        if not dry_run:
            db.rename_tag(conn, name, parent)
            counts = {n: c for n, c in db.tag_counts(conn) if n.startswith(editable)}

    return {
        "merged": merged,
        "folded": folded,
        "before": len(db.vocabulary(conn)) + len(merged) + len(folded)
        if dry_run
        else None,
        "after": len(db.vocabulary(conn)),
    }


def _title_key(title: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (title or "").lower()).strip()


def _same_work(a: sqlite3.Row, b: sqlite3.Row, threshold: float) -> bool:
    """Two rows describe the same work.

    Fuzzy title matching is only safe for papers, whose titles are long and
    distinctive. Two HuggingFace pages named `config.json - InternVL3_5-8B` and
    `config.json - InternVL3-8B` score 0.95 similar and are entirely different
    models, so anything that is not a paper has to match exactly.
    """
    key_a, key_b = _title_key(a["title"]), _title_key(b["title"])
    if key_a == key_b:
        return True

    from .canonical import is_paper

    if not (is_paper(a["source"] or "web") or is_paper(b["source"] or "web")):
        return False
    if min(len(key_a), len(key_b)) < 30:
        return False
    return SequenceMatcher(None, key_a, key_b).ratio() >= threshold


# Which link survives a merge, highest kept. The real conference page beats
# everything; arXiv beats a bare doi.org (more readable, and the DOI is often
# just arXiv's own); web is last. Metadata is merged field-by-field regardless,
# so preferring the conference URL never loses the richer abstract.
_SOURCE_RANK = {
    "acl": 5, "cvf": 5, "ecva": 5, "neurips": 5, "pmlr": 5, "aaai": 5, "ieee": 5,
    "arxiv": 3,
    "openreview": 2,
    "doi": 1,
    "web": 0,
}


def fix_taxonomy(conn: sqlite3.Connection, backend: BaseBackend, *, progress=None) -> dict:
    """One reviewed pass that reshapes the whole taxonomy the way the user wants.

    Runs the same primitives the CLI exposes, in the order that makes each one's
    job easier for the next: regroup every facet into families, fold the thin
    tail in, collapse single-child chains, drop empty proposed tags, and give the
    new families real rows. Every model call is steered by the user's guidance
    box and their deleted tags (via `_augment_system`), so this is "reorganise
    it the way I said", not "reorganise it however the model feels".
    """
    summary = {"restructured": 0, "absorbed": 0, "collapsed": 0, "site_removed": 0, "pruned": 0}
    for facet in ("topic", "method", "task"):
        if progress:
            progress(f"regrouping {facet}/ into families")
        try:
            summary["restructured"] += len(restructure_facet(conn, backend, facet, dry_run=False)["applied"])
            summary["absorbed"] += len(absorb_thin_branches(conn, backend, facet, dry_run=False)["applied"])
        except (BackendError, ValueError) as exc:
            if progress:
                progress(f"  {facet}: {exc}")

    if progress:
        progress("collapsing single-child branches and stray site tags")
    tidy = cleanup_taxonomy(conn, progress=progress)
    summary["collapsed"] = len(tidy["collapsed"])
    summary["site_removed"] = tidy["site_removed"]

    summary["pruned"] = len(db.prune_empty_tags(conn))
    db.materialize_ancestors(conn)
    if progress:
        progress(
            f"done: {summary['restructured']} regrouped, {summary['collapsed']} collapsed, "
            f"{summary['pruned']} empty pruned"
        )
    return summary


def cleanup_taxonomy(conn: sqlite3.Connection, progress=None) -> dict:
    """Two structural repairs that keep the tree honest.

    1. Strip `site/` tags off papers: a paper is browsed by venue, so a
       `site/arxiv` next to `venue/arxiv` is pure noise the old pipeline left.
    2. Collapse a modality leaf that is the ONLY child of its parent. A branch
       like topic/reasoning/visual-chain-of-thought/vlm adds a level and no
       information -- the parent already implies the modality, and there is no
       sibling to tell it apart from. The items move up; the leaf goes.
    """
    from .canonical import PAPER_SOURCES

    # 1a. site/ on papers -- a paper is browsed by venue, not site.
    site_removed = conn.execute(
        "DELETE FROM item_tags WHERE tag_id IN (SELECT id FROM tags WHERE name LIKE 'site/%') "
        "AND item_id IN (SELECT id FROM items WHERE source IN "
        f"({','.join('?' for _ in PAPER_SOURCES)}))",
        tuple(PAPER_SOURCES),
    ).rowcount

    # 1b. site/ for a one-off domain is noise. A site branch is only worth having
    # when a domain recurs, so drop any site tag held by fewer than three items.
    thin_site_ids = [
        r["id"]
        for r in conn.execute(
            "SELECT t.id FROM tags t WHERE t.name LIKE 'site/%' AND ("
            "  SELECT COUNT(*) FROM item_tags it WHERE it.tag_id = t.id) < 3"
        )
    ]
    for tag_id in thin_site_ids:
        site_removed += conn.execute(
            "DELETE FROM item_tags WHERE tag_id = ?", (tag_id,)
        ).rowcount
        conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))

    conn.execute(
        "DELETE FROM tags WHERE name LIKE 'site/%' AND id NOT IN "
        "(SELECT tag_id FROM item_tags)"
    )
    conn.commit()

    # 2. Single-child branches. A parent with exactly one child adds a level and
    #    no information -- the parent and the lone child hold the same items. Fold
    #    the child up into the parent. Done deepest-first and repeated, so a whole
    #    single-child chain (reasoning/mathematical/llm) collapses in one call.
    #    Multi-child categories protect themselves: the pass simply skips them, so
    #    only genuinely lone branches ever move.
    def load_names() -> set[str]:
        return {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM tags WHERE name LIKE 'topic/%' OR name LIKE 'method/%' "
                "OR name LIKE 'task/%'"
            )
        }

    collapsed = []
    changed = True
    while changed:
        changed = False
        names = load_names()
        for parent in sorted(names, key=lambda n: -n.count("/")):
            if parent.count("/") < 1:  # never fold a bare facet root
                continue
            children = [
                n for n in names
                if n.startswith(parent + "/") and n.count("/") == parent.count("/") + 1
            ]
            if len(children) == 1:
                db.rename_tag(conn, children[0], parent)  # lone child's items move up
                collapsed.append((children[0], parent))
                names.discard(children[0])
                changed = True
    if progress:
        progress(f"removed {site_removed} site tags on papers, collapsed {len(collapsed)} single-child branches")
    return {"site_removed": site_removed, "collapsed": collapsed}


def recanonicalize_all(conn: sqlite3.Connection, progress=None) -> dict:
    """Re-derive every item's identity and merge rows that now collide.

    Canonicalization rules improve over time -- arXiv DOIs now fold onto the
    arXiv identity, for instance -- but existing rows keep whatever identity they
    were stored with. This replays the current rules over each row: when the new
    identity is free it simply moves there (the old URL kept as an alias, so no
    link is lost), and when another row already holds it the two are merged with
    the usual link-preference.
    """
    from .canonical import canonicalize, source_of

    rows = conn.execute("SELECT id FROM items ORDER BY id").fetchall()
    changed = merged = 0
    for ref in rows:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (ref["id"],)).fetchone()
        if row is None:
            continue  # already folded away by an earlier merge this pass
        new_url = canonicalize(row["raw_url"] or row["canonical_url"])
        if new_url == row["canonical_url"]:
            continue
        twin = conn.execute(
            "SELECT * FROM items WHERE canonical_url = ? AND id != ?", (new_url, row["id"])
        ).fetchone()
        if twin is not None:
            merge_group(conn, [twin, row])
            merged += 1
            if progress:
                progress(f"merged {row['canonical_url']} -> {new_url}")
        else:
            source, source_id = source_of(new_url)
            conn.execute(
                "UPDATE items SET canonical_url = ?, source = ?, source_id = ? WHERE id = ?",
                (new_url, source, source_id, row["id"]),
            )
            # Keep the old identity resolvable so a re-ingest lands here.
            conn.execute(
                "INSERT OR REPLACE INTO aliases (canonical_url, item_id, merged_at) "
                "VALUES (?, ?, ?)",
                (row["canonical_url"], row["id"], db.utcnow()),
            )
            changed += 1
    conn.commit()
    return {"changed": changed, "merged": merged}


def _completeness(row: sqlite3.Row) -> tuple:
    """Rank a row as a merge target: preferred link first, then richest, then oldest."""
    source_rank = _SOURCE_RANK.get(row["source"] or "web", 0)
    filled = sum(
        1 for f in ("abstract", "authors", "venue", "year", "summary") if row[f]
    )
    return (source_rank, filled, -(row["id"] or 0))


def merge_group(conn: sqlite3.Connection, group: list[sqlite3.Row]) -> dict:
    """Fold duplicates into one item, keeping the best of each field.

    The surviving item inherits the earliest first_seen in the group, because
    that date is the whole point of the ledger, and every merged URL is recorded
    as an alias so a later ingest resolves to the survivor instead of
    recreating the duplicate.
    """
    ordered = sorted(group, key=_completeness, reverse=True)
    primary, others = ordered[0], ordered[1:]
    if not others:
        return {"primary": primary["id"], "merged": []}

    fields: dict[str, object] = {}
    for column in ("abstract", "authors", "venue", "year", "summary", "body", "notes", "relevance"):
        if primary[column]:
            continue
        for other in others:
            if other[column]:
                fields[column] = other[column]
                break

    # BibTeX travels as a set: only adopt another copy's citation wholesale when the
    # survivor has none, so the published flag / source never mismatch the entry.
    if not primary["bibtex"]:
        for other in others:
            if other["bibtex"]:
                for col in ("bibtex", "bibtex_source", "bibtex_venue", "bibtex_published", "bibtex_at"):
                    fields[col] = other[col]
                break

    # Don't lose reading progress: if the survivor is still 'unread' but a merged
    # copy was marked reading/read, carry that across (archived is left untouched --
    # it's a deliberate choice, not progress).
    _progress = {"unread": 0, "reading": 1, "read": 2}
    if primary["status"] in _progress:
        best = max(group, key=lambda r: _progress.get(r["status"], -1))
        if _progress.get(best["status"], -1) > _progress[primary["status"]]:
            fields["status"] = best["status"]

    # User-authored notes are never silently dropped: if more than one copy has
    # notes, keep them all (survivor's first, de-duplicated).
    all_notes = [r["notes"].strip() for r in ordered if r["notes"] and r["notes"].strip()]
    if len(all_notes) > 1:
        fields["notes"] = "\n\n---\n\n".join(dict.fromkeys(all_notes))

    first_seen = min(
        (r["first_seen"] for r in group if r["first_seen"]), default=primary["first_seen"]
    )

    merged_ids = []
    now = db.utcnow()
    for other in others:
        # Preserve each tag's origin -- a manual tag stays manual on the survivor
        # rather than being downgraded to a model tag (which would make it prunable).
        for r in conn.execute(
            "SELECT t.name AS name, it.origin AS origin FROM item_tags it "
            "JOIN tags t ON t.id = it.tag_id WHERE it.item_id = ?",
            (other["id"],),
        ):
            db.add_tags(conn, primary["id"], [r["name"]], origin=r["origin"])
        # Repoint aliases that already pointed at `other` (from an earlier merge) onto
        # the survivor BEFORE deleting it -- otherwise ON DELETE CASCADE drops them and
        # a chained merge resurrects the duplicate the alias existed to prevent.
        conn.execute(
            "UPDATE aliases SET item_id = ? WHERE item_id = ?", (primary["id"], other["id"])
        )
        conn.execute(
            "INSERT OR REPLACE INTO aliases (canonical_url, item_id, merged_at) "
            "VALUES (?, ?, ?)",
            (other["canonical_url"], primary["id"], now),
        )
        conn.execute("DELETE FROM item_tags WHERE item_id = ?", (other["id"],))
        conn.execute("DELETE FROM search_index WHERE item_id = ?", (str(other["id"]),))
        conn.execute("DELETE FROM items WHERE id = ?", (other["id"],))
        merged_ids.append(other["id"])

    if fields:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        conn.execute(
            f"UPDATE items SET {assignments} WHERE id = ?",
            (*fields.values(), primary["id"]),
        )
    conn.execute(
        "UPDATE items SET first_seen = ? WHERE id = ?", (first_seen, primary["id"])
    )
    conn.commit()
    db.reindex_item(conn, primary["id"])

    return {"primary": primary["id"], "merged": merged_ids}


def merge_all(conn: sqlite3.Connection, threshold: float = 0.93) -> dict:
    groups = find_duplicates(conn, threshold)
    merged = 0
    for group in groups:
        result = merge_group(conn, group)
        merged += len(result["merged"])
    return {"groups": len(groups), "removed": merged}


def find_duplicates(conn: sqlite3.Connection, threshold: float = 0.93) -> list[list[sqlite3.Row]]:
    """Same paper under different URLs, where id matching could not see it.

    doc.md section 5 calls this out as canonicalization's known gap -- an arXiv
    preprint and its published DOI stay two records. Title similarity closes it
    without a model.
    """
    rows = [r for r in conn.execute("SELECT * FROM items ORDER BY id") if r["title"]]
    groups: list[list[sqlite3.Row]] = []
    used: set[int] = set()

    buckets: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        key = _title_key(row["title"])
        if len(key) < 15:
            continue
        buckets.setdefault(key[:18], []).append(row)

    for bucket in buckets.values():
        for i, row in enumerate(bucket):
            if row["id"] in used:
                continue
            group = [row]
            for other in bucket[i + 1 :]:
                if other["id"] in used:
                    continue
                if _same_work(row, other, threshold):
                    group.append(other)
                    used.add(other["id"])
            if len(group) > 1:
                used.add(row["id"])
                groups.append(group)

    return groups
