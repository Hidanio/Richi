# Richi development

Develop the implementation in `src/richi/` and the distributable agent skill in
`skills/project-memory/`. Installed skills and legacy entry-point bridges are
local deployment copies; update and review their source here first.

The runtime targets Python 3.9+ on macOS and Linux and uses the standard library.
Install into a virtual environment with `python -m pip install -e .`.
Run `python -m unittest discover -s tests -v` and
`python evals/evaluate_recall.py`. Tests and public evaluations use disposable
stores and synthetic repositories; they must not depend on personal memory.

For authorized work with an existing knowledge store, use the project-memory
skill and inspect `richi config show` to resolve its location. Search relevant
records before repeating investigations; preserve evidence, scope, historical
qualifications, and optimistic concurrency when recording outcomes. Code and
external task systems remain authoritative for current state.

Keep SQLite stores, backups, captured Git bytes, private evaluation corpora,
local configuration, credentials, and runtime files outside committed source.
Public examples must be synthetic. Preserve copyright and licensing attribution.
