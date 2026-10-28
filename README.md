# Richi

English | [Русский](README.ru.md)

**Engineering memory for AI agents.**

Richi turns individual tasks into accumulated engineering experience. It stores
project decisions, investigations, mistakes and knowledge so agents can use them
in later work.

Human time and attention are limited. Work can be shared among agents, but they
need memory, context and an understanding of how tasks relate. Richi preserves
that context together with sources and the conditions under which knowledge applies.

<a id="что-сохраняет-richi"></a>
## What Richi stores

- Task outcomes, decisions, investigations and failed attempts.
- Definitions and technical concepts scoped to a project and context.
- Explicit links between knowledge, components and projects.
- Source references and code revisions that support conclusions.

The implementation uses SQLite, text search, a relationship graph, an interactive
map and Git sources with history and revision comparison. Source checks help
identify code changes; matching bytes alone do not establish that a conclusion
is correct or that a release has shipped.

<a id="быстрый-старт"></a>
## Quick start

Richi is under active development. The standalone distribution includes the CLI,
SQLite store, Git navigation, local map and a portable agent skill. Supported
platforms are **macOS or Linux with Python 3.9+**. Code history features require
Git. The `richi use` menu provides quick workspace selection. An MCP server is
not included in this distribution yet.

```sh
curl -fsSL --proto '=https' --proto-redir '=https' https://github.com/Hidanio/Richi/releases/latest/download/install.sh | sh -s -- --yes
```

The installer becomes available after the first stable release containing it.
It installs Richi, connects Project Memory to all local chats in your Codex
profile, preserves existing instructions and updates the skill with Richi.
Open a new chat to choose its workspace, and a new terminal for the command.
Prerequisites include curl and Python with venv, ensurepip and sqlite3.
See [one-command setup](docs/SETUP.md) for preview, existing skills, recovery and
offline installation, or [manual installation](docs/INSTALLATION.md) for source builds.

For stable releases, use `richi update check`, `richi update apply` and
`richi update rollback`. Updates retain the previous version, dev settings and
workspace data. Enable background checks explicitly with
`richi update auto enable --interval 6h`; inspect results with
`richi update auto status`. This only records release availability. Automatic
installation is off by default; add `--install` to `auto enable` to authorize it
separately. See [updates](docs/UPDATES.md) and [releasing](docs/RELEASING.md).

For development, set the source checkout and enable the shared dev mode:

```sh
richi config set development.source /path/to/Richi
richi config set dev true
```

The CLI, agents and map now use this checkout's runtime code. The CLI picks up
changes on its next invocation; the map reloads its server after source changes.
Refresh the browser page to update the interface. Use `richi config set dev false`
to return to the installed version. Both modes share settings and the knowledge
database within the selected workspace. Launcher changes still require
reinstallation. See the [installation guide](docs/INSTALLATION.md).

For a new installation, `init` creates an empty database in the user data
directory, outside the source checkout. Examples do not preload an encyclopedia:
users or agents add knowledge as they work. Richi does not read chats, issue
trackers or repositories in the background.

**If you already have a database**, configure its path first and run `richi check`.
Standalone installation does not move or replace existing memory. The
[migration guide](docs/MIGRATION.md) covers moving data, shortcuts and agent
instructions to direct `richi` invocation.

<a id="рабочие-области"></a>
## Workspaces

Each workspace groups projects and keeps their knowledge in a separate database.
The same repository can belong to several workspaces with independent memory.
An existing default database remains in `default`.

```sh
richi workspace create work
richi -w work init
richi -w work project add /path/to/service-example
richi use work
richi map
# Quick selection in a terminal:
richi use
```

In a new chat, the agent asks once whether to use the current CLI workspace or
another workspace for that chat. `richi chat bind --current` saves the current
choice; `richi chat bind NAME` selects a chat-specific workspace. Later changes
through `richi use` do not redirect the chat. A detected unbound chat cannot
access memory.

Preview repository discovery with `richi project scan PATH`, then register the
found projects with `--apply`. `richi status` shows the chat context, workspace,
runtime and current directory's project membership; `richi status --projects`
checks registered paths. Before workspace selection, the chat receives only a
prompt to choose one. See the [workspace guide](docs/WORKSPACES.md) for isolation,
shared repositories and settings.

<a id="настройка-и-работа-агента"></a>
## Agent setup and workflow

- [Installation and configuration](docs/INSTALLATION.md): database, data directory,
  map port, environment variables and invocation from different directories.
- [Commands and data formats](docs/CLI.md): search, recording outcomes, the graph,
  definitions, source checks and manual maintenance.
- [Project Memory skill](skills/project-memory/SKILL.md): retrieving context and
  accumulating engineering knowledge with sources, history and concurrency checks.
- [Compact agent output](docs/COMPACT_OUTPUT.md): `--compact-json`, progressive
  loading of instructions and measured limits of token savings.
- [Discovery and batch retrieval](skills/project-memory/references/RETRIEVAL_TOOLS.md):
  short `discover` cards and complete records through `fetch`.
- [Metadata without loading cards](skills/project-memory/references/QUERY_TOOLS.md):
  `query count`, `exists`, `facets`, `select`; operation catalog: `capabilities list`.
- [Composed filters and indexed search](skills/project-memory/references/FILTERS.md):
  strict `--where` filters and explicit construction of a derived index.
- [Token and context measurements](docs/AGENT_WORKFLOW_EVAL.md): comparing built-in
  operations with card reads and working Python scripts.
- [Storage growth and index maintenance](docs/STORAGE.md): current records,
  history and files; preview and backup before optimization.

For Codex, the installer performs global integration; use `richi setup --status`
to inspect it or `richi setup --yes` to connect a supported existing installation.
For other agents, copy the entire `skills/project-memory` directory and configure
access to the installed CLI. Scope memory authorization in agent/project
instructions. See [setup](docs/SETUP.md) and the
[agent integration example](docs/INSTALLATION.md#подключение-агента).

The database, backups, local Git snapshots and configuration belong to the user
and are not part of the repository. Public tests use synthetic data; real work
records and private evaluation sets are not published.

## Documentation languages

Guides and skill references are available in English (`*.md`) and Russian
(`*.ru.md`), with a language link at the top of each page. Command names, API
fields and examples retain their technical spelling. The installed skill entry
point remains `SKILL.md`; `SKILL.ru.md` is its reference translation, not a second
skill. Repository `AGENTS.md` remains the agent instruction entry point. The
original English license is authoritative; its explanatory guide is bilingual.

<a id="лицензия"></a>
## License

Published Richi material uses a custom **source-available license**. The project
is not licensed as OSI open source.

- Personal noncommercial use by individuals is free.
- Organizational use, use on behalf of an organization and commercial use require
  a prior written agreement. This includes internal tools, employee and contractor
  work, evaluations and pilots.
- Anyone may read the public source code and documentation.
- Distributing copies or providing Richi as a service requires separate permission,
  except for rights independently granted by law, platform terms or other
  applicable licenses.
- Using Richi does not by itself change ownership of your data and knowledge.

Full terms: [LICENSE.md](LICENSE.md).
Examples and permission requests: [LICENSING.md](LICENSING.md).

<a id="участие"></a>
## Contributing

Suggestions and bug reports are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md)
before preparing code changes: rights to third-party contributions must be agreed
before incorporation into the product.

<a id="автор-и-контакт"></a>
## Author and contact

**Richardas Kuchinskas** — [hidanjkezor@gmail.com](mailto:hidanjkezor@gmail.com).

Copyright © 2026 Richardas Kuchinskas.
