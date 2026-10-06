# Richi development

Develop the implementation in `src/richi/` and the distributable agent skill in
`skills/project-memory/`. Installed skills are local deployment copies; update
and review their source here first. Use the installed `richi` CLI for all local
operations; do not add wrappers for the former standalone Python scripts.

The runtime targets Python 3.9+ on macOS and Linux and uses the standard library.
Install a fixed package into a dedicated virtual environment: upgrade pip with
`python -m pip install --upgrade pip`, then run `python -m pip install .`.
The installed launcher in `launcher/richi_launcher/` selects the runtime before
importing it. Configure the checkout with
`richi config set development.source /path/to/Richi`, then `richi config set dev true`.
The same configuration selects development sources for CLI, agents, and map;
`richi config set dev false` restores the installed package without changing data.
Ordinary `src/richi/` edits take effect on the next CLI call and reload the running
map. Launcher changes require reinstalling the package. Use `richi config show`
to inspect the selected runtime. For an isolated module invocation use
`python -I -m richi` with the installation's Python.

Run `python -m unittest discover -s tests -v` and
`python evals/evaluate_recall.py`. Run installed-package checks in a disposable
virtual environment with the current checkout installed normally, so they test
the proposed version rather than a previous fixed installation. Tests and public
evaluations use disposable stores and synthetic repositories; they must not
depend on personal memory. Test schema changes against a copy: selecting a
runtime never migrates SQLite automatically.

For authorized work with an existing knowledge store, use the project-memory
skill and inspect `richi config show` to resolve its location. Search relevant
records before repeating investigations; preserve evidence, scope, historical
qualifications, and optimistic concurrency when recording outcomes. Code and
external task systems remain authoritative for current state.

Keep SQLite stores, backups, captured Git bytes, private evaluation corpora,
local configuration, credentials, and runtime files outside committed source.
Public examples must be synthetic. Preserve copyright and licensing attribution.

At each new chat, inspect `richi chat current` and `richi workspace list` before
memory access. If unbound and the human has not explicitly selected a workspace
in this chat, ask whether to use the current CLI workspace or a chat-only
override. After the answer, `richi chat bind --current` or `richi chat bind NAME`
saves that choice. Reuse it in later turns. A global CLI switch does not redirect
the chat; only an explicit human request permits `chat bind NAME --replace`.
For non-Codex integrations supply stable RICHI_CHAT_ID or --chat on every call.
Do not bypass a binding by changing/unsetting the chat ID or storage selectors.

For repository work run `project check PATH --id PROJECT`, then use
`--project-path PATH` on relevant memory operations to validate membership again.
Workspaces isolate knowledge, not shared source files. `project scan ROOT`
previews discovery; --apply registers a complete conflict-free result atomically.
Never copy another workspace's knowledge without a specific request. Use
synthetic homes/registries in tests, clearing ambient CODEX_THREAD_ID in tests
that are not exercising chat behavior. Test same-project isolation and worktrees.
