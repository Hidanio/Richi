<a id="git-происхождение-знаний-и-история-кода"></a>
# Git: knowledge provenance and code history

English | [Русский](GIT.ru.md)

Richi ties a knowledge source to a repository, path, full commit SHA, Git blob
and SHA-256 of the bytes actually read. Commands use local Git and Python 3.9+
without additional libraries. They do not switch branches, fetch, or change
working files or the index.

<a id="сохранить-проверенную-версию"></a>
## Save a verified version

Byte capture, checkout selection and safe source attachment are described in
[GIT_CAPTURE.md](GIT_CAPTURE.md). For the Git reads below, select a linked worktree
with an explicit `--repo`: `--project-path` alone validates membership but does
not select their checkout. `sources related` reads saved anchors, does not select
a checkout and does not accept `--repo`.

<a id="открыть-код-историю-и-сравнение"></a>
## Open code, history and comparisons

```sh
richi sources show --ref entry:task:ABC-123 --source 2
richi sources history --ref entry:task:ABC-123 --source 2 --limit 20
richi sources history --ref entry:task:ABC-123 --source 2 --target HEAD
richi sources diff --ref entry:task:ABC-123 --source 2 --target main
richi sources diff --ref entry:task:ABC-123 --source 2 --worktree
```

`--source` is a one-based position in the card's **full** `sources` array; the
default is 1. `entry:`, `entity:` and `edge:` are supported. Instead of a card,
you can pass `--json /tmp/cache-source-01.json` immediately after capture.

The CLI and map share source viewing. Legacy `git:FULL_SHA:path` references open
through the same commands with `--ref`. For a commit-only reference, first list
changed files, then select an available file:

```sh
richi sources commit --ref entry:task:ABC-123 --source 1
richi sources show --ref entry:task:ABC-123 --source 1 --path internal/cache.go
richi sources history --ref entry:task:ABC-123 --source 1 --path internal/cache.go
richi sources diff --ref entry:task:ABC-123 --source 1 --path internal/cache.go --target HEAD
richi sources git-check --ref entry:task:ABC-123 --source 1 --path internal/cache.go --target HEAD
```

`--path` is allowed only to select a file from a legacy whole-commit reference.
It cannot change the path of a file-specific source. `sources commit` accepts
only such a reference in an existing card. Legacy references require the card's
explicit project scope; a SHA alone without that scope in arbitrary JSON is
insufficient. Unavailable or ambiguous repositories return an error. A Git
anchor computed while reading is not saved to memory and does not replace the
old observation date.

`show` returns saved bytes even if the file has changed or been deleted from the
current branch. Text is UTF-8; binary data is base64. By default, `history` ends
at the saved commit; `--target HEAD` builds the saved path's history through local
HEAD. HEAD may lie on a different branch of history. Forward renames and later
changes at the new path are not followed automatically: verify the new path first
and capture it separately if needed. History includes SHA, parents, author date
and commit subject. Git `--follow` heuristically follows renames backward; it is
not a complete semantic history analysis. A shallow clone has incomplete history,
which the response indicates.

`diff` compares saved code with an explicitly selected local ref or current
working-tree bytes. The response always includes the resolved target's full SHA.
Binary diff reports a difference without a text patch. Git history describes
code; read knowledge history with `get --history`.

<a id="проверить-применимость-и-найти-связанные-знания"></a>
## Check applicability and find related knowledge

```sh
richi sources git-check --ref entry:task:ABC-123 --target HEAD
richi sources git-check --project service-example --worktree
richi sources related --project service-example --path internal/cache.go
richi sources related --project service-example \
  --path internal/cache.go --commit FULL_COMMIT_SHA
```

Without `--source`, `git-check` checks a group of **structured** `source.git`
anchors. Legacy references do not yet participate in that group check. With
`--source N`, exactly one `--ref` is required; the selected source of any supported
format is checked and its output appears in `result`, as in the map. A
commit-only reference also requires `--path`.

For each source, group `git-check` reports `unchanged`, `changed`, `deleted` or
`unavailable`. Overall status is `unchanged`, `changed`, `unavailable` or
`no_git_sources`; deletions produce overall `changed`, incomplete coverage
produces `unavailable`. The result also includes the target commit's relation
to the saved commit: `same`, `ahead`, `behind`, `diverged` or `unknown`. When the
path is absent in the target commit, possible renames are returned separately;
do not automatically substitute them for the source.

Here, `--project` selects **sources with this `git.repo_id`** in all knowledge
states, including cards from other projects and edges. This differs from
`punisher` project scope and project priority in `recall`. `related` finds exact
repository/path matches, optionally also matching the full SHA. This is reverse
“code → knowledge” lookup; it does not infer semantic dependencies for every result.

