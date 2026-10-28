<a id="стабильные-релизы-github"></a>
# Stable GitHub releases

English | [Русский](RELEASING.ru.md)

Richi's updater reads stable releases from [Hidanio/Richi](https://github.com/Hidanio/Richi/releases).
It accepts strict `X.Y.Z` versions and `vX.Y.Z` tags. Drafts and prereleases are
excluded. There is no PyPI publication or separate update server.

To prepare a release, update both `[project].version` in `pyproject.toml` and
`__version__` in `src/richi/__init__.py`, review and merge the change, then push
the matching `vX.Y.Z` tag. Pushing a tag starts `.github/workflows/release.yml`.
The workflow builds one wheel and tests that artifact on Python 3.9 and 3.12,
on Linux and macOS, including the public recall evaluation. Only after every
matrix job passes does it create a GitHub draft with the wheel, manifest and installer
attached, then publish it. A failed upload leaves a draft, which is invisible
to the updater; inspect or remove that draft before retrying publication.
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

Compatibility fields describe this runtime and must be reviewed when Python,
SQLite schema or map requirements change. New manifest formats or updater
protocols require an explicit launcher upgrade path. The client enforces a
50 MiB wheel limit and verifies the checksum and metadata before installation.
Downloads start at fixed repository URLs and follow redirects only to GitHub's
release asset hosts. No manifest field can select a download host or command.
SHA256 detects corruption; publisher authenticity relies on GitHub HTTPS and
control of the official repository. Protect release tags and repository access.

The wheel bundles the entire canonical `skills/project-memory/` directory.
`build_installer.py` creates a deterministic stdlib-only `richi-installer.pyz`
from that wheel, with a SHA256 sidecar. The workflow publishes those files and
`install.sh` alongside the wheel and manifest. Each release test job installs
the exact archive and wheel into a synthetic home without accessing personal
data. The public one-command URL works only after this release is published.
