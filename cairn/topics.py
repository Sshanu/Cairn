"""Three-agent organization pipeline -- separates concept discovery, hierarchy
design, and item tagging, so structure is designed top-down (concise breadth,
deep depth) instead of exploding bottom-up.

  1. propose  (tag-proposal agent)  read titles in clustered batches of 50 and
              propose a flat POOL of candidate concepts across the library.
  2. hierarchy (hierarchy agent)     turn the pool into a clean HIERARCHY, one
              facet at a time (topic, method, task, + any the user defines) --
              each facet built independently. This is the vocabulary.
  3. tag       (tagging agent)        tag every item into that fixed vocabulary,
              per-item, multi-label, in batches of 10 (may grow the vocab for a
              genuine gap). One-time for the whole library, then incremental for
              new items on a schedule.

All three prompts are editable in Settings. Clustering is only ever used to make
batches coherent; it never assigns a tag. type/venue/site stay computed; manual
tags are untouched.
"""

from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

from . import config, db, embed
from .backends.base import BaseBackend
from .ingest import strip_site_suffix

# Codex exec spawns an isolated subprocess per call (~15-20s each), so the LLM
# steps are I/O-bound and independent -- fanning them out concurrently turns a
# ~24-call propose from ~8 minutes into ~1-2. Kept modest so a codex/OpenAI plan
# is not rate-limited into failing every other batch.
_MAX_WORKERS = 5


def _fan_out(prompts, call, *, progress=None, label="batch", retries=2):
    """Run `call(prompt)` over prompts concurrently, yielding (result, error, n) as
    each finishes. DB work must happen on the caller's thread, before/after -- only
    the LLM call is parallel (sqlite objects belong to their creating thread).

    Each call is retried on failure with a short backoff, so a transient network
    blip or a rate-limit is ridden out rather than silently dropping a batch."""
    def _attempt(p):
        last = None
        for i in range(retries + 1):
            try:
                return call(p)
            except Exception as exc:  # noqa: BLE001 -- transient; retry then give up
                last = exc
                if i < retries:
                    time.sleep(2.0 * (i + 1))  # 2s, 4s
        raise last

    done = 0
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
        futures = {ex.submit(_attempt, p): p for p in prompts}
        for fut in as_completed(futures):
            done += 1
            try:
                yield fut.result(), None, done
            except Exception as exc:  # exhausted retries -- skip this batch, not fatal
                if progress:
                    progress(f"{label} {done}/{len(prompts)} failed after retries: {exc}")
                yield None, exc, done


PROPOSE_BATCH = 50
TAG_BATCH = 10
DEFAULT_FACETS = config.DEFAULT_FACETS  # just 'topic' by default; edited in Settings
_MODEL_FACET_PREFIXES = ("topic/", "method/", "task/", "contribution/", "project/")


# --------------------------------------------------------------------------- #
#  embeddings + clustering (batching scaffolding only)
# --------------------------------------------------------------------------- #

def _item_text(title: str | None, abstract: str | None) -> str:
    clean = strip_site_suffix(title) or (title or "")
    return f"{clean}. {(abstract or '')[:400]}".strip()


def ensure_embeddings(conn: sqlite3.Connection, *, batch: int = 256, progress=None) -> int:
    rows = conn.execute(
        "SELECT id, title, abstract FROM items WHERE embedding IS NULL AND title IS NOT NULL"
    ).fetchall()
    if not rows:
        return 0
    done = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        vecs = embed.embed([_item_text(r["title"], r["abstract"]) for r in chunk])
        for r, v in zip(chunk, vecs):
            conn.execute("UPDATE items SET embedding = ? WHERE id = ?", (embed.to_blob(v), r["id"]))
        conn.commit()
        done += len(chunk)
        if progress:
            progress(f"embedded {done}/{len(rows)}")
    return done


