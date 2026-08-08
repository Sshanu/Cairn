# Cairn — Performance Investigation Report

_Author: Claude (assistant). Scope: why the web UI is slow to load items, tags,
collections, topics, Workspace, History and Settings. Independent codex review is
being run separately._

---

## 1. Executive summary

The Cairn web app becomes **intermittently slow (2–15+ seconds) on essentially every
read** — item lists, the tag tree, collections, History, and even Settings (which is a
trivial string read). During these stalls the **server process is near-idle on CPU**,
the **SQLite WAL is small**, and the **machine has free cores**. That combination means
the server is **blocked waiting on a shared resource**, not doing heavy work.

I found and fixed **two real bugs** (WAL bloat; a thread-pool starvation regression I
introduced today). They measurably helped but did **not** fully resolve the interactive
slowness. The residual slowness points at **three architectural issues**: an N+1 query
in the search path, **write-lock contention** from the poll process running codex
tagging while holding the DB, and a **shared/serialized connection model**. Section 6
gives the ranked refactor.

I want to be explicit: I have **not** fully fixed this, and earlier in the session I
wrongly blamed CPU/an unrelated project before finding the real causes.

---

## 2. Symptom & how to reproduce

- Click a topic/tag (e.g. `topic/language-resources`), open a collection, click
  **Workspace**, **History**, or **Settings**.
- Observed: 2–15+ seconds, **intermittent** (sometimes fast, sometimes very slow).
- Other apps on the same machine (VS Code, Chrome, Zotero, Notion) stay smooth — so it
  is **Cairn-specific**, not the OS/CPU.

---

## 3. Evidence gathered (measurements)

| Observation | Value | Interpretation |
|---|---|---|
| Machine | 14 cores | Not core-starved |
| Load average during stalls | ~64–79 | High, but driven by an unrelated project's codex procs; **see caveat** |
| **Cairn serve process CPU** | **~0.1–6%, state `S` (sleeping)** | Server is **blocked/waiting**, not computing |
| `/api/items?tag=…` (bloated WAL) | **12 s** | Profiler: stuck in SQLite `walFindFrame` + page reads |
| WAL file at that time | **5.3 MB** | Every read scanned it |
| After `wal_checkpoint(TRUNCATE)` | 5.3 MB → **45 KB** | |
| `/api/items` right after checkpoint | **16 ms** | WAL bloat was real |
| Later, WAL = **0 bytes**, single request | **18 s** | So WAL is **not** the whole story |
| Profiler (WAL empty) | threads in **`sock_call_ex`** (network) + `_PySemaphore_Wait` | Threads blocked on **network I/O** + waiting for a **thread-pool slot** |
| `/api/settings` (a one-line string read) | **slow too** | Proves it's **not** any single query — it's **thread-pool / connection contention** |
| 8 concurrent `/api/items` (before enrich fix) | **~7 s each** | Requests **serialized/queued** |
| 8 concurrent `/api/items` (after enrich fix) | **0.2 s total** | The enrich fix freed threads |
| Single `/api/items` (after enrich fix) | **~9 s (still)** | Something else remains |

**Caveat on load average:** an unrelated project (ParetoFront) was running ~16–38
`codex exec` processes at times. I initially over-attributed the slowness to that. The
user correctly pointed out other apps stayed fast and CPU was available — so the primary
cause is inside Cairn, not machine CPU.

---

## 4. Root causes CONFIRMED and FIXED

### 4.1 WAL bloat (fixed)
**Evidence:** `sample` of the serve during a 12s request showed the stack deep in
`libsqlite3` `getPageNormal → readDbPage → unixRead` and `walFindFrame`; the WAL was
5.3 MB. In WAL mode, each page read consults the WAL; a large WAL makes every read slow.

**Why it grew:** default `wal_autocheckpoint` is 1000 pages (~4 MB) — far too lax for an
app doing frequent small writes (poll: tag queue, title heal) plus many reads.

**Fix (committed):** `cairn/db.py` `connect()`:
```python
conn.execute("PRAGMA wal_autocheckpoint=200")   # ~800 KB cap
conn.execute("PRAGMA synchronous=NORMAL")
```
plus `PRAGMA wal_checkpoint(TRUNCATE)` each poll tick in `cairn/cli.py` `poll()`.

**Result:** 12 s → 16 ms immediately after. Real bug. **But the slowness returned with
WAL=0**, so it was necessary-not-sufficient.

### 4.2 Thread-pool starvation from background enrichment (fixed — my regression)
**Evidence:** With WAL empty and the server at ~0% CPU, a `sample` showed worker threads
in `sock_call_ex` (network sockets) and others in `_PySemaphore_Wait`. Meanwhile even
`/api/settings` (no DB/network/CPU work) was slow — the signature of **all worker
threads being busy**.

