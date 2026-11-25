<a id="команды-richi"></a>
# Richi commands

English | [Русский](CLI.ru.md)

Use `richi agents --status` to see which local clients Richi detects and which
are connected. Setup defaults to Codex. Select Claude Code, Cursor or
Antigravity with `--agent claude`, `--agent cursor` or `--agent antigravity`.
Setup applies to the installation and runs before workspace selection. See the
[setup guide](SETUP.md).

Use `richi config show` to inspect the selected settings and runtime. In a
regular terminal, put database and configuration options before the subcommand:

```sh
richi --db /path/to/memory.sqlite3 recall 'local cache'
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

A global option that the command does not take also uses `usage_error`, for
example `--project-path` with `init`, `--compact-json` with `map`, or storage
options with `chat` or `agents`; `"help"` then names that command's page. Every
other error keeps its own `"code"` or has none, for example a reference, a
field name, a date or a `--where` expression that a command checks itself,
`--check-limit` without `--check-sources`, a storage option that the command
takes but the selected workspace refuses (`--db` with a workspace that keeps
its own storage), or a malformed chat ID (`"code": "invalid_chat_id"`).

In an agent chat that is not bound yet, a mistake that the runtime parses
outside help, such as a mistyped knowledge command, an unknown option or a
missing or malformed value, is reported as a missing binding
(`"code": "chat_workspace_required"`): until the chat chooses its workspace,
the launcher cannot hand the arguments to the selected runtime, which parses
them. The launcher checks some mistakes itself before the binding and reports
them as `usage_error` even then: those in its own commands (such as
`workspace`, `chat` and `config`), a mistyped launcher command or `help`, and
mistakes in the global options before the command (a mistyped one, a missing
value, the form of `-w`, `--db` and `--config`, `--workspace` with `--config`,
`--project-path` with `map`) or in the `--port`, `--db` and `--config` options
of `map`.

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
`richi --compact-json recall 'local cache'`. Successful stdout JSON omits
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
# Run in the foreground. Press Ctrl+C to stop.
richi map serve --open
# Return to the installed version.
richi config set dev false
```

Normally, CLI uses the fixed installed package. In dev, knowledge commands use
the runtime from the selected checkout, and the map reloads its server after source
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
richi -w work recall 'local cache' --project service-example
```

`richi use` opens its menu only in an ordinary terminal. In a new chat, the agent
checks the binding and asks once if the user has yet to choose a workspace.
In Cursor, insert the exact `--chat cursor:<id>` from the session hook after
`richi` in every chat example below. The `next_commands` returned by
`chat current` and `chat bind` already include it.

```sh
richi chat current
richi workspace list
```

Save the user's choice with `richi chat bind --current` or `richi chat bind work`.
If the chosen workspace is new, run `richi init` when the user has authorized
creating its store. Use `richi rules show` for workspace-wide work.
In a repository, verify membership and include its path when loading rules
and recalling memory.

```sh
richi project check /path/to/service-example --id service-example
richi --project-path /path/to/service-example rules show
richi --project-path /path/to/service-example recall 'local cache' --project service-example
```

To register several repositories, preview `richi project scan /path/to/repositories`
before applying the result with `--apply`. Change rules only at the user's
request. For creation examples and the `rules log` change history, see
[standing rules](../skills/richi/references/RULES.md).

The map's **Rules** tab shows them and, in a page opened by `richi map`, adds
and edits them with templates and a list of installed skills: one skill for every agent, or
one per agent when they differ.

The skill catalog lists sources for the built-in Claude Code, Codex, Cursor and
Antigravity profiles, including clients that have yet to be connected. You can switch their skill sources,
add skill folders or describe another agent. These settings apply to every workspace.

```sh
richi skill-sources list
richi skill-sources add gemini-cli --name "Gemini CLI" --skills ~/.gemini/skills
richi skill-sources update claude --disable claude-app
```

See [agents and their skills](../skills/richi/references/AGENT_SKILLS.md); the
map's **Agents** tab shows and changes the same settings.

In a bound chat, `-w` may only repeat the stored workspace. Direct `--db`,
`--config` and corresponding storage environment variables are rejected.
See [workspaces and chat identity](WORKSPACES.md) for the other supported clients.

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

Complete references live in `skills/richi/references`. This is the
single documentation source for CLI and the installed skill: the entire skill
directory is portable, without links to files outside it.

| Task | Reference |
|---|---|
| Create a database, register a project, make a backup | [CLI](../skills/richi/references/CLI.md) |
| Load or manage the user's standing rules | [Rules](../skills/richi/references/RULES.md) |
| Create or fully replace a record, save a task card | [Writing and conflict protection](../skills/richi/references/ENTRY_WRITE.md) |
| Find prior work and understand result limits | [Recall](../skills/richi/references/RECALL.md) |
| Select candidates and batch-read full records | [Discover and fetch](../skills/richi/references/RETRIEVAL_TOOLS.md) |
| Assemble question and code-change context | [Brief](../skills/richi/references/BRIEF.md) |
| Link knowledge to code versions, open history and diffs | [Git sources](../skills/richi/references/GIT.md) |
| Capture checked checkout/worktree bytes and attach a source | [Capture and attach](../skills/richi/references/GIT_CAPTURE.md) |
| Find knowledge associated with changed files | [Impact](../skills/richi/references/IMPACT.md) |
| Create entities and edges | [Graph](../skills/richi/references/GRAPH.md) |
| Read neighbors of a known record | [Neighbors and output boundaries](../skills/richi/references/NEIGHBORS.md) |
| Describe terms with different meanings and versions | [Encyclopedia](../skills/richi/references/ENCYCLOPEDIA.md) |
| Find tools, configurations and tasks that used them | [Tools](../skills/richi/references/TOOLS.md) |
| Investigate a missing known result | [Recall diagnostics](../skills/richi/references/RECALL_DEBUG.md) |
| Check workspace status, candidates or a source group | [Diagnostics](../skills/richi/references/DIAGNOSTICS.md) |
| Assess duplicates and consolidation without changes | [Bounded audit](../skills/richi/references/MEMORY_AUDIT.md) |
| Manually consolidate knowledge and inspect punisher candidates | [Maintenance](../skills/richi/references/MAINTENANCE.md) |
| Validate a retrieval change with reproducible examples | [Evaluation](../skills/richi/references/EVALUATION.md) |
| Measure storage growth and maintain the search index | [Storage](STORAGE.md) |

Examples use fictional projects `service-example`, `service-consumer`, task
`ABC-123` and `example.com` links. Replace them with verified data for your
project. Example dates, SHAs and paths are not evidence.
