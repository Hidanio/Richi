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
```

<a id="справка"></a>
## Built-in help

`richi` without arguments prints a short overview of the most common commands.
The complete reference is built into the CLI and never opens a knowledge store:

```sh
richi help                    # every command, grouped by purpose
richi help recall             # one command: description, options, examples
richi help sources capture    # a subcommand; same as richi sources capture --help
richi help topics             # topics shared by many commands
richi help filters            # the --where grammar
```

Topics: `richi help start` (first steps), `richi help records` (record types,
references, knowledge states, safe updates), `richi help filters`, `richi help
output` (JSON, errors, exit codes, budgets) and `richi help environment`
(variables and default locations). Each command page ends with examples,
related pages and a link to its detailed guide, plus the path of a local copy
when one is installed, so pages can be read offline.

A mistyped command, option or topic name, or a mistyped value from a fixed
list of choices, gets a suggestion. A mistake that the argument parser catches
(an unknown command or option, a missing or malformed value, options that a
usage line separates with `|`, such as `[--target REF | --worktree]`,
`--workspace` with `--config`, or an argument that a run without a terminal
needs) uses `"code": "usage_error"` and names the page that explains the
command in `"help"`:

```json
{"error": "'stauts' is not a richi command. Did you mean 'status'? See 'richi help'.", "code": "usage_error", "help": "richi help"}
```

Every other error keeps its own `"code"` or has none, for example a reference,
a field name, a date or a `--where` expression that a command checks itself,
`--check-limit` without `--check-sources`, `--db` with a workspace that keeps
its own storage, or a malformed chat ID (`"code": "invalid_chat_id"`).

In an agent chat that is not bound yet, any mistake in a knowledge command, a
mistyped name as well as an unknown option or a missing or malformed value, is
reported as a missing binding (`"code": "chat_workspace_required"`): until the
chat chooses its workspace, the launcher cannot hand the arguments to the
selected runtime, which parses them. Launcher commands such as `workspace`,
`chat` and `config` still get `usage_error` and suggestions.

Help needs no chat binding. In an agent chat it is printed by the installed
release, so new help from a development checkout appears there after the
package is reinstalled. In a terminal the selected runtime prints it; when the
selection itself is broken, for example by an unknown `RICHI_WORKSPACE`, the
installed release prints it instead, so `richi help environment` still explains
the variables. The pages of the launcher commands (`agents`, `setup`, `update`,
`status`, `workspace`, `use`, `chat` and `config`) always come from the
installed launcher, which also runs these commands, so their changes appear
only after the package is reinstalled.

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
`config set dev` also moves the managed agent skills to the checkout and back
([what dev switches](INSTALLATION.md#what-dev-switches)).
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
| Find tools, configurations and tasks that used them | [Tools](../skills/project-memory/references/TOOLS.md) |
| Investigate a missing known result | [Recall diagnostics](../skills/project-memory/references/RECALL_DEBUG.md) |
| Check workspace status, candidates or a source group | [Diagnostics](../skills/project-memory/references/DIAGNOSTICS.md) |
| Assess duplicates and consolidation without changes | [Bounded audit](../skills/project-memory/references/MEMORY_AUDIT.md) |
| Manually consolidate knowledge and inspect punisher candidates | [Maintenance](../skills/project-memory/references/MAINTENANCE.md) |
| Validate a retrieval change with reproducible examples | [Evaluation](../skills/project-memory/references/EVALUATION.md) |
| Measure storage growth and maintain the search index | [Storage](STORAGE.md) |

Examples use fictional projects `service-example`, `service-consumer`, task
`ABC-123` and `example.com` links. Replace them with verified data for your
project. Example dates, SHAs and paths are not evidence.
