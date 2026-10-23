# Storage size and explicit index maintenance

Use for requested storage diagnosis or maintenance, not before ordinary searches.
Workspace selection follows [SKILL.md](../SKILL.md). These operations cover the
selected workspace database, even when a project path validates membership.

```sh
richi --compact-json storage stats --deep --files --max-chars 8000
richi --compact-json storage optimize
# Only within authorized index maintenance:
richi --compact-json storage optimize --apply
```

`stats` is read-only. Default counts and page/file sizes are separate;
`--deep` adds logical text bytes and optional physical page attribution;
`--files` inventories bounded known directories without reading content or
following links. Inspect `coverage`, `budget.omitted_sections` and unavailable
capabilities. One observation is size, not a growth rate. Filesystem and database
observations are not one atomic snapshot. WAL, free pages, history, backups and
evaluation files must not be conflated with current searchable knowledge.

`optimize` defaults to a read-only plan. Explicit `--apply` rejects
`--project-path`, creates a verified SQLite backup, then performs supported
FTS/planner index maintenance in an atomic transaction. SQLite planner statistics
may change. Failure rolls back index work and retains the backup. It does not
delete knowledge/history/events/files, VACUUM, checkpoint, migrate application
schema or advance verification dates. A successful operation does not prove a
speed or disk-size improvement; compare relevant measurements.

Backups do not include Git evidence blobs or repository objects. Preserve those
for restoration. Never manually delete WAL/SHM, prune immutable history or
idempotency events, or collect Git blobs based only on current records. Historical
references and capture-before-attach can still require them. A cold archive needs
a separate reviewed retention and restoration contract. For semantic cleanup use
[MEMORY_AUDIT.md](MEMORY_AUDIT.md) and [MAINTENANCE.md](MAINTENANCE.md).
