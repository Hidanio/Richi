# Storage growth and index maintenance

The [synthetic retrieval/storage experiment](RETRIEVAL_STORAGE_EVAL.md) includes
measured revision growth and verifies that maintenance preserves canonical data.

Richi separates current knowledge, immutable revision history, indexes, the SQLite
write-ahead log, backups and captured evidence. A large workspace directory does
not necessarily mean a large searchable database. These commands observe the
selected workspace only and never inspect another workspace to complete a report.

```sh
richi storage stats
richi storage stats --deep --files --max-chars 8000
richi storage optimize
richi storage optimize --apply
```

`storage optimize` without `--apply` only describes the plan. The explicit apply
backs up SQLite first, then maintains indexes in one write transaction. No live
knowledge is deleted, summarized, moved or marked superseded. Application schema,
schema version, record IDs, rowids, history and idempotency events stay unchanged.
SQLite may update its own planner statistics. No speed or disk-size improvement is
promised: an already healthy index may have nothing to optimize.

## Size report

`storage stats` opens a read transaction. It reports:

- Resolved workspace and database, observation time, SQLite/schema version and
  configured search backend.
- SQLite page size, page count and free-page count. `allocated_bytes` is page size
  times page count; `free_page_bytes` is reusable space inside the database.
- Filesystem sizes of the main database, WAL and shared-memory sidecar separately.
- Counts of current cards/links, revision history and idempotency events.

The page snapshot and filesystem observations happen at different moments;
concurrent writes may make the figures differ. Main-file bytes alone can omit
committed pages still in WAL. Do not add WAL to logical table payloads and call
that “unique knowledge size.” Free pages and unused bytes within occupied pages
are also different measurements.

`--deep` additionally scans selected text columns for **UTF-8 byte counts** and
uses the optional `dbstat` virtual table to attribute allocated pages to current
records, history, FTS, idempotency, secondary indexes, and metadata/links. History
attribution includes its indexes. The report names the columns measured: these
logical text totals exclude some scalar columns and are neither complete row sizes
nor physical allocation. `dbstat` excludes freelist pages, WAL and external files.
Without that SQLite capability the report says `unavailable`; it does not report
zero bytes or enable an extension.

`--files` inventories only these directories relative to the selected database:

| Directory | Purpose |
|---|---|
| `backups/` | SQLite restore copies |
| `artifacts/git/` | Captured working-tree bytes |
| `evaluations/` | Local benchmark inputs and results |
| `runtime/` | Workspace runtime files |

No file contents, card text, titles, source URLs or custom schema names are emitted.
The workspace/database path is explicit. The inventory uses directory descriptors
and refuses symbolic links at every traversed level. Special files are skipped.
It counts regular-file logical sizes, so hard links count once per directory entry;
this is not filesystem block usage or a recursive inventory of the whole home.
Missing known directories are reported as absent. Unreadable or linked directories
and interrupted scans are incomplete, not zero-total proofs.

Database scans have a cooperative five-second SQLite progress budget. Directory
scans share a two-second/10,000-entry budget and a depth limit of 32. These limits
are checked during work, not a strict wall-clock guarantee for operating-system
calls or lock acquisition. Partial SQL counts are omitted, not filled with zeros.
`coverage.database_complete`, `coverage.files_complete`, and nested file/deep
coverage describe what was observed. Missing `dbstat` means deep coverage is
incomplete even when logical byte counts succeeded.

`--max-chars` is 2,000–100,000, default 8,000. It bounds the actual serialized JSON,
including the final newline and the selected compact/pretty format. Optional whole
sections can be omitted with `budget.omitted_sections` and
`coverage.output_complete: false`; identifiers and paths are never silently cut.
A scope that cannot fit fails with a request for a larger budget.

A single report measures size, not growth. Save dated reports outside source and
compare like-for-like scope/coverage before estimating growth per day. Reading
statistics does not checkpoint, run integrity checks, hash evidence, repair
indexes, alter knowledge or create backups.

## Explicit optimize

The plan names two possible operations:

1. FTS5 `optimize` merges the existing full-text index segments when the configured
   backend is FTS5. A scan-only backend skips this operation.
2. `PRAGMA optimize` asks SQLite to maintain useful planner statistics. SQLite
   3.32–3.45 uses an `analysis_limit` of 400; 3.46+ uses `optimize=0x10002` with the
   same configured limit and SQLite's own bounded-analysis behavior. Earlier
   versions skip this step because bounded analysis is unavailable. The previous
   connection-level analysis limit is restored afterward. `completed` means the
   pragma completed, not that SQLite necessarily analyzed a table.

