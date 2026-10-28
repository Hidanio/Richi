<a id="рабочие-области"></a>
# Workspaces

English | [Русский](WORKSPACES.ru.md)

A workspace groups related projects and their knowledge. Each workspace has its
own SQLite database, record history, definitions, relationships, captured Git
bytes, settings and map logs. One repository can belong to several workspaces:
projects and knowledge are registered independently in each database, and matching
IDs do not overlap. Search and graph operations use only the selected workspace.

<a id="создание-и-переключение"></a>
## Creating and switching

```sh
richi workspace list
richi workspace current
richi workspace create work
richi -w work init
richi -w work project add /path/to/service-api
richi -w work project add /path/to/service-storage --id storage
richi use work
richi project list
richi map
```

`workspace create` creates an empty workspace's settings; `init` explicitly
creates its database. These commands do not copy knowledge from the current
workspace or clone repositories. `project add` accepts an existing directory and
stores its absolute path. The project ID and name default to the directory name;
`--id`, `--name` and `--description` are available. Adding the same ID and path
again preserves previously recorded metadata. A conflicting path for an existing
ID produces an error. Use `project upsert --json FILE` for deliberate metadata
changes.

```sh
richi use personal
richi use default
# В терминале без имени — пронумерованный список для быстрого выбора:
richi use
```

Switching changes the default workspace for subsequent commands. It does not stop
running operations or maps. A workspace name is required in a noninteractive
environment: Richi does not try to read a menu choice from an agent's input JSON.

`default` uses the existing configuration and memory: installing workspace support
does not move or split old records. New workspaces get separate
`workspaces/NAME/` directories inside the platform data directory. The
`workspaces.json` registry lives in the platform configuration directory.
`richi -w NAME config show` displays the actual paths.

In a normal terminal, explicit `--config` or `-w` selects the configuration;
`RICHI_CONFIG`, `RICHI_WORKSPACE` and the saved CLI default follow in precedence.
An explicit config cannot be combined with an explicit workspace. The rules below
pin a separate choice for a detected chat.

<a id="выбор-области-для-чата"></a>
## Choosing a workspace for a chat

The CLI's current workspace and a chat's workspace are separate settings.
`richi use work` changes the selection for normal terminal invocations. A new
chat first asks whether to use the current CLI workspace or choose another one
for that chat only.

```sh
richi chat current
richi workspace list
# После ответа пользователя — один из вариантов:
richi chat bind --current
richi chat bind work
```

The choice is saved for that chat ID without changing the global CLI default.
`--current` snapshots the current value: a later `richi use personal` in a
terminal will not redirect an already bound chat. There is no need to repeat the
question on each message or after context restoration. If the user has already
explicitly named a workspace in this chat, the agent uses that choice without
asking again.

In Codex, the launcher obtains the ID from `CODEX_THREAD_ID`. For other integrations:

```sh
richi --chat task-123 chat bind work
richi --chat task-123 project list
# Только по явному указанию изменить область этого чата:
richi --chat task-123 chat bind personal --replace
```

You can set `RICHI_CHAT_ID` instead of passing a flag; explicit `--chat` takes
precedence. Until a choice is saved, a detected chat cannot read or write
knowledge, even with `-w`. A `-w` that repeats the bound workspace is allowed;
another workspace and direct database/config overrides are rejected.
`chat current`, the workspace list and help are available before selection;
broken dev sources do not prevent binding management. This protects against
accidental context mixing, not access by other OS users. The CLI cannot display
a question inside another application's chat: the agent asks according to the
skill/AGENTS instructions, and the launcher verifies that the choice exists and
is consistent.

<a id="состояние-workspace-и-путей-проектов"></a>
## Workspace and project-path status

```sh
richi status
richi --project-path /path/to/checkout status --project service-api
richi status --projects --limit 50
```

`status` returns one JSON report: chat and CLI choices, the resolved workspace,
configuration and database paths, installed/dev runtime, database state and
membership of the specified path. The current working directory is checked by
default; `--project` checks the expected ID without selecting a workspace. An
unregistered current directory is acceptable for workspace-wide work, but its
knowledge must not be treated as verified for a particular project.

