<a id="локальная-память-проектов"></a>

# Local project memory

English | [Русский](CLI.ru.md)

SQLite stores the project map, relationships and working experience: tasks, research, attempts, decisions, facts and instructions. A Jira ID is optional. Records help answer “have we ever done X,” find the outcome and understand why a decision was made.

The encyclopedia stores definitions of project terms as `concept` nodes in the same
database. One term may have several meanings across contexts and versions;
shared synonyms do not merge cards. The catalog, authoring rules and semantic
variants are described in [ENCYCLOPEDIA.md](ENCYCLOPEDIA.md).

The database is stored separately from source code; `richi config show` reports its actual path. Python 3.9 or newer with the standard `sqlite3` module is required; the runtime uses the standard library and requires no cloud service. The interactive map uses an optional local HTTP server on `127.0.0.1`. Calls are explicit: there is no background reading of conversations, Jira or repositories, or updating of knowledge. A source in a record indicates provenance; the existence of a link alone does not prove a claim.

<a id="быстрый-старт"></a>

## Quick start

After installing Richi, commands are available from any working directory. The `init`
and direct `--db` examples below apply to an ordinary terminal: in a chat, workspace
selection and binding according to [SKILL.md](../SKILL.md) are required first. Direct storage/config
overrides do not bypass that binding.

```sh
richi init
richi project list
richi recall 'почему выбрали локальный кеш' --max-chars 16000
richi brief 'какой опыт учесть перед изменением кеша' --max-chars 16000
richi search 'оптимизация запросов'
richi context --project service-example --limit 20
richi check
richi backup
richi punisher --limit 20 --max-chars 16000
richi punisher --project service-example --summary-warn-chars 6000
```

For another database, put `--db` **before** the command:

```sh
richi --db '/путь с пробелами/память.sqlite3' init
```

All results are JSON on stdout; errors are JSON on stderr with a nonzero exit code. `--help` prints plain text. For `put/upsert`, `--json` accepts a UTF-8 file or `-` for stdin. An import accepts one object or an array of up to 1000 objects and runs atomically: an error in one object rolls back the entire batch. Duplicate IDs within a batch are forbidden; the maximum input size is 10 million characters. Git `sources --json` commands accept a single-source file, without stdin.

<a id="компактный-json-для-ии-и-скриптов"></a>

### Compact JSON for AI and scripts

The global `--compact-json` flag goes **before the command**:

```sh
richi --compact-json --project-path /work/example recall 'причина решения' --project service-example
richi --compact-json --project-path /work/example entry get task:ABC-123
```

It removes indentation and separator spaces from successful JSON on stdout.
The existing indented format remains the default; strings within values
are not compressed and fields are not renamed. Errors on stderr, help/version,
`--output` files and HTTP/map retain their format; `--compact-json map` is rejected.
For bounded commands, size and fitting use the same renderer that emits
the response: `budget.output_chars` includes the final newline.
At the same `--max-chars`, a compact response may fit more cards
or sources; therefore identical full bounded responses and a fixed
percentage of token savings are not promised. This is formatting, not a new retrieval mode
or a guarantee of excerpt completeness.

A launcher installed with flag support is required, even with `dev=true`.
If an older launcher specifically reports an unknown `--compact-json`, repeat
the command without that flag using the same workspace/project selectors. Do not change runtime,
configuration or binding for formatting. See
[WORKSPACES.md](WORKSPACES.md) for details.

Read a known ID directly through `entry get`, `entity get` or `edge get` when
a full current record is needed. Prior recall and entry read calls are not required:
a short full record may be smaller than a bounded wrapper. Add `--history`
for chronology. For an unknown ID, first find the record and follow `read_more`.

Read commands open SQLite in `mode=ro` with `query_only=ON`.
`punisher` selects candidates for manual maintenance, `recall --explain --expect REF`
explains misses, and `sources snapshot/check` tracks changes in selected
cards, relationships and local files. Examples and limits are in
[DIAGNOSTICS.md](DIAGNOSTICS.md). These operations do not update knowledge or invoke models.
`punisher --summary-warn-chars N` accepts an integer from 1–1000000 (default 6000).
`long_summary` means that the `len` of the stored Unicode summary is strictly greater than N;
the threshold is not a write limit. Severity is `review` for confirmed and `info`
for hypothesis/superseded. A finding includes length, threshold, kind, state and exact
`read_arguments` for a full read; length does not prove staleness or poor
quality. Check `summary_warn_chars`, coverage and omissions in the report;
its `health` still describes database integrity separately from candidates.

To connect knowledge with code versions, use `sources capture/attach`,
`sources git-check`, `sources show/history/diff` and reverse lookup through `sources related`.
The full SHA, path and byte hash are stored; uncommitted code gets a separate
snapshot. History is read without checkout or network access. Examples are in [GIT.md](GIT.md).

