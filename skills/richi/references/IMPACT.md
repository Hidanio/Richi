<a id="знания-для-изменённых-файлов"></a>

# Knowledge for changed files

English | [Русский](IMPACT.ru.md)

`sources impact` assembles bounded context from direct source bindings
to changed paths. It retrieves previously recorded experience for a selected change.
A matching card means its source is linked to a file, not proof
that the change affects all conclusions in the card or behavior of neighboring services.

<a id="запуск"></a>

## Running

Use the installed `richi` command:

```sh
richi sources impact --project service-example \
  --base BASE_COMMIT --target HEAD --limit 12 --max-chars 16000

richi sources impact --project service-example \
  --base HEAD --worktree --limit 12 --max-chars 16000
```

`--base` is required. Commit mode compares exactly the two specified trees:
the command does not choose a merge base or imply the history of the entire branch.
Both revisions are resolved to full SHAs before retrieval. To compare a branch
with its branching point, first explicitly identify the appropriate base commit.

`--worktree` compares the base version with current files, including staged
and unstaged changes and non-ignored untracked files. The working copy
may change during reading; the report is not an atomic snapshot of it.
`--repo` accepts the registered checkout or a linked worktree.
Git runs locally: without fetch, checkout or index changes.

In the pilot, working-copy mode rejects Git repositories with configured
external `clean`/`process` filters: ordinary `git diff` can execute them
even with external diff and textconv disabled. The command does not disable user
settings. Comparing two commits remains available for such a repository.

<a id="что-попадает-в-контекст"></a>

## What enters the context

- Structured `source.git`: exact match of repository ID and path.
- Legacy `git:FULL_SHA:path`: the path is matched against changes, and the repository
  is checked using the exact commit and projects explicitly linked to the card.
- Legacy `git:FULL_SHA`: paths actually changed by this commit are used.
  For a merge, the first parent is used. A commit appearing somewhere in a card
  does not make every file in that version a knowledge source.
- A rename is represented as deletion of the old path and addition of the new one.
  This preserves retrieval for both paths without claiming semantic identity.

The pilot includes entries, entities and explicit edges. States confirmed,
hypothesis and superseded are shown explicitly: an older version or hypothesis may
help explain history but does not become a current confirmed contract.
Cards are deduplicated by ref, while specific sources and paths remain in match
reasons. Text excerpts come from stored text; they do not replace
the full card. Before drawing a conclusion, read it through the indicated `read_more` command.

Confirmed records appear first. Within a state, direct file
bindings have higher priority, followed by a source-version match with the selected
target or base. The reason appears in `match_reasons`; ordering is not an assessment
of truth or semantic impact. Under a tight budget, diagnostic
details and excerpts are shortened first, then additional sources and cards
with the weakest signals. Omissions remain visible in counters.

The pilot does not expand results through the graph automatically. If needed,
read `graph neighbors REF --depth 1` for a selected card. Shared tags,
file names in another repository and textual mentions in summary do not
create a direct relationship with the change.

<a id="покрытие-и-дальнейшая-проверка"></a>

## Coverage and further checks

The report contains resolved versions, changed paths, matching cards,
match reasons, limits and omissions. Check coverage, budget and
truncated. No matches under incomplete coverage do not prove absence
of knowledge. Even complete retrieval shows only relationships already expressed by sources;
`recall` remains available for a broader question.

The output-size limit covers the entire JSON. A large change list, an unavailable
or ambiguous repository and an exhausted budget must remain visible
limitations. Do not write truncated cards back to the database.

The target retrieval time is 30 seconds, checked between operations; this is not
a hard process-duration limit. Individual Git calls have their own
timeouts. Incomplete SHAs and legacy links without explicit repository scope remain
unresolved and are counted in coverage.

Context selection does not verify the truth of conclusions, current dependency state
or release status, and does not reread the bytes of every stored file version. For a selected
source, separate `sources show/history/diff`
and `sources git-check --ref REF --source N` operations are available. For a legacy commit-only link,
first run `sources commit --ref REF --source N`, then select
`--path` from the list of available changed files. Checking does not update sources,
`verified_at`, `knowledge_state` or `work_state`.

All runs are explicit. Ordinary recall and punisher do not run this command or
scan repositories in the background. Usefulness comparisons use a fixed
set of changes: expected refs, false matches, completeness and context volume,
call count and local command duration. This does not measure token savings or
the quality of all future agent decisions.