**Cause:** Earlier today I made `/api/capture` schedule metadata resolution as a FastAPI
**`BackgroundTasks`** job (`_enrich_captured` → `meta.resolve()`, a blocking network
call). FastAPI/Starlette runs **sync endpoints and background tasks in the same anyio
thread-pool**. A few stuck network enrich tasks hold the workers, so **every** sync
request queues behind them.

**Fix (committed):** `cairn/api.py` `capture_tab` no longer enriches inline; it enqueues
the item and returns. Enrichment/tagging happen in the **poll process**, off the web
server.

**Result:** 8 concurrent requests **7 s each → 0.2 s total**. Big improvement. **Single
requests still ~9 s intermittently**, so more remains (Section 5).

---

## 5. Root causes STILL OPEN (my hypotheses, with code)

> These are read from the code and consistent with the evidence, but I have **not**
> yet fixed or A/B-verified them. Codex's independent review should confirm/refute.

### 5.1 N+1 query in the search path — `cairn/db.py`
`search()` / `_rows()` attaches tags **per row**:
```python
record["tags"] = item_tags(conn, row["id"])   # one query PER result item
```
A 50-item page → ~**50 extra queries**. Each is individually cheap, but on **one shared
connection** they run strictly serially, and any single lock-wait stalls the whole page.

### 5.2 Write-lock contention from the poll — `cairn/cli.py` `poll()`
The poll tick runs `_drain_tag_queue()` → `topics.drain_tag_queue` → codex tagging
(**~15–19 s per batch**) and `_heal_titles()`, all writing to the DB. If a write
transaction is open across those long calls, or writes are frequent, **serve reads wait
up to `busy_timeout=5000ms`** each. This explains the **intermittency** (slow while the
poll churns). My capture change enqueues more items, increasing the queue the poll must
grind through.

### 5.3 Connection model — `cairn/api.py` `_conn()` + `cairn/db.py`
The server uses a **thread-local** SQLite connection. If, in practice, reads share a
connection or the pool of thread connections is small, concurrent requests **serialize**
on statement execution. Combined with 5.1 (50 statements/request), a burst of app
requests (Workspace click fires items + tags + stats + collections + build) can queue
badly.

### 5.4 Large tag tree — `cairn/db.py` `tag_tree` / `/api/tags`
There are ~842 topic tags and ~875 type tags. `tag_tree` builds counts for all of them.
I already optimized it once (per-node COUNT → single pass), but with the tree this large
it's worth re-checking its cost under load.

---

## 6. Recommended refactor (ranked by impact)

1. **Batch the tag fetch (kills the N+1).** Replace per-row `item_tags()` with **one**
   query for the whole page:
   ```sql
   SELECT it.item_id, t.name
   FROM item_tags it JOIN tags t ON t.id = it.tag_id
   WHERE it.item_id IN (:ids)
   ```
   then group in Python. ~50 queries/page → **2**.

2. **Never hold a DB transaction across a codex call.** In `topics.drain_tag_queue` /
   `tag_items`: resolve embeddings + run codex **outside** any transaction, then apply
   all DB writes in **one short** transaction. Keeps the write lock held for
   milliseconds, not the ~19 s a codex batch takes — so interactive reads stop blocking.

3. **Separate read vs write connections.** Give read endpoints a small pool of
   connections opened `PRAGMA query_only=1`; route all writes through a single writer.
   In WAL mode, `query_only` readers never wait on the writer.

4. **Throttle / relocate heavy tagging.** Run the tag/organize passes as a lower-priority
   detached job (they already can be), and cap how much the poll does per tick, so the
   background never contends with a foreground click.

5. **Confirm query plans + indexes.** `EXPLAIN QUERY PLAN` the tag-filtered item query
   and the `EXISTS(... t.name = ? OR t.name LIKE ?)` subquery; add a covering index on
   `item_tags(item_id, tag_id)` / `tags(name)` if a scan shows.

6. **Defense in depth.** Consider `async def` read endpoints (so a slow one can't hold a
   worker thread), or raising the anyio thread-pool limit, so a single slow op degrades
   gracefully instead of stalling everything.

---

## 7. Verification plan (before claiming fixed)

For each change, measure on an **isolated server** (`tt serve --port 879x`, unaffected by
launchd throttling), with the poll actively running, both **single** and **8-concurrent**:
```
/api/items?tag=topic/language-resources
/api/items (Workspace, no filter)
/api/history
/api/settings
```
Target: **<100 ms single, <300 ms for 8 concurrent, even while the poll is tagging.**
Only then is it fixed.

---

## 8. Honest status

- ✅ WAL bloat: real, fixed, verified (12 s → 16 ms).
- ✅ Thread-pool starvation: real (my regression), fixed, verified (7 s → 0.2 s concurrent).
- ❌ Interactive reads still ~9 s intermittently — **not fixed**; needs §6 items 1–3,
  verified per §7.
- ⚠️ I made incorrect CPU/ParetoFront calls before finding the real bugs. The user was
  right that it's a Cairn implementation issue.
