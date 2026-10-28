<a id="команды-richi"></a>
# Richi commands

English | [Русский](CLI.ru.md)

Global Codex integration: `richi setup --dry-run`, `richi setup --yes`,
`richi setup --status`. Setup is installation-wide, needs no chat binding,
rejects workspace/storage selectors and preserves existing instructions.
See [SETUP.md](SETUP.md) for installation, adoption flags and recovery.

After installation, use `richi` from any working directory. `richi config show`
displays selected settings and runtime. Global database and configuration options
precede the subcommand:

```sh
richi --db /path/to/memory.sqlite3 recall 'локальный кеш'
richi --config /path/to/config.json check
richi --help
richi sources --help
```

Agents can add global `--compact-json` before the subcommand:
`richi --compact-json recall 'локальный кеш'`. Successful stdout JSON omits
indentation; the default format is preserved. `--max-chars` uses the selected
format, so more data may fit a tight budget. Errors, help, `--output` files and
the map are unchanged; combining the flag with `map` is rejected. See the
contract and measurements in [COMPACT_OUTPUT.md](COMPACT_OUTPUT.md).

Select the mode in the current workspace's settings:

```sh
richi config set development.source /path/to/Richi
richi config set dev true
richi config show
richi map
# Сервер в текущем терминале; Ctrl+C завершает его:
richi map serve --open
# Возврат к установленной версии:
richi config set dev false
```

Normally, CLI uses the fixed installed package. In dev, commands use current
code from the selected checkout, and the map reloads its server after source
changes. A running map also detects mode switches after validating the new
runtime. Refresh the browser page for interface changes. The database and
knowledge history are shared; SQLite is never migrated automatically.
`config show` and disabling dev remain available even if sources are broken.
See [installation instructions](INSTALLATION.md) for installation, updates
and diagnostics.

Select a workspace before the subcommand:

```sh
richi workspace list
richi workspace create work
richi -w work init
richi -w work project add /path/to/service-example
richi use work
richi use
richi -w work recall 'локальный кеш' --project service-example
```

`richi use` opens its menu only in an ordinary terminal. In a new chat, the agent
asks for a workspace once, then saves the selection:

```sh
richi chat current
richi chat bind --current
# Или другая область только для чата:
richi chat bind work
richi project check /path/to/service-example --id service-example
richi --project-path /path/to/service-example recall 'локальный кеш' --project service-example
# Просмотр и последующий импорт папки репозиториев:
richi project scan /path/to/repositories
richi project scan /path/to/repositories --apply
```

The `bind` alternatives are mutually exclusive: run only the user's selection.
In a bound chat, `-w` may only repeat the stored workspace. Direct `--db`,
`--config` and corresponding storage environment variables are rejected.
For another client, pass a stable ID through `--chat ID` or `RICHI_CHAT_ID`;
Codex supplies `CODEX_THREAD_ID`. See [workspaces and knowledge isolation](WORKSPACES.md).

Unified context and path checks:

```sh
richi status
richi --project-path /path/to/service-example status --project service-example
richi status --projects --limit 50
```

The report only reads metadata from the selected workspace. Exit code 0 means
a report was produced; inspect `status` and `diagnostics.projects` coverage.
An unbound chat receives `selection_required` without database access. A broken
runtime returns `unavailable` with available settings and a reason. See details
and limits in the [workspace guide](WORKSPACES.md#состояние-workspace-и-путей-проектов).

Complete references live in `skills/project-memory/references`. This is the
single documentation source for CLI and the installed skill: the entire skill
directory is portable, without links to files outside it.

| Task | Reference |
|---|---|
| Create a database, register a project, make a backup | [CLI](../skills/project-memory/references/CLI.md) |
| Create or fully replace a record, save a task card | [Writing and conflict protection](../skills/project-memory/references/ENTRY_WRITE.md) |
| Find prior work and understand result limits | [Recall](../skills/project-memory/references/RECALL.md) |
| Select candidates and batch-read full records | [Discover and fetch](../skills/project-memory/references/RETRIEVAL_TOOLS.md) |
| Assemble question and code-change context | [Brief](../skills/project-memory/references/BRIEF.md) |
| Link knowledge to code versions, open history and diffs | [Git sources](../skills/project-memory/references/GIT.md) |
| Capture checked checkout/worktree bytes and attach a source | [Capture and attach](../skills/project-memory/references/GIT_CAPTURE.md) |
| Find knowledge associated with changed files | [Impact](../skills/project-memory/references/IMPACT.md) |
| Create entities and edges | [Graph](../skills/project-memory/references/GRAPH.md) |
| Read neighbors of a known record | [Neighbors and output boundaries](../skills/project-memory/references/NEIGHBORS.md) |
| Describe terms with different meanings and versions | [Encyclopedia](../skills/project-memory/references/ENCYCLOPEDIA.md) |
| Investigate a missing known result | [Recall diagnostics](../skills/project-memory/references/RECALL_DEBUG.md) |
| Check workspace status, candidates or a source group | [Diagnostics](../skills/project-memory/references/DIAGNOSTICS.md) |
| Assess duplicates and consolidation without changes | [Bounded audit](../skills/project-memory/references/MEMORY_AUDIT.md) |
| Manually consolidate knowledge and inspect punisher candidates | [Maintenance](../skills/project-memory/references/MAINTENANCE.md) |
| Validate a retrieval change with reproducible examples | [Evaluation](../skills/project-memory/references/EVALUATION.md) |
| Measure storage growth and maintain the search index | [Storage](STORAGE.md) |

Examples use fictional projects `service-example`, `service-consumer`, task
`ABC-123` and `example.com` links. Replace them with verified data for your
project. Example dates, SHAs and paths are not evidence.