def load_matrix(conn: sqlite3.Connection) -> tuple[list[int], np.ndarray]:
    rows = conn.execute("SELECT id, embedding FROM items WHERE embedding IS NOT NULL").fetchall()
    if not rows:
        return [], np.zeros((0, embed.DIM), dtype=np.float32)
    return [r["id"] for r in rows], np.vstack([embed.from_blob(r["embedding"]) for r in rows])


def _coherent_batches(ids: list[int], matrix: np.ndarray, size: int) -> list[list[int]]:
    """Group ids into batches of ~`size`, each drawn from one cluster so the
    batch is topically coherent. Fewer than `size` items -> a single batch, no
    clustering (handles the incremental 'less than 10 items' case)."""
    n = len(ids)
    if n <= size:
        return [ids] if ids else []
    from sklearn.cluster import KMeans

    k = max(1, round(n / size))
    labels = KMeans(n_clusters=k, n_init=4, random_state=0).fit(matrix).labels_
    order = sorted(range(n), key=lambda i: int(labels[i]))
    out: list[list[int]] = []
    cur: list[int] = []
    cur_lab = None
    for i in order:
        lab = int(labels[i])
        if cur and (lab != cur_lab or len(cur) >= size):
            out.append(cur)
            cur = []
        cur.append(ids[i])
        cur_lab = lab
    if cur:
        out.append(cur)
    return out


# --------------------------------------------------------------------------- #
#  agent 1 -- propose a pool of candidate concepts
# --------------------------------------------------------------------------- #

PROPOSE_SYSTEM = (
    "You are helping a researcher organize their library so they can browse it by research "
    "area and rediscover work later. Read each item (title + abstract) -- papers, but also "
    "blogs, code, datasets, tools -- and name the specific research areas and problems it is "
    "ABOUT: the subject, not the tools it happens to use.\n\n"
    "So a paper that uses a VLM to study medical QA is about medical-question-answering, not "
    "'vlm'; a survey of accessibility tech built on LLMs is about accessibility, not 'llm'.\n\n"
    "Return a flat list of concise concept names (lowercase, hyphenated, no slashes). Favour "
    "specific, real research topics over vague umbrellas. Use your judgment on how many."
)

PROPOSE_SCHEMA = {
    "type": "object",
    "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
    "required": ["tags"],
    "additionalProperties": False,
}


def propose_tags(conn: sqlite3.Connection, backend: BaseBackend, *, progress=None) -> list[str]:
    ids, matrix = load_matrix(conn)
    batches = _coherent_batches(ids, matrix, PROPOSE_BATCH)
    system = config.tag_proposal_prompt() or PROPOSE_SYSTEM
    # Build every prompt first (DB reads stay on this thread), then fan the
    # independent LLM calls out -- sequential codex calls made this the slow step.
    prompts = []
    for batch_ids in batches:
        rows = [r for r in (db.get_item(conn, i) for i in batch_ids) if r is not None]
        if not rows:
            continue
        titles = "\n".join(
            f"- {(r['title'] or r['canonical_url'])[:120]}"
            + (f"  ::  {(r['abstract'] or '')[:200]}" if r["abstract"] else "")
            for r in rows
        )
        prompts.append(f"Propose the concepts covered by these {len(rows)} papers:\n\n{titles}")

    def _call(p):
        return backend.invoke_json(p, schema=PROPOSE_SCHEMA, system=system, max_tokens=1024)

    pool: dict[str, int] = {}
    for out, err, done in _fan_out(prompts, _call, progress=progress, label="propose batch"):
        if err or not out:
            continue
        for raw in out.get("tags") or []:
            name = db.normalize_tag(str(raw)).strip("/")
            if name:
                pool[name] = pool.get(name, 0) + 1
        if progress:
            progress(f"proposed tags {done}/{len(prompts)} ({len(pool)} in pool)")
    # Ordered by prevalence (how many batches named each) -- a signal to Agent 2 of
    # what the library is actually full of, so common topics become real branches.
    return [name for name, _ in sorted(pool.items(), key=lambda kv: (-kv[1], kv[0]))]