`--apply` is workspace-wide and rejects `--project-path`. It requires a writable
connection and no pre-existing transaction. Before index work it calls Richi's
consistent SQLite backup API, checks backup integrity and keeps the returned copy
in `backups/`. Backup failure stops before index mutation. The response budget is
checked before the backup/write phase.

The subsequent index transaction is atomic: an error, busy writer or SQLite
progress interruption rolls it back and retains the backup. Index SQL has a
cooperative 30-second progress deadline; backup duration and lock waiting are
separate. The apply result includes the backup path and completed/skipped actions.
Ordinary busy-timeout behavior still applies. A successful apply preserves all
knowledge/history/event payloads and rowids; it may alter FTS pages, SQLite planner
statistics, WAL and file sizes. More bytes immediately after index maintenance
are possible and do not by themselves indicate a regression.

This command does **not** run VACUUM, manually checkpoint WAL, delete backups or
artifacts, drop/rebuild schema, advance verification dates or repair a corrupt
index. `check` remains the separate SQLite integrity/FK diagnostic; its success
alone is not a proof of FTS external-content consistency.

SQLite backups do not include `artifacts/git/` or repository objects needed to
reopen historical code evidence. Keep those with any complete restoration plan.
Do not manually remove active `-wal` or `-shm` files.

## How growth happens

Each changed entry/entity/edge retains its full previous and new JSON payloads in
append-only history. With a roughly constant payload size P, a modification adds
about 2P of logical history text, before page/index overhead. Even a small date or
state edit can copy a large card. Unchanged retries already avoid another history
revision; a new idempotency event ID can still add an event record.

FTS currently indexes current entry text from all knowledge states. Ordinary
query filters determine which states appear. Marking a card superseded retains
its text, sources and history; it is not storage reclamation. Backups are complete
SQLite snapshots, so multiple backups can exceed the active database size.
Captured Git bytes are content-addressed and deduplicate identical content;
distinct captured versions remain separate.

Before automatic retention, measure these sources independently. Never infer
that old, long, rarely returned or superseded knowledge can be deleted safely.

## Next changes to evaluate

The first potential write optimization is skipping FTS reindexing when indexed
columns are unchanged. The current UPSERT sets every column, so an `UPDATE OF`
trigger alone is insufficient: a `WHEN` comparison of old/new indexed values is
needed. Existing stores need an explicit reviewed trigger/schema maintenance
path. That change is not included in `storage optimize`.

Longer-term revision payload deduplication or a cold archive needs a separate
schema design, restore checks, historical source closure and stable ID lookup.
Current append-only triggers intentionally reject deleting history. `superseded`
means replaced knowledge, not a cold-storage retention label.

Do not garbage-collect Git blobs from only current-card references: histories may
still reference them, and capture can precede attachment. Backup retention needs
an explicit policy that preserves verified/pinned restore points. Idempotency
records also cannot be pruned casually: old event IDs would cease to replay.

VACUUM is deliberately excluded. Current entries use a text primary key and
implicit rowid while external-content FTS uses that rowid. SQLite documents that
VACUUM can change implicit rowids; a safe compaction design must establish stable
rowids or verify/rebuild FTS appropriately, and account for temporary disk space,
locking and recovery. Do not treat arbitrary VACUUM as a harmless cleanup.

Use synthetic fixtures to vary current record count, payload size, graph fanout
and revisions per card independently. Measure metadata-only writes, text writes,
unchanged retries, unique event IDs, and duplicate/unique evidence bytes. Compare
current/history/FTS/WAL/file growth, write/read latency and recall correctness,
including concurrent readers. Fresh-process timing is not necessarily a cold
filesystem-cache measurement. Public fixtures must contain no personal knowledge.

## SQLite references

- [PRAGMA optimize and analysis limits](https://www.sqlite.org/pragma.html#pragma_optimize)
- [FTS5 optimize command](https://www.sqlite.org/fts5.html#the_optimize_command)
- [DBSTAT virtual table](https://www.sqlite.org/dbstat.html)
- [Online Backup API](https://www.sqlite.org/backup.html)
- [Write-ahead logging](https://www.sqlite.org/wal.html)
- [VACUUM and implicit rowids](https://www.sqlite.org/lang_vacuum.html)
