<a id="стабильные-релизы-github"></a>
# Stable GitHub releases

English | [Русский](RELEASING.ru.md)

Richi's updater reads stable releases from [Hidanio/Richi](https://github.com/Hidanio/Richi/releases).
It accepts strict `X.Y.Z` versions and `vX.Y.Z` tags. Drafts and prereleases are
excluded. There is no PyPI publication or separate update server.

To prepare a release, update both `[project].version` in `pyproject.toml` and
`__version__` in `src/richi/__init__.py`, review and merge the change, then push
the matching `vX.Y.Z` tag. Pushing a tag starts `.github/workflows/release.yml`.
The workflow builds one wheel and tests that artifact on Python 3.9, 3.12,
3.13 and 3.14 on Linux and macOS, including the public recall evaluation.
Only after every matrix job passes does it create a GitHub draft with the
wheel, manifest and installer attached, then publish it. A failed upload leaves
a draft, which is invisible to the updater; inspect or remove that draft before
retrying publication.
Published versions must not be replaced: corrections get a new version and tag.

Do not publish an empty release with GitHub's **New release** button while the
workflow is running: the updater can see it before the manifest and wheel exist,
and the final `gh release create` step expects to create the release itself.
Push only the tag and let the workflow publish the tested artifacts.
The `v1.0.0` tag was published from code reporting `0.1.0`; its validation failed.
The corrected version is `1.0.1`, with a new tag on the version-fix commit.
Keep the old tags intact. After merging the fix and pulling `main`, verify and
push only the intended tag:

```sh
git switch main
git pull --ff-only origin main
python -m build --wheel --outdir dist_release
python scripts/release_manifest.py dist_release/richi-1.0.1-py3-none-any.whl --tag v1.0.1 --output dist_release/richi-release.json
git tag v1.0.1
git push origin refs/tags/v1.0.1
```

Run the tag commands only after the build and validation succeed. Wait for all
release jobs, including publication, to succeed, then run `richi update check`.
Expect version `1.0.1` and `can_apply: true` on a compatible older installation.
If any release job fails, inspect its logs before sharing the installer URL.

The workflow uses `contents: read` by default. Only the final publication job
receives `contents: write`, through its built-in `GH_TOKEN`; it does not run
checkout code. The draft and publish sequence follows the [GitHub CLI release
commands](https://cli.github.com/manual/gh_release_create).

For local preparation without publishing:

```sh
python -m build --wheel
python scripts/release_manifest.py dist/richi-1.0.1-py3-none-any.whl --tag v1.0.1
python scripts/build_installer.py dist/richi-1.0.1-py3-none-any.whl
python tests/installer_release_smoke.py dist
```

`scripts/release_manifest.py` checks the wheel's filename, package metadata,
embedded source version, source checkout version and optional tag before
writing deterministic `dist/richi-release.json`. The manifest contains only
public compatibility metadata plus the wheel's filename, SHA256 and byte size:

```json
{
  "format_version": 1,
  "version": "1.0.1",
  "updater_protocol": 1,
  "python_min": [3, 9],
  "database_schemas": [1, 2],
  "map_schema": 2,
  "wheel": {
    "filename": "richi-1.0.1-py3-none-any.whl",
    "sha256": "<64 lowercase hexadecimal digits>",
    "size": 123456
  }
}
```

`python_min` must be reviewed when Python requirements change. The other
compatibility fields are frozen; see the release contract below. The client enforces a
50 MiB wheel limit and verifies the checksum and metadata before installation.
Downloads start at fixed repository URLs and follow redirects only to GitHub's
release asset hosts. No manifest field can select a download host or command.
SHA256 detects corruption; publisher authenticity relies on GitHub HTTPS and
control of the official repository. Protect release tags and repository access.

<a id="protocol-1-release-contract"></a>
## Protocol-1 release contract

Installed 1.0.1 updaters are frozen code, and they install only what they accept.
Every protocol-1 release keeps them able to update in place:

- The manifest declares `database_schemas: [1, 2]` and `map_schema: 2`, always.
  `scripts/release_manifest.py` takes both from
  `installation.PROTOCOL1_DATABASE_SCHEMAS` and `PROTOCOL1_MAP_SCHEMA`; never change
  them. Real support is what the package reports in its self-check.
- `richi.memory.VERSION` and `richi.graph.MAP_SCHEMA_VERSION` stay 2: 1.0.1 compares
  `memory.VERSION` with `map_schema`. Storage code uses `memory.SCHEMA`.
- `memory.SCHEMA` stays a top-level integer literal (the bootstrap parses it for
  generations installed by 1.0.1), and `SUPPORTED_VERSIONS` is every schema from 1
  to `SCHEMA`. Never drop a schema: 1.0.1 installs without checking support, and
  its rollback has no schema checks. A schema bump also extends `migrate`
  (`graph.MIGRATION_TARGET`); `tests/test_tool_schema.py` enforces these rules.
- Changing a manifest field or format, the map wire schema or the updater protocol
  needs a new protocol. Publish its manifest as an additional asset (for example
  `richi-release-2.json`): protocol-1 updaters select `richi-release.json` by exact
  name and ignore other assets. Keep publishing a protocol-1 manifest while
  protocol-1 installations must update.
- A Codex-only agent setup record (format 1) keeps exactly the fields 1.0.1 writes,
  with adapter 1 implied. After a rollback, 1.0.1 plans its own record again and
  reports any other field as drift, so `setup --status` would say `repair_required`.
- `tests/update_matrix_e2e.py` updates every published release to the candidate in
  place. Its sources are the stable `vX.Y.Z` tags from `v1.0.1` below the candidate's
  version, so a new tag joins the matrix without edits. Each source is built from its
  tag with its own release scripts and installed with its own installer next to
  Codex, Claude Code where it supports it, a workspace with graph records, a bound
  chat and a running map. For every source it runs a manual update, rollbacks by
  either manager through the original console, an update again, and an automatic
  installation; one more run updates the oldest release through every intermediate
  one. After each step the expected release must be active and every connected agent
  configured in `setup --status`, with the chat binding, recall/search/read results
  and store schema unchanged. A map started on the source keeps its URL and serves
  whatever each update to the candidate and each rollback activates, without manual
  steps: managers up to 1.1.0 cannot hand maps off, so the candidate's maps follow by
  themselves and the user's next terminal command replaces older maps, while commands
  of agents and scripts leave them alone. The test and release workflows run it
  before publication. Locally it needs a full clone with tags, the `build` package and
  the candidate installed; `--from vX.Y.Z` runs one source.

The wheel bundles the entire canonical `skills/project-memory/` directory.
`build_installer.py` creates a deterministic stdlib-only `richi-installer.pyz`
from that wheel, with a SHA256 sidecar. The workflow publishes those files and
`install.sh` alongside the wheel and manifest. Each release test job installs
the exact archive and wheel into a synthetic home without accessing personal
data. The public one-command URL works only after this release is published.