# --------------------------------------------------------------------------- #
#  agent 2 -- build a hierarchy per facet from the pool
# --------------------------------------------------------------------------- #

HIERARCHY_SYSTEM = (
    "You are DESIGNING the research-topic taxonomy for a researcher's library -- the browsable "
    "tree they navigate to find work by area. Plan it and reason about it as a domain expert "
    "would; the result is a single coherent tree.\n\n"
    "You are given the concepts found across the library, ordered by how common each is. Treat "
    "this as EVIDENCE of what the library holds and what matters -- a PRIOR, not a checklist. You "
    "need NOT place every concept, and you should use your own knowledge of the field to name and "
    "structure things well.\n\n"
    "Design principles:\n"
    "- Plan ~10-15 real, specific top-level areas (e.g. vision-language-models, interpretability, "
    "reasoning, retrieval-augmentation, agents, generative-models) -- real areas, not vague "
    "umbrellas (multimodal-grounding, learning-adaptation).\n"
    "- Subdivide each area DEEPLY into its actual research directions. A topic the library has a "
    "lot of deserves its own branch, e.g. topic/vision-language-models/{multi-image, grounding, "
    "alignment, uncertainty-estimation, personalization, long-context}; a cross-cutting theme may "
    "be its own area too (interpretability, personalization).\n"
    "- MERGE variants and near-duplicates into ONE branch yourself: 'long-context' and "
    "'long-context-vision-language' are the SAME branch -- never emit both.\n"
    "- Every leaf is a specific research topic; no filler nodes (general, other, misc, evaluation, "
    "methods). Go 3-4 levels deep where warranted. Every path starts with the facet name.\n\n"
    "Return the complete tree as a flat list of slash-paths."
)

HIERARCHY_SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "description": {"type": "string"}},
                "required": ["name", "description"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tags"],
    "additionalProperties": False,
}


def build_hierarchy(
    conn: sqlite3.Connection, backend: BaseBackend, pool: list[str], facet: str, *, progress=None
) -> list[tuple[str, str]]:
    """ONE reasoning call (was two: pick-areas + chunked-nest). The agent designs the whole
    tree from the concept pool, treating it as a PRIOR not a checklist. A single coherent
    output is what lets it merge variants and place recurring topics as real branches --
    the old chunking gave each parallel call a blind view, which is what produced the
    duplicate branches (long-context AND long-context-vision-language)."""
    system = config.hierarchy_prompt() or HIERARCHY_SYSTEM
    if progress:
        progress(f"{facet}: designing the hierarchy from {len(pool)} concepts")
    prompt = (
        f"Facet: {facet}\n\n"
        "Concepts found across the library, most common first -- evidence of what's present and "
        "what matters (a PRIOR, not a checklist; you need not place every one):\n"
        + "\n".join(f"- {t}" for t in pool)
        + f"\n\nDesign the {facet} taxonomy: plan the areas, then a clean, deep tree of specific "
        f"research directions, merging variants into one branch. Return every path starting "
        f"with '{facet}/'."
    )

    def _call(p):
        return backend.invoke_json(p, schema=HIERARCHY_SCHEMA, system=system, max_tokens=8000)

    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    # A single call, but routed through _fan_out so it is retried on a transient failure.
    for out, err, _n in _fan_out([prompt], _call, progress=progress, label=f"{facet} hierarchy"):
        if err or not out:
            continue
        for entry in out.get("tags") or []:
            name = db.normalize_tag(str(entry.get("name") or ""))
            parts = name.split("/")
            if len(parts) >= 2 and parts[0] == facet and name not in seen:
                seen.add(name)
                result.append((name, str(entry.get("description") or "")))
    if progress:
        tops = len({p[0].split("/")[1] for p in result if p[0].count("/") >= 1})
        progress(f"{facet}: {tops} areas, {len(result)} paths")
    return result


