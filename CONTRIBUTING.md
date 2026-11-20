# Contributing to Richi

English | [Русский](CONTRIBUTING.ru.md)

Contributions are welcome. Open a pull request directly with a bug fix, a new
feature, or a documentation change. For ideas and bug reports, open an issue.
When reporting a bug, include the Richi version and a small example with
synthetic data so others can reproduce it.

We accept contributions under the [project license](LICENSE.md).

## Development

Use branch names without a `codex/` prefix and use underscores instead of hyphens,
for example `audit_fixes_bilingual_docs`.

Use macOS or Linux with Python 3.9+ and Git. Install the current checkout normally
into a disposable virtual environment. From the repository root:

```sh
richi_test_venv="$(mktemp -d "${TMPDIR:-/tmp}/richi-tests.XXXXXX")"
python3 -m venv "$richi_test_venv"
. "$richi_test_venv/bin/activate"
python -m pip install --upgrade pip
python -m pip install .
python scripts/check_cli_docs.py
python -m unittest discover -s tests -v
python evals/evaluate_recall.py
```

`scripts/check_cli_docs.py` reads the real parsers, so a new command, subcommand
or option is checked without registering it. CI fails until the command has:

- `help=` on its declaration and on each of its arguments and options;
- a page in `richi help` that renders and has an example that runs it
  (`DETAILS` in `src/richi/cli_help.py`, or `document()` for launcher commands);
- a guide (`guide=` on the page or its group) whose English and Russian versions
  show the command in code, as `richi COMMAND ...` or a span such as `` `sources show` ``;
- a place in one `richi help` group (top-level commands only);
- an entry in the agent catalog (`capabilities.COMMANDS`) or, with a reason, in
  `capabilities.UNCATALOGED`. A catalog guide is a `NAME.md` skill reference and
  is also the guide of the command's page.

Every `richi ...` line in a shell code block, fenced or indented and also inside
a list item or a block quote, must parse with the current CLI and be accepted
with its global options. Inline code that names a command, with or without
`richi` (`` `richi recall ...` ``, `` `sources show --ref REF` ``), must name a
real command, its real arguments and options, global options that the command
accepts and values from fixed lists; `config set` values are checked as the CLI
checks them, other types and ranges only in shell code blocks. Each later
`richi` command on a line or in a span, after a shell operator such as `|` or
`&&`, is checked too, and `richi help ...` and `capabilities describe ...` must
name existing pages and catalog entries. Only block lines that start with
`richi` are parsed, and a guide shows a command only where it is checked:
`richi` after `cd DIR &&` on a block line, after `python -m` or in a shell
comment is neither checked nor counted. This holds for the Markdown files of
the repository root, `docs/`, `evals/` and `skills/`, and for every guide that
a page links. Dated reports (file names that end in `-YYYY-MM-DD` or `_EVAL`)
record the CLI as it was and are not checked. The docstring of
`scripts/check_cli_docs.py` lists every rule, and the script prints each problem
with the rule it breaks and how to fix it.

A launcher command is declared once, by the launcher; the runtime lists it in
`LAUNCHER_COMMANDS` of `src/richi/cli_parser.py` with its page's summary. A
development runtime formats help with the installed launcher's
`richi_launcher/help_format.py`. List every name of it that the runtime uses in
`NEEDS` of `src/richi/cli_help.py` (a test checks the list), and raise `API`
for a changed behavior or signature. A launcher that lacks a needed name or has
another `API` then gets plain argparse help until the package is reinstalled,
instead of failing.

After source edits, run `python -m pip install --force-reinstall .` before
repeating the full test suite and evaluation. Direct Python imports and package
smoke tests use the installed package; running them against an older installation
can miss regressions in the checkout. Editable installs are unsupported because
the launcher requires a fixed package for release mode and recovery.

For everyday CLI development, configure `development.source` and `dev true` as
described in the [installation guide](docs/INSTALLATION.md). This selects
`src/richi/` for CLI, agents and the map, and links the managed agent skills to
`skills/richi/`. It does not redirect direct Python
imports or load launcher changes from the checkout; changes under `launcher/`
require reinstalling the package.

Keep test data synthetic. Tests should create temporary databases and repositories;
do not point a test suite at a live knowledge store. Runtime configuration and
data live outside the package. See [installation](docs/INSTALLATION.md) and
[migration](docs/MIGRATION.md) for the CLI and compatibility workflow.

The complete agent skill is in `skills/richi/`. Its `references/`
directory is the canonical source of detailed command documentation. The
`docs/CLI.md` index links there; keep command contracts in that one place so
copying the whole skill directory remains sufficient for installation. Links inside
`skills/project-memory/` keep development checkouts usable with earlier launchers.
Codex follows a linked skill directory but skips a linked `SKILL.md` file.
After editing `skills/richi/SKILL.md`, copy it to `skills/project-memory/SKILL.md`
as a regular file. The rename tests check that their contents match.

For retrieval changes, preserve a reproducer and compare relevant cases before
and after. The [evaluation guidance](skills/richi/references/EVALUATION.md)
distinguishes synthetic regression checks from private task-based evaluations.
Do not publish a private snapshot or rewrite old expectations to improve a score.

Keep English `*.md` documents and their complete Russian `*.ru.md` translations
synchronized. Preserve language navigation and cross-links; use the matching
language for local documentation links. Do not translate commands or API
identifiers. `LICENSE.md` remains the original license text.
