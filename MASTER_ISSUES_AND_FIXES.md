# Cairn — Master Issues & Fixes Log

A consolidated record of every issue found and every fix applied in this working
session, plus what's still open. Grouped by area. Status legend:

- ✅ **Fixed & deployed** (committed and pushed)
- 🔧 **Fixed, needs deploy** (committed; requires `git pull` + rebuild/restart to take effect)
- 📋 **Recommended / open** (identified, not yet implemented — mostly the perf refactor)

Companion reports: [`PERF_REPORT.md`](PERF_REPORT.md) (my analysis) and
[`CODEX_PERF_REPORT.md`](CODEX_PERF_REPORT.md) (independent codex review).

> **THE root cause (P14):** the residual slowness that survived every query fix was
> the **launchd plist marking the server `ProcessType=Background`** — macOS throttled
> its CPU (App Nap). Proof: the *same* request that took ~3ms on a directly-run server
> took **226–2950ms** on the launchd-managed one, same machine, same moment. Changing
> it to `Interactive` brought every read to **1–10ms**. The query refactor (P3–P12)
> was still worth doing, but P14 was the dominant cause of the multi-second stalls.

---

## 0. Quick status table

| # | Issue | Area | Status |
|---|---|---|---|
| P1 | WAL file bloated to 5.3 MB → every read scans it | Perf/DB | ✅ |
| P2 | Background metadata enrich ran network calls in the web thread-pool → all endpoints stall | Perf/API | ✅ |
| P3 | N+1: `item_tags()` per row (120-item page = 120 queries) | Perf/DB | ✅ |
| P4 | Missing/defeated indexes (`saved_at`, `item_tags(tag_id,…)`, `COALESCE`) | Perf/DB | ✅ |
| P5 | `/api/settings` runs 4 blocking `launchctl` subprocesses | Perf/API | ✅ |
| P6 | DB migrations run on every per-thread connection | Perf/DB | ✅ |
| P7 | Global 8s polling re-runs heavy `stats`/`tags`/`items` | Perf/UI | ✅ |
| P8 | Poll tagging holds DB; reads wait up to `busy_timeout=5s` | Perf/DB | 📋 |
| P9 | History returns all items for 30 days, N+1 serialized | Perf | ✅ |
| P10 | `/api/stats` runs multiple whole-table counts + tag counts (hit on every mount + 8s poll) | Perf/API | ✅ |
| P11 | `/api/items` always computes a full `total` count in addition to the page | Perf/API | ◐ mitigated (fast via P4; count still runs) |
| P12 | `tag_tree()` scans **all** item/tag pairs + keeps per-ancestor sets in Python | Perf/DB | ✅ |
| P13 | Backup `VACUUM INTO` + backfill/facet reindex lock the DB during UI use | Perf/DB | 📋 |
| **P14** | **launchd serve plist was `ProcessType=Background` → macOS throttled the server (App Nap). THE root cause of the residual slowness** | Perf/launchd | ✅ |
| S1 | Extension "Saving…" hangs forever (capture blocked on network) | Extension | ✅ |
| S2 | Saved item didn't appear without manual refresh | Extension/UI | ✅ |
| B1 | OpenReview metadata unfetchable (server Cloudflare-403) | BibTeX/Meta | ✅ |
| B2 | Captured OpenReview BibTeX wiped by "Find BibTeX" | BibTeX | ✅ |
| B3 | Collection BibTeX export was empty (stored-only) | BibTeX | ✅ |
| T1 | URL saved as the title (PDF/unloaded tabs) | Titles | ✅ |
| T2 | ACL nested-PDF URL didn't resolve | Titles | ✅ |
| T3 | Titles didn't self-heal | Titles | ✅ |
| C1 | codex-cli 0.143 rejects Cairn's schemas (no `additionalProperties`) | Codex | ✅ |
| C2 | agent health-check schema not strict → false failures | Codex | ✅ |
| I1 | `install.sh` didn't check Python **version** (macOS ships 3.9) | Install | ✅ |
| I2 | `install.sh` omitted the `embed` extra (embeddings silently off) | Install | ✅ |
| I3 | `install.sh` `python3 -V` aborts under `set -e` if absent | Install | ✅ |
| U1 | Pinned branches: no sub-branches, no rename | UI | ✅ |
| U2 | Appearance settings (density/tags) not applied live | UI | ✅ |
| X1 | New captures never auto-tagged (piled up untagged) | Tagging | ✅ |
| X2 | `tag_all` skipped un-embedded items | Tagging | ✅ |
| L1 | No observable logging for agent/capture/processes | Logging | ✅ |