`brief QUERY` assembles task context from recall; with `--project ID --base BASE`,
it adds knowledge for changed files and one hop along stored relationships.
Results are deduplicated within a shared budget, preserving states,
sources and selection reasons. Optional `--check-sources` compares the bytes
of Git sources from selected cards with the target: up to 12 attempts by default,
with separate coverage and omission reasons. Without `--base` and `--check-sources`,
Git is not invoked. Examples and limits are in [BRIEF.md](BRIEF.md).

<a id="проекты"></a>

## Projects

`project upsert` creates or fully updates a project. `id` is a stable name, usually the repository directory name. `id` and `name` are required; other fields may be absent.

```sh
richi project upsert --json - <<'JSON'
[
  {
    "id": "service-example",
    "name": "Example service",
    "repo_path": "/work/example service",
    "description": "Назначение сервиса, установленное по документации.",
    "source": "/work/example service/README.md",
    "verified_at": "2026-09-29"
  },
  {
    "id": "service-consumer",
    "name": "Consumer service"
  }
]
JSON
```

The names and paths in these examples are illustrative; they are not knowledge about real projects. A project object contains `id`, `name`, `repo_path`, `description`, `source`, `verified_at`. `project list` returns `{"projects": [...]}`.

<a id="опыт-задачи-и-решения"></a>

## Experience, tasks and decisions

Creation, full payload replacement, CAS, history, event_id and current task cards
are described in [ENTRY_WRITE.md](ENTRY_WRITE.md). That reference is sufficient for an ordinary edit;
other CLI sections cover separate operations.

<a id="связи-проектов"></a>

## Project relationships

```sh
richi relation put --json - <<'JSON'
{
  "id": "relation:example-consumer-api",
  "from_project": "service-consumer",
  "to_project": "service-example",
  "kind": "calls_api",
  "description": "Гипотеза: consumer вызывает API example; проверить по конфигурации.",
  "knowledge_state": "hypothesis"
}
JSON
```

`id`, `from_project`, `to_project` and `kind` are required. Both projects must exist and differ. `kind` is a free-form nonempty string, for example `calls_api`, `publishes_to`, `shares_storage`, `business_dependency`. Optional fields are `description`, `source` (a string), `knowledge_state`, `verified_at`. `confirmed` requires `source`. Updating by ID fully replaces the fields. Change history is maintained for entries; projects and relationships store only current state.

<a id="поиск-и-контекст"></a>

## Search and context

For a code-change task, file-based knowledge selection is available:
`sources impact --project PROJECT_ID --base BASE --target HEAD` or
`sources impact --project PROJECT_ID --base HEAD --worktree`.
The command matches changed paths against record sources and returns
compact excerpts, match reasons, knowledge states and coverage.
It uses structured Git anchors and safely resolved legacy
Git links; it does not infer the semantic impact of a change.
The pilot and limits are described in [IMPACT.md](IMPACT.md).

For an agent's initial search, prefer `recall "вопрос" --project PROJECT_ID --limit 8 --max-chars 16000`: exact IDs, weighted text retrieval, entity retrieval and one hop along explicit relationships with a bounded JSON size. The project raises priority without excluding neighboring services. Read full records and history as needed. Format, budget, currency and checks are described in [RECALL.md](RECALL.md). The commands below retain the previous full output and filtering rules.

```sh
richi search 'кеширование запросов' --limit 20
richi search 'ABC-123' --project service-example
richi search 'старое решение' --include-superseded
richi context --project service-example --limit 30
```

Search covers ID, title, summary, Jira key, tags and aliases. Query words are treated as literals; user-supplied FTS/SQL syntax is not executed. One prefix word match is sufficient; results sort by FTS5 BM25, then recency. Quotes, parentheses and punctuation are safely split into words; a query without words returns an empty list. Query length is limited to 2000 characters and 64 words. The result limit is 1–200, default 20. Common words in a long question may produce irrelevant results: short concepts and several formulations are useful.

This is lexical retrieval, not embedding search. There are no automatic Russian inflections, translation, synonyms or paraphrase understanding. Add alternative formulations and Russian/English terms to `aliases`; try several queries when looking for past work. No result means only that no record was found, not proof that the work never happened.

If SQLite lacks FTS5 when a database is created, `scan` is used automatically: Unicode-casefold substring search, any-word matching and date sorting. This is slower on a large database and yields somewhat different matches. `init`, `check` and `search` report `search_backend`; `search` also returns `match_mode`. Triggers update the FTS5 index together with writes. An existing FTS5 database must be opened using Python/SQLite with FTS5; the index is not automatically removed when the environment changes.