def build_vocabulary(
    conn: sqlite3.Connection, backend: BaseBackend, pool: list[str], facets, *, progress=None
) -> int:
    from .organize import install_taxonomy

    total = 0
    for facet in facets:
        paths = build_hierarchy(conn, backend, pool, facet, progress=progress)
        total += install_taxonomy(conn, paths)
    return total


# --------------------------------------------------------------------------- #
#  agent 3 -- tag each item into the vocabulary (per-item, batches of 10)
# --------------------------------------------------------------------------- #

def tag_items(
    conn: sqlite3.Connection, backend: BaseBackend, ids: list[int], *, progress=None
) -> int:
    """Tag each item per-item into the current vocabulary, in coherent batches of
    TAG_BATCH. Reuses the classify machinery; may grow the vocab for a real gap."""
    from . import organize

    all_ids, matrix = load_matrix(conn)
    pos = {iid: i for i, iid in enumerate(all_ids)}
    have = [i for i in ids if i in pos]
    missing = [i for i in ids if i not in pos]
    submatrix = (
        matrix[[pos[i] for i in have]] if have else np.zeros((0, embed.DIM), dtype=np.float32)
    )
    batches = _coherent_batches(have, submatrix, TAG_BATCH)
    if missing:
        batches.append(missing)  # no embedding yet -> one plain batch
    base = config.tagging_prompt() or organize.CLASSIFY_SYSTEM
    system = organize._augment_system(conn, base)
    # Snapshot the target vocabulary once. Step 1 already built the hierarchy, so
    # every item tags against the same tree -- which lets the classify calls (the slow
    # part, ~19s each over dozens of batches) run concurrently. All DB reads and
    # writes stay on this thread; only the LLM call is parallel.
    vocab = list(db.vocabulary(conn))
    tree = organize._vocabulary_tree(conn)
    prepared = [
        rows
        for rows in ([r for r in (db.get_item(conn, i) for i in b) if r is not None] for b in batches)
        if rows
    ]
    total = sum(len(rows) for rows in prepared)
    tagged = 0
    done = 0

    def _call(rows):
        return rows, organize._classify_batch(backend, rows, vocab, tree, system)

    for (result, err, _n) in _fan_out(prepared, _call, progress=progress, label="tag batch"):
        if err or not result:
            continue
        rows, resp = result
        in_batch = {r["id"] for r in rows}
        for entry in resp.get("items") or []:
            try:
                iid = int(entry.get("id"))
            except (TypeError, ValueError):
                continue
            if iid not in in_batch:
                continue
            tags = []
            for raw in list(entry.get("tags") or []) + list(entry.get("new_tags") or []):
                name = db.normalize_tag(str(raw))
                # Agent 3 tags ONLY the model facets (topic/method/task/contribution).
                # type/, venue/ and site/ are computed from the URL and project/ is the
                # user's own filing -- dropping everything else stops a github link in an
                # abstract from becoming a site/github tag on an arXiv paper.
                if name and name.startswith(_MODEL_FACET_PREFIXES[:4]):
                    tags.append(name)
            # Clear ONLY the model's own facet tags -- a hand-added topic/method/task/
            # contribution is the user's curation and must survive re-tagging. Filtering
            # on it.origin='model' is what keeps "Tag everything" from silently wiping it.
            model_facets = [
                r["name"]
                for r in conn.execute(
                    "SELECT t.name FROM item_tags it JOIN tags t ON t.id = it.tag_id "
                    "WHERE it.item_id = ? AND it.origin = 'model' AND ("
                    "t.name LIKE 'topic/%' OR t.name LIKE 'method/%' "
                    "OR t.name LIKE 'task/%' OR t.name LIKE 'contribution/%')",
                    (iid,),
                )
            ]
            for old in model_facets:
                db.remove_tag_from_item(conn, iid, old)
            if tags:
                db.add_tags(conn, iid, tags, origin="model")
                tagged += 1
            db.mark_enriched(conn, iid, str(entry.get("summary") or "").strip())
        conn.commit()
        done += len(rows)
        if progress:
            progress(f"tagging items: {done}/{total}")
    return tagged