---

## 1. Performance (the current focus)

### P1 — SQLite WAL bloat ✅
**Symptom:** every read 12 s; server idle CPU.
**Cause:** WAL grew to 5.3 MB; default `wal_autocheckpoint=1000` (~4 MB) is far too lax,
so every page read scanned a huge WAL (`walFindFrame` dominated the profile).
**Fix:** `cairn/db.py connect()` → `PRAGMA wal_autocheckpoint=200` + `synchronous=NORMAL`;
`cairn/cli.py poll()` runs `wal_checkpoint(TRUNCATE)` each tick. **12 s → 16 ms** at the time.
**Caveat:** necessary, not sufficient — slowness returned with WAL empty (see P2–P8).

### P2 — Thread-pool starvation from background enrich ✅
**Symptom:** all endpoints slow including `/api/settings`; server ~0% CPU.
**Cause:** I had `/api/capture` run `meta.resolve()` (blocking network) as a FastAPI
`BackgroundTasks` job. Those share the anyio thread-pool that serves **all** sync
endpoints, so a few stuck network tasks held the workers and every request queued.
**Fix:** `cairn/api.py capture_tab` no longer enriches inline — it enqueues and returns;
tagging happens in the poll process. **8 concurrent 7 s each → 0.2 s total.**
**Note:** this was a regression I introduced earlier in the session.

### P3 — N+1 tag fetch per row 📋 (highest-impact remaining)
`api.py _serialize()` → `db.item_tags(conn, row["id"])` runs **one query per result
item**. A 120-row page = 1 count + 1 search + **120 tag queries**, all on one connection.
**Fix:** fetch tags for all page item-ids in one `WHERE item_id IN (…)` query, group in
memory. Apply to `/api/items`, `/api/history`, exports.

### P4 — Missing / index-defeating filters 📋
- No index on `items(saved_at)` — yet it's the **default sort**.
- `item_tags` PK is `(item_id, tag_id)` — good for item→tags, **bad for tag→items**
  (tag/topic/collection clicks). No `item_tags(tag_id, item_id)` index.
- `COALESCE(i.bucket,'library')` / `COALESCE(i.source,'web')` in filters **prevent**
  plain `bucket`/`source` indexes from being used.
**Fix:** add `item_tags(tag_id, item_id)`, `items(saved_at DESC)`,
`items(bucket, saved_at DESC)`, `items(status, saved_at DESC)`; normalize `bucket`/`source`
to non-null (or expression indexes) so filters are sargable.

### P5 — `/api/settings` blocks on subprocesses 📋
`_settings_view()` calls `agents.status()` (`cairn/agents.py:177`) which runs **four
`launchctl list` subprocesses synchronously, no timeout**. This — not the DB — is why
"Loading backend settings…" hangs.
**Fix:** return settings immediately; move agent status to its own endpoint; run the four
checks concurrently with short timeouts; cache for a few seconds.

### P6 — Migrations at request-time connection setup 📋
`db.connect()` runs `executescript(SCHEMA)` + `migrate()` on **every** connection. With a
per-thread pool, new threads pay schema/DDL/migration cost mid-request.
**Fix:** run schema+migrations **once** at startup/CLI init; request connections only open
the DB and set read pragmas.

### P7 — Global 8s polling 📋 (partly self-inflicted)
`ui/src/main.tsx` sets `refetchInterval: 8_000` on the **default** query options, so every
query — `stats`, `tags`, `items` (all heavy) — re-runs every 8 s and on focus.
**Fix:** poll a cheap `/api/changes` (or version) endpoint; invalidate `stats`/`tags`/
visible items only when the DB actually changed. Cache `stats`/`tag_tree` behind
write-side invalidation.

### P8 — Write-lock contention from the poll 📋
Poll drains the tag queue (codex tagging ~15–19 s/batch) + heals titles, all writing.
`busy_timeout=5000` means each contended read can wait up to 5 s → intermittent stalls.
**Fix:** never hold a transaction across a codex call — compute first, then one short
write; batch commits; a single-writer queue for background jobs; avoid `TRUNCATE`
checkpoints during active UI use.

### P9 — History returns everything for 30 days, N+1 📋
`db.py` history fetches 30 distinct days then **all** items in them (no cap), serialized
per-item (N+1). Uses `substr(first_seen,1,10)` so the `first_seen` index isn't used.
**Fix:** return day summaries first, expand a day on demand; store/index `first_seen_date`.