`related` also reads structured anchors. To select context for changed files,
including supported legacy Git references, use
`sources impact --project ID --base BASE --target HEAD` or `--worktree`.
Comparison semantics, knowledge states and limitations are in [IMPACT.md](IMPACT.md).

To check sources while selecting context, use
`brief 'вопрос' --project ID --check-sources --target HEAD`.
It checks full source arrays only for selected cards: up to 12 attempts by
default, including structured anchors and supported file references in
`git:FULL_SHA:path` form. A commit-only reference requires a separate file
selection. `--project` restricts the repository being checked while retaining
normal project priority in recall. Diff-based retrieval (`--base`) and checking
can be enabled independently; together they use one target SHA. Results,
omissions, time limits and shared budget are described in [BRIEF.md](BRIEF.md).
Ordinary `brief` without these flags does not invoke Git.

`recall` and `entry read` return Git metadata with selected sources. If truncated,
retrieve the full card. `punisher` validates saved Git metadata and suggests
`sources git-check` for cards containing those sources; an ordinary run does
not scan repositories. Changed bytes are a reason to recheck the conclusion.
Matching bytes do not prove unchanged dependencies or release state. No check
changes `verified_at`, `knowledge_state` or `work_state`.

`sources snapshot/check` retains its original manifest semantics: current cards,
edges and local files through `reference`. It preserves nested Git metadata,
but historical-code comparison specifically uses `git-check`.

<a id="границы-и-перенос-данных"></a>
## Boundaries and data transfer

- The repository ID is its registered `project.id`. `--repo` permits a linked
  worktree sharing the same Git common directory. A separate clone is not
  automatically accepted as the same repository. When moving the canonical
  checkout, explicitly update the full project object through `project upsert`.
- Branches and `origin/main` resolve locally only. A missing ref/object stays
  unavailable; no network refresh occurs. Partial/promisor repositories are
  rejected to prevent a read from initiating a hidden lazy fetch.
- Regular files up to 8 MiB are checked; symlinks, submodules, directories and
  paths outside the repository are unsupported. Text diffs are limited to 1 MiB
  per side and 20 000 lines. Output is bounded by `--max-chars` (2000–100000);
  inspect `budget`, `truncated`, `coverage` and omission counts.
- Project lookup visits up to 5000 cards and selects up to 500 sources; specific
  `--ref` values are preferable for precise checks. History is limited to 100 commits.
- `backup` copies SQLite. For a complete move, also preserve `artifacts/git/` and
  required repository Git objects: the database does not automatically contain
  them. Snapshots are hash-checked and never overwritten; there is no automatic deletion.

<a id="кнопки-в-интерактивной-карте"></a>
## Buttons in the interactive map

Open the map with `richi map`. In a task, concept or explicit-edge card, a source
containing `git` has **Код** (Code), **История** (History), **Diff** and
**Проверить** (Check) buttons. Previously saved `git:<полный SHA>:<путь>` references
also open through these buttons. A `git:<полный SHA>` reference has a
**Файлы коммита** (Commit files) button: select a changed file, then open its
code, history or comparison.

For a legacy reference, repositories are checked only among projects explicitly
linked to the card. The commit must exist in exactly one suitable repository;
absence or ambiguity is reported as an error. Content and hashes are computed
when opening the specified historical commit. This temporarily resolves the
reference for viewing; it neither changes the record nor verifies its conclusions.
SQLite dates, states and sources are preserved. Arbitrary references, short SHAs
and paths without a version are not automatically anchored to HEAD.

For a merge commit, changes are listed against the first parent; for an initial
commit, against the empty tree. Deleted files are marked unavailable in the
commit itself. The list is bounded and possible truncation is reported.

Code and results open in a separate window. Comparison accepts a local branch,
tag or SHA, or the working tree. A check applies to one selected source. Full
SHAs, the uncommitted-snapshot indicator, output limits and source unavailability
appear in that same window.

If another task changed the card after the map loaded, the server rejects a
request for the old version: refresh the map and select the source again. This
prevents a saved source position from redirecting to a different file. Git is
read only on a click; the map still does not write knowledge, fetch or switch branches.

The HTTP API accepts an existing card reference, its source position and card
version. For a whole-commit reference, it additionally allows selection only of
a regular file from the commit's change list. Arbitrary paths, source JSON,
capture and attach are unavailable from the browser. The API uses the same
loopback server with Host/Origin checks, a response-size limit and one Git
operation at a time.

The launcher checks Git-interface support. If an older server is still running,
the new version starts on the next free port; the old process is not forcibly
terminated. Run `richi map` again to open the updated map. An interactive CLI
menu remains a later step.