def _clear_model_facets(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM item_tags WHERE origin != 'manual' AND tag_id IN "
        "(SELECT id FROM tags WHERE name LIKE 'topic/%' OR name LIKE 'method/%' "
        " OR name LIKE 'task/%' OR name LIKE 'contribution/%')"
    )
    conn.execute(
        "DELETE FROM tags WHERE (name LIKE 'topic/%' OR name LIKE 'method/%' "
        "OR name LIKE 'task/%' OR name LIKE 'contribution/%') "
        "AND origin != 'manual' AND id NOT IN (SELECT DISTINCT tag_id FROM item_tags)"
    )
    conn.commit()


def build_vocab(conn: sqlite3.Connection, backend: BaseBackend, *, progress=None) -> dict:
    """Step 1 of 2: propose a concept pool and build the hierarchy per facet, then
    swap it in. STOPS there -- the vocabulary is installed (visible in the sidebar as
    empty branches) so you can review it before tagging.

    Robust by construction: the NEW hierarchy is computed in full BEFORE the old one
    is touched, so a network drop, a hung agent, or a killed job never leaves you
    with erased-but-not-rebuilt topics -- the old tree simply stays until a complete
    new one is ready to replace it. Each agent call is retried on a transient error.
    """
    from .organize import install_taxonomy

    if not embed.available():
        raise RuntimeError("model2vec not installed; run: pip install '.[embed]'")
    # type/venue/site are computed from the URL -- never build a hierarchy for them.
    facets = tuple(f for f in (config.facets() or DEFAULT_FACETS) if f not in ("type", "venue", "site"))
    facets = facets or DEFAULT_FACETS
    ensure_embeddings(conn, progress=progress)
    ids, _ = load_matrix(conn)
    if len(ids) < 2:
        return {"pool": 0, "vocab": 0, "items": len(ids), "facets": list(facets)}

    # 1. Propose the concept pool (retried, parallel). Do NOT erase anything yet.
    if progress:
        progress(f"proposing concepts from {len(ids)} papers")
    pool = propose_tags(conn, backend, progress=progress)
    if not pool:
        raise RuntimeError(
            "No concepts came back — check the agent and your connection. "
            "Your current topics are untouched; nothing was erased."
        )

    # 2. Build every facet's hierarchy (paths only -- still no writes).
    if progress:
        progress(f"building hierarchy for facets: {', '.join(facets)}")
    facet_paths = [(facet, build_hierarchy(conn, backend, pool, facet, progress=progress)) for facet in facets]
    if not any(paths for _, paths in facet_paths):
        raise RuntimeError(
            "The hierarchy came back empty — check the agent and your connection. "
            "Your current topics are untouched; nothing was erased."
        )

    # 3. Only now swap: erase the old model facets and install the new tree. This is
    #    the only destructive step, and it runs only once a full hierarchy exists.
    if progress:
        progress("installing the new hierarchy (replacing the old topics)")
    _clear_model_facets(conn)
    vocab = sum(install_taxonomy(conn, paths) for _, paths in facet_paths)
    if progress:
        progress(f"hierarchy ready: {vocab} tags across {', '.join(facets)} — review it, then Tag everything")
    return {"pool": len(pool), "vocab": vocab, "items": len(ids), "facets": list(facets)}