### P10 — `/api/stats` is heavy and hit constantly 📋 _(codex)_
`/api/stats` (`api.py:119`) runs **multiple whole-table counts** plus tag counts. The app
shell mounts `stats` on every page (`App.tsx:83`), Workspace loads it again
(`Workspace.tsx:163`), and the 8s poll (P7) re-runs it — so this heavy query fires
constantly.
**Fix:** cache stats behind write-side invalidation; don't re-run it on a timer; compute
counts incrementally or from a summary table.

### P11 — `/api/items` computes a full `total` on every page 📋 _(codex)_
`/api/items` (`api.py:524`) always runs a **full `COUNT` for `total`** in addition to
fetching the page rows — doubling the query work on every list load / scroll.
**Fix:** skip the count when a cursor is present; return an approximate/`has_more` flag,
or compute `total` only on the first page.

### P12 — `tag_tree()` scans all item/tag pairs 📋 _(codex)_
`/api/tags` (`api.py:984`) calls both `tag_tree()` and `tag_counts()`. `tag_tree()`
(`db.py:793`, `db.py:834`) walks **every (item, tag) pair** and keeps per-ancestor item
**sets in Python** to compute subtree counts. With ~842 topic + ~875 type tags this is
expensive, and it runs on every mount + poll.
**Fix:** compute subtree counts in SQL (recursive CTE or a closure table); cache the tree
behind write-side invalidation; don't poll it.

