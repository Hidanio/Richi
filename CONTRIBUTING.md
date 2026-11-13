# Contributing to Richi

English | [Русский](CONTRIBUTING.ru.md)

Ideas, bug reports, and documentation feedback are welcome. Richi uses the
[Richi Personal Use and Organization Agreement License](LICENSE.md), with
separate authorization for organizational and commercial use.

## Reports and proposals

When an issue tracker is available, describe the problem, expected behavior,
reproduction steps, and relevant version. Otherwise, contact
[hidanjkezor@gmail.com](mailto:hidanjkezor@gmail.com).

Use minimal synthetic examples. Do not include credentials, private knowledge
databases, proprietary project code, personal data, or confidential logs.
Reading public code and submitting a description or proposal does not by itself
require a separate agreement. Running or modifying Richi for an organization
remains subject to the license, even when the purpose is to investigate a bug.

## Code and documentation patches

Contact Richardas Kuchinskas at
[hidanjkezor@gmail.com](mailto:hidanjkezor@gmail.com) before preparing a code or
documentation patch for inclusion. Agree on the scope and applicable contribution
terms first. Third-party patches are accepted for incorporation only after
the required rights have been established in writing.

You retain ownership of your original work. A pull request is not treated as an
automatic transfer of copyright or a general grant allowing the maintainer to
license your work commercially under other terms. Appropriate permission is
needed to distribute the contribution both under the project license and under
separate organizational or commercial agreements.

Contribute only material you have the right to provide. Work created for an
employer or client may require that party's authorization. Identify third-party
material and its license rather than relabeling it as original Richi code.

This policy does not override rights independently granted under applicable law,
hosting-platform terms, existing licenses, or an applicable separate agreement.

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
with its global options. Every inline `` `richi ...` `` must name a real command,
real options, valid values and global options that the command accepts, and
`richi help ...` and `capabilities describe ...` must name existing pages and
catalog entries. This holds for the Markdown files of the repository root,
`docs/`, `evals/` and `skills/`, and for every guide that a page links. Dated
reports (file names that end in `-YYYY-MM-DD` or `_EVAL`) record the CLI as it
was and are not checked. The script prints each problem with the rule it breaks and how to fix
it.

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
`skills/project-memory/`. It does not redirect direct Python
imports or load launcher changes from the checkout; changes under `launcher/`
require reinstalling the package.

Keep test data synthetic. Tests should create temporary databases and repositories;
do not point a test suite at a live knowledge store. Runtime configuration and
data live outside the package. See [installation](docs/INSTALLATION.md) and
[migration](docs/MIGRATION.md) for the CLI and compatibility workflow.

The complete agent skill is in `skills/project-memory/`. Its `references/`
directory is the canonical source of detailed command documentation. The
`docs/CLI.md` index links there; keep command contracts in that one place so
copying the whole skill directory remains sufficient for installation.

For retrieval changes, preserve a reproducer and compare relevant cases before
and after. The [evaluation guidance](skills/project-memory/references/EVALUATION.md)
distinguishes synthetic regression checks from private task-based evaluations.
Do not publish a private snapshot or rewrite old expectations to improve a score.

Keep English `*.md` documents and their complete Russian `*.ru.md` translations
synchronized. Preserve language navigation and cross-links; use the matching
language for local documentation links. Do not translate commands or API
identifiers. `LICENSE.md` remains the original license text.