`context` includes the selected project, direct relationships in **both directions**, descriptions of neighboring projects and a bounded record list. Records from the project come first, followed by neighbors; within those groups, recently updated records come first. It returns `project`, `relations`, `related_projects`, `entries`, `limit`, `selection`. Context does not traverse beyond one hop. `superseded` records and relationships are hidden by default; `--include-superseded` includes them. Global notes without project membership are available through `search` but are not added to every project context.

<a id="надёжность-и-резервные-копии"></a>

## Reliability and backups

For manual deduplication and knowledge consolidation, ask `project-memory`
to perform maintenance in a separate task. This mode does not run on a schedule
or on every search. The procedure, preservation of sources/history and relationship
checks are described in [MAINTENANCE.md](MAINTENANCE.md).

WAL, a lock wait of up to 10 seconds, foreign keys and explicit transactions are used. Competing processes serialize writes; to protect a card from losing concurrent changes, pass `expected_updated_at` from the latest read. Without that field, the new payload replaces the previous one. There is no automatic conflict merging.

`check` verifies schema version, SQLite integrity and foreign keys. New databases use schema 2. Older commands are compatible with schema 1; the graph requires an explicit migration: `backup`, then `migrate`. Unknown versions are rejected. Repeated `init` does not reset data; schema 1 requires `migrate`, and an unknown nonempty database is not reinitialized. Original SQL tables are `projects`, `entries`, `entry_projects`, `relations`, `entry_history`, `entry_events`, `metadata` and, where supported, `entries_fts`. Schema 2 adds `entities`, `graph_nodes`, `graph_edges`, histories for new objects and the `graph_links` view, preserving original records and FTS.

```sh
richi backup
richi backup --output '/путь с пробелами/memory-backup.sqlite3'
richi --db '/путь с пробелами/memory-backup.sqlite3' check
```

Without `--output`, a backup is saved in `backups/` with a UTC timestamp in its name. The SQLite backup API includes committed data from WAL and validates integrity. Existing files are not overwritten. To restore, first stop processes using the original database; the simplest safe option is to use the verified copy through `--db`. Do not copy only an active `memory.sqlite3` while ignoring `-wal` and `-shm`.

Git sources additionally use local repository objects and snapshots
in `artifacts/git/` alongside the database. SQLite backup does not include these files; preserve them
separately for a complete transfer of historical bytes. See [GIT.md](GIT.md).

The database and backups are ordinary unencrypted local files. Store necessary work summaries and links; avoid secrets and unnecessary personal data. Do not commit the database, WAL/SHM or backups to Git; source code and instructions can be versioned separately.

<a id="интерактивная-карта"></a>

## Interactive map

`richi map` starts the map in the background and opens the browser. Repeated
launches reuse a compatible server for the same database. The default
port is 8765; if occupied, the launcher looks for an available port. Database and port settings
are shown by `richi config show`. Startup logs are stored in `runtime/` alongside the database.

```sh
richi map
# Для сервера в текущем терминале:
richi map serve --open
# Другая база и порт задаются явно:
richi --db /path/to/memory.sqlite3 map serve --port 8766 --open
```

The server is accessible only through `127.0.0.1`. In `map serve` mode, Ctrl+C
stops the process. Richi does not install a desktop shortcut automatically.
An existing local shortcut can be retained through the transitional adapter.

The initial view shows projects. Map search finds titles, descriptions, tags and aliases of loaded objects; selecting an object reveals its neighborhood within 1–3 hops. Nodes can be dragged, and the mouse changes zoom and position. A node card shows the full summary, work state, verification date and sources. An edge card shows direction, meaning, provenance and evidence. Hypotheses and superseded knowledge are enabled separately. Record membership in a project comes automatically from `project_ids`; it does not prove an effect on service behavior.

The refresh button rereads the database, including records from other tasks. There is no automatic polling or editing through the map: new knowledge and relationships are written through the CLI and skill. The server serves the map, graph data and stored Git sources through bounded APIs; it accepts reads only from loopback and uses no external libraries, analytics or CDN. External sources open only when a link is clicked; a local document path can be copied.

Git sources have **Code**, **History**, **Diff** and **Check** buttons.
They also work for legacy `git:<full SHA>:<path>` links; whole-commit
links have **Commit files** for selecting a changed file. The repository and commit
are checked locally when opened; the original record is not rewritten.
The viewer reads the stored source through a bounded API, while comparison reads the selected
local version or working copy. URLs do not provide arbitrary file access.
If another task changes a card, the map asks to refresh data first.
See [GIT.md](GIT.md). After updating the server, run `richi map` again:
the launcher will choose a version supporting Git viewing.

For large graphs, export is limited to 2000 nodes and 10000 edges; the map reports truncation. Map search operates within the loaded snapshot. Full-text `richi search` over records operates separately and is not restricted to the map snapshot. The shared graph is stored in SQLite; a separate graph server is not yet needed. Formats for new entities, relationships and commands are described in [GRAPH.md](GRAPH.md).