### P13 — Maintenance jobs lock the DB during UI use 📋 _(codex)_
Backup uses `VACUUM INTO` (`backup.py:73`); backfill/facet jobs reindex many items
(`api.py:1235`, `ingest.py:405`). Running during active UI time collides with reads
(compounding P8's 5s waits).
**Fix:** schedule heavy maintenance off-peak; batch commits; a single-writer queue so
reads don't stack behind repeated lock waits.

---

## 2. Extension & Save

### S1 — "Saving…" hangs forever ✅
**Cause:** `/api/capture` did `save_url(fetch_meta=True)` → network `resolve()` **before
responding**; on a slow page/network the popup hung, and the extension had no timeout.
It "worked on one machine, not another" because it depended on how fast that fetch was.
**Fix:** capture saves instantly (`fetch_meta=False`, extension-supplied metadata);
extension bounds the metadata wait (4 s) and the capture fetch (15 s AbortController) and
shows a clear error instead of spinning.

### S2 — Item didn't appear without manual refresh 🔧
**Fix:** `ui/src/main.tsx` refetches on focus + polls while visible (see P7 caveat — this
polling needs to be narrowed). Needs UI rebuild to take effect.

---

## 3. BibTeX / OpenReview / Metadata

### B1 — OpenReview papers unreadable ✅
**Cause:** OpenReview's API/pages are behind Cloudflare; the **server** gets 403, so title/
abstract/bibtex couldn't be fetched.
**Fix:** the **browser extension** fetches the OpenReview API (`content._bibtex`, title,
abstract, authors) — the browser has already cleared Cloudflare — and hands them to
`/api/capture`. Also added `/api/resolve` so the popup shows a real title for PDFs the
page can't scrape (ACL/arXiv/DOI resolved server-side).

### B2 — Captured OpenReview BibTeX wiped by "Find BibTeX" ✅
**Cause:** re-resolve found nothing (server can't reach OpenReview) and **cleared** any
non-`manual` entry.
**Fix:** protect `source='openreview'` too (like `manual`) in `_resolve_and_store` and
`bibtex.backfill`; return the kept entry instead of "not found".

### B3 — Collection BibTeX export was empty ✅
**Cause:** export concatenated only **stored** BibTeX; BibTeX is resolved lazily, so an
un-opened collection exported nothing.
**Fix:** `/api/export/bibtex` resolves missing entries on export (capped at 100), caches
them. Verified 0/6 → 6/6.

---

## 4. Titles

### T1 — URL saved as the title ✅
**Cause:** a PDF/unloaded tab reports its URL as the tab title; `save_url` stored it and
could overwrite a real title on re-save.
**Fix:** `save_url` drops placeholder titles (URL/host/blank); `repair_titles` re-resolves
URL-titles for every source.

### T2 — ACL nested-PDF URL didn't resolve ✅
**Fix:** `_acl_id` falls back to the final path component
(`…/2025.findings-emnlp.98.pdf` → `2025.findings-emnlp.98`); strip BibTeX case-braces.

### T3 — Titles didn't self-heal ✅
**Fix:** `ingest.heal_titles` on the poll timer, retry-capped via `items.title_tries`.

---

## 5. Codex backend

### C1 — codex-cli 0.143 rejects Cairn's schemas ✅
**Cause:** codex-cli 0.143 enforces OpenAI structured-output rules — every object schema
must set `additionalProperties:false` and list all properties in `required`. Cairn didn't,
so **every codex call failed** with `invalid_json_schema`.
**Fix:** `_strict_schema()` in `cairn/backends/codex.py` adds those recursively before
writing the schema file.

### C2 — health-check schema not strict ✅
**Fix:** `base.py health_check` schema now sets `required` + `additionalProperties:false`
so `/api/agent/test` doesn't falsely fail on OpenAI-compatible backends. (Codex-review P2.)

### Related — computed facets after enrich ✅
`_enrich_captured` re-applies `taxonomy.computed_facets` after resolve fills venue/year,
so `venue/` tags aren't missing. (Codex-review P2.)

---

## 6. Install / Setup

- **I1 ✅** `install.sh` now probes `python3` / `python3.13…3.10` for a **≥3.10**
  interpreter (macOS ships 3.9), recreates a stale venv, and prints how to install one.
- **I2 ✅** installs `.[api,extract,web,embed]` — the `embed` extra (model2vec +
  scikit-learn) was omitted, so embeddings/topic-organizing were silently off on a fresh
  machine. Verified `embed.available()==True` on a fresh clone.
- **I3 ✅** guards `python3 -V` with `command -v` so the "no python" path prints guidance
  instead of aborting under `set -e`. (Codex-review P3.)
- **Also:** Node 18+ version check; health-check + auto-open after start; `CAIRN_NO_AGENTS=1`
  opt-out. **Verified end-to-end on a fresh clone** (all deps import, UI builds, server serves).

---

## 7. UI

- **U1 ✅** Pinned branches render as full tree `Branch`es (expand to sub-branches +
  rename inline); pins re-point on rename/move; drag a branch onto another to re-parent.
- **U2 ✅** Appearance settings (density, tags-in-list, age spine) apply **live** via a
  reactive `useSettings()` store (were read once at mount, so toggling "did nothing").
- Decluttered branch rows (removed pencil/describe; double-click renames).

---

## 8. Tagging / organizing

- **X1 ✅** Every save enqueues to a `tag_queue`; the poll drains it in batches — new
  captures auto-tag instead of piling up untagged.
- **X2 ✅** `tag_all` embeds first then tags every titled item (was pulling ids from
  `load_matrix`, which returns embedded items only — so anything saved since the last
  build was never tagged).

---

## 9. Logging & diagnostics

- **L1 ✅** `cairn/logs.py` configures a clear stderr logger; every **agent call**
  (latency/tokens/errors), **capture**, and **background enrich** logs to the terminal /
  serve log. `CAIRN_LOG=DEBUG` for more. `POST /api/agent/test` live-tests the backend.

---

## 10. Outstanding work (the perf refactor — do next, verify each)

In impact order (from both reports):

1. **Batch tag serialization** (P3) — kills the 120-query N+1.
2. **`/api/settings`: remove blocking `launchctl`** (P5) — fixes Settings hang.
3. **Add indexes** `item_tags(tag_id, item_id)`, `items(saved_at DESC)`; drop `COALESCE`
   on filtered columns (P4).
4. **Migrations once at startup**, not per connection (P6).
5. **Scope the 8s polling** to a cheap `/api/changes` check (P7).
6. **Keep write transactions short** (never across a codex call) + single-writer queue (P8, P13).
7. **History**: day summaries + on-demand expansion (P9).
8. **Cache `stats` + `tag_tree`** behind write-side invalidation; don't re-run on a timer;
   compute `tag_tree` subtree counts in SQL (P10, P12).
9. **`/api/items`: skip the full `total` count** when paging (P11).

**Verification bar (before claiming fixed):** on an isolated server with the poll
actively tagging, both single and 8-concurrent requests to `/api/items` (filtered +
unfiltered), `/api/history`, `/api/settings` should be **<100 ms single / <300 ms for 8
concurrent**.

---

## 11. Honest notes

- The WAL and thread-pool fixes were real and helped, but did **not** fully fix the
  interactive slowness. The N+1, the `launchctl` subprocesses in Settings, the missing
  indexes, and per-connection migrations remain.
- I initially and wrongly attributed the slowness to CPU / an unrelated project; the user
  was right that it is a Cairn implementation problem.
- The independent codex review (`CODEX_PERF_REPORT.md`) was more complete than my own
  first pass — it found the Settings-subprocess and per-connection-migration causes I
  missed.
