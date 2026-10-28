# Capture and attach the code actually checked

English | [Русский](GIT_CAPTURE.ru.md)

Capture creates evidence, not a knowledge assertion or verification-date update.
Use the selected workspace and repository membership guard from [SKILL.md](../SKILL.md).
The registered project must have `repo_path`; all examples are synthetic.

```sh
richi project check /work/example --id service-example
richi --compact-json --project-path /work/example sources capture \
  --project service-example --path internal/cache.go --output /tmp/cache-source-01.json
# For committed bytes, add --rev HEAD (or a local tag, branch or SHA).
richi --compact-json --project-path /work/example entry get task:EXAMPLE-42
richi --compact-json --project-path /work/example sources attach \
  --ref entry:task:EXAMPLE-42 --json /tmp/cache-source-01.json \
  --expected-updated-at 'UPDATED_AT_FROM_GET'
```

## Checkout and version

For `sources capture` selection order is explicit `--repo`, Git root of the
validated `--project-path`, then registered `repo_path`. The shell cwd alone
never selects a checkout. `--project-path` still checks workspace membership;
`--repo` must share the registered repository's Git common directory (linked
worktrees are allowed, a separate clone is not automatically the same repo).
`--path` is relative to the selected Git root, even when project-path is a subdirectory.

Inspect `capture_context.repo_root`, `selected_by` (`repo`, `project_path`,
`registered`) and `resolved_commit` before attaching: they must describe the
checkout/version actually examined. `--output` creates a new file containing
only `source`; context stays in stdout and existing output files are never overwritten.

`--rev` captures committed bytes. The default / `--worktree` captures actual
working bytes, including untracked changes, into `artifacts/git/<sha256>.blob`
beside the database. Its base commit is HEAD; `dirty` compares this file to that
commit. Uncommitted bytes are not the content of a commit. Unborn repositories
without HEAD are unsupported; a deleted file can be captured with an older `--rev`.

The generated version-1 `git` anchor has `repo_id`, `path`, full `commit`, `blob`,
`mode`, `dirty`, plus `snapshot` for worktree evidence. Blob belongs to the base
commit and is null for a new file. `revision`, `sha256`, `observed_at` are filled
from actual evidence. Older sources without `git` remain valid and are not
silently attached to current HEAD; no SQLite migration is needed.

## Attach without replacing the record

For a new entry/entity/edge, include the generated object in its `sources`.
For an existing record, `sources attach` appends while preserving fields,
projects, existing sources and history. It requires a fresh `updated_at`, even
when the source was already attached, and verifies stored bytes are available.
On conflict reread/reconcile; then verify the full record. Do not attach during
a draft-only or read-only task. `--json` here takes one source file, not stdin.

Git operations use local objects without checkout, index changes or fetch.
Capture supports regular files up to 8 MiB, not symlinks, submodules or paths
outside the repo. Partial/promisor repositories are rejected to avoid lazy fetch.
Keep artifacts outside source checkouts; SQLite backup alone does not include
`artifacts/git/` or repository objects required to reopen historical evidence.

For historical browsing, `git-check`, diff and related knowledge read [GIT.md](GIT.md).
Capture's project-path selection is specific to capture: Git-reading commands
such as `git-check`, `diff` and `impact` require explicit `--repo` to select a
linked worktree. `sources related` searches stored anchors only, chooses no
checkout and accepts no `--repo`.
