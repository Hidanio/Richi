# Contributing to Richi

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

Use macOS or Linux with Python 3.9+ and Git. From the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests
```

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
