**Report**

I did not implement changes. I reviewed the FastAPI, SQLite, and React paths for the reported slow screens.

**Root Causes**

1. The read path repeatedly does whole-library work on navigation and polling.

- The React Query default polls every query every 8s and refetches on focus: [main.tsx](/Users/shanu.kumar/Documents/Projects/tabtrack/ui/src/main.tsx:39).
- The app shell always mounts `stats`: [App.tsx](/Users/shanu.kumar/Documents/Projects/tabtrack/ui/src/App.tsx:83), collections/tag tree via `tags`: [App.tsx](/Users/shanu.kumar/Documents/Projects/tabtrack/ui/src/App.tsx:173), [App.tsx](/Users/shanu.kumar/Documents/Projects/tabtrack/ui/src/App.tsx:269).
- Workspace additionally loads `stats` and `items`: [Workspace.tsx](/Users/shanu.kumar/Documents/Projects/tabtrack/ui/src/pages/Workspace.tsx:163), [Workspace.tsx](/Users/shanu.kumar/Documents/Projects/tabtrack/ui/src/pages/Workspace.tsx:166).
- `/api/stats` runs multiple whole-table counts plus tag counts: [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:119).
- `/api/tags` calls both `tag_tree()` and `tag_counts()`: [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:984). `tag_tree()` itself scans all item/tag pairs and keeps per-ancestor item sets in Python: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:793), [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:834).

2. Item list reads are N+1 and do duplicate query work.

- `/api/items` always computes a full `total` and then fetches the page: [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:524).
- Each row is serialized by `_serialize()`, which calls `db.item_tags()` once per item: [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:88), [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:97).
- `db.item_tags()` is a separate query per item: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:915). A 120-row Workspace page means 1 count + 1 search + 120 tag queries.

3. Several hot filters defeat or lack indexes.

- Schema indexes only `items.status`, `items.first_seen`, `ledger.last_seen`, and later `items.bucket`: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:101), [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:234).
- Default list ordering uses `saved_at`, but there is no `saved_at` index: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:1163), [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:1228).
- Bucket/source filters wrap columns in `COALESCE(...)`, which prevents normal `bucket`/`source` indexes from helping: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:1191), [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:1194).
- Tag/topic/collection clicks use correlated `EXISTS` subtree filters: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:1166). The join table primary key is `(item_id, tag_id)`, good for item-to-tags, not tag-to-items: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:72). There is no `item_tags(tag_id, item_id)` index.

4. History can return very large pages and also N+1 serializes.

- History fetches 30 distinct days, then returns every item in those days with no item cap: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:1099), [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:1116).
- It uses `substr(first_seen, 1, 10)`, so the `first_seen` index is not directly useful: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:1095), [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:1102).
- The API serializes every returned history item through `_serialize()`, again causing per-item tag queries: [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:641), [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:657).

5. Settings includes blocking process checks.

- `/api/settings` returns `_settings_view()`: [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:420).
- `_settings_view()` includes `agents.status()`: [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:235).
- `agents.status()` runs four `launchctl list` subprocesses synchronously, without timeout: [agents.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/agents.py:177). This can plausibly explain Settings taking seconds while the Python process is near-idle.

6. Intermittency is likely amplified by SQLite waits/background maintenance.

- Every DB connection sets `busy_timeout=5000`, so lock waits can cost up to 5s each: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:157).
- Request connections are per-thread; a new thread connection runs WAL setup, schema DDL, and migrations: [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:38), [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:151).
- The poll command can drain the tag queue, heal titles, then run `wal_checkpoint(TRUNCATE)`: [cli.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/cli.py:232), [cli.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/cli.py:240).
- Backup uses `VACUUM INTO`: [backup.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/backup.py:73). Backfill/facet jobs can reindex many items: [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:1235), [ingest.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/ingest.py:405).

**Recommendations Ranked By Impact**

1. Batch item serialization. Fetch tags for all returned item IDs in one query and attach them in memory. Apply this to `/api/items`, `/api/history`, export paths, and any list/detail sidebar payloads. This removes the biggest N+1 pattern.

2. Add read indexes and rewrite filters to use them. At minimum: `items(saved_at DESC)`, `items(bucket, saved_at DESC)`, `items(status, saved_at DESC)`, `item_tags(tag_id, item_id)`, and either normalized non-null `bucket/source` values or expression indexes matching current `COALESCE(...)` filters.

3. Replace correlated tag subtree `EXISTS` filters with an indexed tag-first query. Resolve matching tag IDs once, join through `item_tags(tag_id, item_id)`, then join `items`. For hierarchy browsing at scale, consider a materialized tag closure table.

4. Stop polling heavy reads globally. Poll a cheap `/api/version` or `/api/changes` endpoint, then invalidate `stats`, `tags`, and visible item queries only when the database changed. Cache `stats`, `tag_tree`, and `digest` behind write-side invalidation.

5. Split history into day summaries and item expansion. First return date/count rows only; fetch items for a day only when expanded. Store/index `first_seen_date` instead of using `substr(first_seen, 1, 10)`.

6. Make Settings non-blocking. Return settings immediately, move `agents.status()` to a separate endpoint, run the four checks concurrently with short timeouts, and cache the result for a few seconds.

7. Move migrations out of request-time connection setup. Run schema/migrations once at startup or CLI init. Request connections should only open the DB and set read pragmas.

8. Reduce lock collision from maintenance jobs. Batch commits/reindexing, avoid `wal_checkpoint(TRUNCATE)` during active UI periods, and consider a single writer queue for background jobs so read endpoints do not stack behind repeated 5s waits.
tokens used
132,182
**Report**