def _prune_thin(conn: sqlite3.Connection, min_items: int = 3) -> int:
    """Enforce 'every tag has >= min_items papers': drop empty model tags (incl.
    unused vocabulary from the build step), and collapse a thin deep leaf into its
    parent so its papers aren't lost. Top-level areas are left as landmarks."""
    removed = 0
    for _ in range(8):  # a few rounds -- collapsing can make a new leaf thin
        rows = conn.execute(
            "SELECT id, name, origin FROM tags WHERE name LIKE 'topic/%' OR name LIKE 'method/%' "
            "OR name LIKE 'task/%'"
        ).fetchall()
        names = {r["name"] for r in rows}  # all names, so is_leaf sees manual children too
        changed = False
        for r in rows:
            name, tid = r["name"], r["id"]
            if r["origin"] == "manual":
                continue  # an empty branch you made yourself is a plan, not a leftover
            is_leaf = not any(o != name and o.startswith(name + "/") for o in names)
            if not is_leaf:
                continue
            cnt = conn.execute("SELECT COUNT(*) FROM item_tags WHERE tag_id = ?", (tid,)).fetchone()[0]
            if cnt == 0:
                conn.execute("DELETE FROM tags WHERE id = ?", (tid,))
                removed += 1
                changed = True
            elif cnt < min_items and name.count("/") >= 2:  # a deep leaf -> collapse to parent
                parent = "/".join(name.split("/")[:-1])
                for (it,) in conn.execute("SELECT item_id FROM item_tags WHERE tag_id = ?", (tid,)):
                    db.add_tags(conn, it, [parent], origin="model")
                    db.remove_tag_from_item(conn, it, name)
                conn.execute("DELETE FROM tags WHERE id = ?", (tid,))
                removed += 1
                changed = True
        conn.commit()
        if not changed:
            break
    return removed


def tag_all(conn: sqlite3.Connection, backend: BaseBackend, *, progress=None) -> dict:
    """Step 2 of 2: tag every item into the current (reviewed) vocabulary, then
    prune so every surviving tag holds at least 3 papers."""
    # Embed anything new first -- local and fast, so coherent batching and "Related"
    # keep working. But tagging no longer DEPENDS on embeddings: we tag every item
    # with a title, and tag_items puts any un-embedded ones in a plain batch. The old
    # code pulled its id list from load_matrix (embedded items ONLY), so every item
    # saved since the last build -- which has no embedding yet -- was silently skipped.
    ensure_embeddings(conn, progress=progress)
    ids = [
        r["id"]
        for r in conn.execute("SELECT id FROM items WHERE title IS NOT NULL AND title != ''")
    ]
    tagged = tag_items(conn, backend, ids, progress=progress)
    if progress:
        progress("pruning tags with fewer than 3 papers")
    pruned = _prune_thin(conn)
    for iid in ids:
        db.reindex_item(conn, iid)
    return {"tagged": tagged, "items": len(ids), "pruned": pruned}


def organize_topics(conn: sqlite3.Connection, backend: BaseBackend, *, progress=None) -> dict:
    """Both steps at once (build the hierarchy, then tag) -- used by the CLI."""
    v = build_vocab(conn, backend, progress=progress)
    if v.get("items", 0) < 2:
        return {**v, "tagged": 0}
    return {**v, **tag_all(conn, backend, progress=progress)}


def tag_new_items(conn: sqlite3.Connection, backend: BaseBackend, ids: list[int], *, progress=None) -> int:
    """Incremental: embed + tag newly-ingested items into the existing vocabulary
    (batches of 10, no clustering under 10). Grows the vocab for genuine gaps."""
    if not ids or not embed.available():
        return 0
    ensure_embeddings(conn, progress=progress)
    tagged = tag_items(conn, backend, ids, progress=progress)
    for iid in ids:
        db.reindex_item(conn, iid)
    return tagged


def drain_tag_queue(conn: sqlite3.Connection, backend: BaseBackend, *, progress=None) -> int:
    """Tag everything sitting in the queue in one batched pass, then clear it.

    This is what the save/interval design drains: a capture enqueues (it never tags
    inline -- that would put a ~19s agent call in front of every save), and a timer
    calls this to tag the whole accumulated batch at once. An item leaves the queue
    once attempted -- even if it got no topic -- so a login page or profile with
    nothing to classify is not retried on every tick."""
    ids = db.tag_queue_ids(conn)
    if not ids:
        return 0
    if progress:
        progress(f"tagging {len(ids)} newly-saved item(s)")
    tagged = tag_new_items(conn, backend, ids, progress=progress)
    db.dequeue_from_tagging(conn, ids)
    return tagged