`--projects` adds diagnostics for all registered paths: missing directories,
inaccessible folders, broken Git registrations, duplicates and worktrees. A
project without a local path is informational. A normal invocation may also
inspect registered paths to identify the current project, but does not return
a full audit. It uses the `project check` rules and only the selected database.

`--limit` (1–200, default 50) bounds response details; problematic paths appear
first. `total`, `checked`, `omitted`, `counts` and `status` describe coverage:
omitted details or an incomplete check do not establish that there are no problems.
The internal budget is 10 seconds and 10000 registrations; the launcher limits
the worker to 30 seconds. An unfinished check does not confirm project membership.

Before chat binding, the command returns `selection_required` with possible next
commands, without reading runtime settings, the database or project paths. With
broken dev sources, available metadata and an error are retained; the installed
version is not substituted for the selected one. Binding and configuration
selection are snapshotted for each invocation.

The top-level `status` is `ok`, `attention`, `selection_required` or `unavailable`.
Exit code 0 means a report was produced, not that everything is healthy; automation
must inspect `status` and diagnostic coverage. Invalid arguments, a workspace
conflict or corrupt configuration produce a JSON error and exit code 1. The
command does not initialize, move, migrate or repair anything. It reads database
metadata and the project registry, not knowledge records; use `check` and an
explicitly requested `punisher` for content diagnostics.

<a id="добавление-папки-с-репозиториями"></a>
## Adding a directory of repositories

```sh
richi -w work project scan /path/to/repositories
richi -w work project scan /path/to/repositories --apply
```

The first invocation previews discovered repositories, grouped worktrees,
omissions and conflicts; the second registers the result in one transaction.
Traversal depth defaults to 3 and can be set with `--max-depth`. Scanning does not
enter an already discovered checkout's source tree, hidden directories, vendor,
node_modules or virtual environments, and does not follow nested symlinks. The
scan root must also be a real directory: use a symlink's target path. It does not
clone code, import previous knowledge or watch the folder; new checkouts are added
on the next run.

Existing IDs, descriptions and paths are preserved. Git worktrees match by their
common Git directory; separate clones are not merged merely because their remote
URLs match. On an ID conflict, error or incomplete traversal, `--apply` does not
register a partial list. The preview reflects the current state; applying checks
the directory and collisions again. The same repository can be registered
independently in another workspace.

<a id="проверка-проекта-перед-операцией"></a>
## Checking a project before an operation

```sh
richi -w work project check /path/to/checkout --id service-api
richi -w work --project-path /path/to/checkout recall 'кеш' --project service-api
richi -w work --project-path /path/to/checkout entry put --json result.json
```

`project check` displays matching status, workspace, database and project. It uses
only the selected workspace: a match in another database does not count. An
independent nested Git repository does not automatically belong to its outer
project. Ambiguous registrations need correction.

`--project-path` repeats the check within the operation's transaction and rejects
unknown paths or a mismatched `--project`. For entry writes, the verified project
must be in `project_ids`; other related projects in the same workspace are allowed.
You can deliberately omit the path for workspace-wide knowledge and administration.
The flag does not validate the text's meaning or replace source verification.
Database creation, migration and backup, project registration and map startup
reject this flag because they operate on the workspace or its registry. Run
`project check` separately for the map, which displays the entire workspace.

<a id="код-режим-разработки-и-карта"></a>
## Code, development mode and the map

Isolation applies to Richi memory. If two workspaces register the same repository
path, the files and Git working copy are shared: both see code changes. Register
separate Git worktrees or checkouts for independent changes. Richi does not
create them automatically or provide isolation between operating-system users.

A new workspace inherits the current `dev` and `development.source` settings at
creation. Its settings are independent afterward:

```sh
richi -w work config set development.source /path/to/Richi
richi -w work config set dev true
richi -w personal config set dev false
```

Dev and the installed version use one database within a workspace. Switching
modes does not move knowledge between workspaces or apply SQLite migrations.
Changes to the installed launcher require reinstalling the package.

`richi -w work map` opens that workspace's map. The map displays the workspace
name and remains pinned to its database even after `richi use personal`. Another
workspace gets a separate map on a free local port. Dev settings and source
reloads also stay pinned to the original workspace. Changing the default does
not change the contents of an already open tab.