I did not implement changes. I reviewed the FastAPI, SQLite, and React paths for the reported slow screens.

**Root Causes**

1. The read path repeatedly does whole-library work on navigation and polling.

- The React Query default polls every query every 8s and refetches on focus: [main.tsx](/Users/shanu.kumar/Documents/Projects/tabtrack/ui/src/main.tsx:39).
- The app shell always mounts `stats`: [App.tsx](/Users/shanu.kumar/Documents/Projects/tabtrack/ui/src/App.tsx:83), collections/tag tree via `tags`: [App.tsx](/Users/shanu.kumar/Documents/Projects/tabtrack/ui/src/App.tsx:173), [App.tsx](/Users/shanu.kumar/Documents/Projects/tabtrack/ui/src/App.tsx:269).
- Workspace additionally loads `stats` and `items`: [Workspace.tsx](/Users/shanu.kumar/Documents/Projects/tabtrack/ui/src/pages/Workspace.tsx:163), [Workspace.tsx](/Users/shanu.kumar/Documents/Projects/tabtrack/ui/src/pages/Workspace.tsx:166).
- `/api/stats` runs multiple whole-table counts plus tag counts: [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:119).
- `/api/tags` calls both `tag_tree()` and `tag_counts()`: [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:984). `tag_tree()` itself scans all item/tag pairs and keeps per-ancestor item sets in Python: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:793), [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:834).

2. Item list reads are N+1 and do duplicate query work.

- `/api/items` always computes a full `total` and then fetches the page: [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:524).
- Each row is serialized by `_serialize()`, which calls `db.item_tags()` once per item: [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:88), [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:97).
- `db.item_tags()` is a separate query per item: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:915). A 120-row Workspace page means 1 count + 1 search + 120 tag queries.

3. Several hot filters defeat or lack indexes.

- Schema indexes only `items.status`, `items.first_seen`, `ledger.last_seen`, and later `items.bucket`: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:101), [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:234).
- Default list ordering uses `saved_at`, but there is no `saved_at` index: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:1163), [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:1228).
- Bucket/source filters wrap columns in `COALESCE(...)`, which prevents normal `bucket`/`source` indexes from helping: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:1191), [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:1194).
- Tag/topic/collection clicks use correlated `EXISTS` subtree filters: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:1166). The join table primary key is `(item_id, tag_id)`, good for item-to-tags, not tag-to-items: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:72). There is no `item_tags(tag_id, item_id)` index.

4. History can return very large pages and also N+1 serializes.

- History fetches 30 distinct days, then returns every item in those days with no item cap: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:1099), [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:1116).
- It uses `substr(first_seen, 1, 10)`, so the `first_seen` index is not directly useful: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:1095), [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:1102).
- The API serializes every returned history item through `_serialize()`, again causing per-item tag queries: [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:641), [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:657).

5. Settings includes blocking process checks.

- `/api/settings` returns `_settings_view()`: [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:420).
- `_settings_view()` includes `agents.status()`: [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:235).
- `agents.status()` runs four `launchctl list` subprocesses synchronously, without timeout: [agents.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/agents.py:177). This can plausibly explain Settings taking seconds while the Python process is near-idle.

6. Intermittency is likely amplified by SQLite waits/background maintenance.

- Every DB connection sets `busy_timeout=5000`, so lock waits can cost up to 5s each: [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:157).
- Request connections are per-thread; a new thread connection runs WAL setup, schema DDL, and migrations: [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:38), [db.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/db.py:151).
- The poll command can drain the tag queue, heal titles, then run `wal_checkpoint(TRUNCATE)`: [cli.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/cli.py:232), [cli.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/cli.py:240).
- Backup uses `VACUUM INTO`: [backup.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/backup.py:73). Backfill/facet jobs can reindex many items: [api.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/api.py:1235), [ingest.py](/Users/shanu.kumar/Documents/Projects/tabtrack/cairn/ingest.py:405).

**Recommendations Ranked By Impact**

1. Batch item serialization. Fetch tags for all returned item IDs in one query and attach them in memory. Apply this to `/api/items`, `/api/history`, export paths, and any list/detail sidebar payloads. This removes the biggest N+1 pattern.

2. Add read indexes and rewrite filters to use them. At minimum: `items(saved_at DESC)`, `items(bucket, saved_at DESC)`, `items(status, saved_at DESC)`, `item_tags(tag_id, item_id)`, and either normalized non-null `bucket/source` values or expression indexes matching current `COALESCE(...)` filters.

3. Replace correlated tag subtree `EXISTS` filters with an indexed tag-first query. Resolve matching tag IDs once, join through `item_tags(tag_id, item_id)`, then join `items`. For hierarchy browsing at scale, consider a materialized tag closure table.

4. Stop polling heavy reads globally. Poll a cheap `/api/version` or `/api/changes` endpoint, then invalidate `stats`, `tags`, and visible item queries only when the database changed. Cache `stats`, `tag_tree`, and `digest` behind write-side invalidation.

5. Split history into day summaries and item expansion. First return date/count rows only; fetch items for a day only when expanded. Store/index `first_seen_date` instead of using `substr(first_seen, 1, 10)`.

6. Make Settings non-blocking. Return settings immediately, move `agents.status()` to a separate endpoint, run the four checks concurrently with short timeouts, and cache the result for a few seconds.

7. Move migrations out of request-time connection setup. Run schema/migrations once at startup or CLI init. Request connections should only open the DB and set read pragmas.

8. Reduce lock collision from maintenance jobs. Batch commits/reindexing, avoid `wal_checkpoint(TRUNCATE)` during active UI periods, and consider a single writer queue for background jobs so read endpoints do not stack behind repeated 5s waits.
