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
