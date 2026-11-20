<a id="контекст-инженерной-задачи"></a>

# Engineering task context

English | [Русский](BRIEF.ru.md)

`brief` assembles one bounded JSON response from a query, an optional selected
code change and stored relationships. This is an explicit read of shared memory.

```sh
richi brief 'почему выбрали этот timeout' \
  --project service-example --limit 8 --max-chars 16000

richi brief 'ABC-123: изменение timeout' \
  --project service-example --base BASE_COMMIT --target HEAD

richi brief 'проверка текущих изменений' \
  --project service-example --base HEAD --worktree

richi brief 'применим ли прежний опыт работы с кешем' \
  --project service-example --check-sources --target HEAD

richi brief 'какой опыт учесть перед изменением кеша' \
  --check-sources --check-limit 12
```

`--base` enables Git selection and requires `--project`, the ID of a registered
repository. `--check-sources` independently enables checks of stored files
for selected cards. Without either flag, Git is not invoked; ordinary brief retains
its previous behavior.

`--target`, `--worktree` and `--repo` are allowed with `--base` or `--check-sources`.
Without `--base`, explicitly supplying any of these parameters requires `--project`.
`--target` and `--worktree` are mutually exclusive. `--repo` selects a linked worktree
of the registered repository according to [GIT.md](GIT.md).
Exact endpoints are compared; there is no automatic merge-base. Working-tree diff
selection follows the restrictions in [IMPACT.md](IMPACT.md), including
rejection of external clean/process filters. SQLite is read in one transaction;
the Git working copy remains mutable during the read.

<a id="откуда-берутся-карточки"></a>

## Where cards come from

1. The query uses existing `recall`: the same lexical rules, exact Jira ID
   protection, handling of ambiguous concepts and one hop along an explicit relationship.
   The project raises relevance rather than isolating knowledge from other services.
2. The specified diff uses `sources impact`. Matches are determined by sources
   for changed files, including supported legacy `git:FULL_SHA[:path]` links.
3. The first three direct entry/entity matches from the diff are checked for one hop
   along explicit sourced relationships. Relationship cards with `edge:` refs do not become
   traversal nodes. Project membership does not expand into all its records. Neighbors
   found only through the graph do not initiate another hop.

The query and diff channels are independent: for example, an experiment without a Jira ID
may enter through a stored relationship with a task found by diff files. This does not weaken
the Jira filter of ordinary recall. Relationship type and direction remain visible; a hop
does not prove causal impact or applicability of past experience to new code.

By default, both channels and relationships use only `confirmed`. For historical
or unresolved material, explicitly add `--include-superseded` or
`--include-hypotheses`. These states are preserved in the response. `work_state` is also
passed through without promotion: merged does not mean released.

<a id="как-читать-результат"></a>

## Reading the result

- `results` are deduplicated by stable `ref`. `selected_by` shows `query`,
  `code_change` or `explicit_relation`; additional reasons appear in
  `query_match_reasons` and `code_match_reasons`.
- `matches` contains the exact index in the original sources array, SHA and matching
  paths. `connections` preserves the edge ref, direction, description, sources and
  `anchor_ref`. A related result is retained together with at least one
  card from which it was obtained; the full edge has a separate `read_more`.
- `summary_excerpt` contains verbatim excerpts. Read `is_excerpt` and omission
  counters. Open the full card through `read_more` before drawing a conclusion
  or applying a procedure. Truncated responses must not be written back.
- `comparison` records the full SHAs of selected diff endpoints.
  Selection itself does not compare each source's bytes with the target. For that check,
  add `--check-sources` or use
  `sources git-check --ref REF --source N` for a specific piece of evidence.
- `coverage` separates search completeness, diff-source inspection and the limit
  on related candidates. `coverage_complete` refers to these bounded
  mechanisms, not completeness of all system knowledge or dependencies.

Exact query matches are protected first, then query and file channels alternate.
Up to two slots may go to related cards; they do not displace
exact IDs. Ranks from different mechanisms are not added together. Without a diff,
ordinary recall selection is preserved until the shared budget is applied.

The result limit is 1–50, and the budget is 4000–100000 characters for the entire JSON,
including the final newline. Defaults are 8 cards and 16000 characters. Excerpts
and secondary evidence are shortened first, then whole cards and relationships
left without their anchor card are removed. Full refs, SHAs and sources are not truncated.
An unusually large mandatory response header may require a larger budget.

`candidate_count` is the number of unique cards reaching assembly; candidates
outside internal shortlists are separately reflected in `coverage`. `selection_omitted`
shows omissions caused by the card limit; `budget.omitted_results` shows subsequent
reductions due to size. `no_match` becomes true only when there are no
matches and all declared coverage is complete. An empty response with omissions does not mean
that relevant experience does not exist.

<a id="проверка-источников-выбранных-карточек"></a>

## Checking sources of selected cards

`--check-sources` compares stored Git-source bytes with the selected target.
First, ordinary brief selects cards and fits context into the budget; then
checking reads the full `sources` arrays of only those cards. Source truncation
in an excerpt does not change their indices or restrict checking to that excerpt.
Other nodes and relationship sources shown only inside `connections`
do not become an additional set of checks.

`--check-limit` specifies the number of Git-check attempts: the default is 12, and the allowed range is
1–50. It can be explicitly supplied only with `--check-sources`. An unavailable repository
or invalid anchor consumes an attempt; ordinary URLs, commit-only links
without a path and sources outside the selected repository do not. This is a bounded
check of selected context, not a scan of the whole database.

With `--project ID`, sources from that repository are checked. Cards from neighboring
projects may still enter through recall, where the project only raises
relevance; their sources from other repositories are marked unchecked.
Without `--project` or explicit target parameters, each identified repository is compared
with its own local HEAD. Combining `--base ... --check-sources` uses
the already resolved SHA of the diff target. Branches and HEAD are resolved to exact SHAs
for checking; no network refresh is performed.

Structured `source.git` and legacy file links
`git:FULL_SHA:path` are supported. For a legacy link, the repository must be unambiguously determined
from the card's stored scope; the current `--project` does not create new scope for it.
If a legacy link contains `sha256`, it must match the file at the stored
commit; a mismatch yields `unavailable`. A missing historical hash is separately
reported in the result.
A `git:FULL_SHA` link without a path remains unchecked: first select a file through
`sources commit`, then run a separate `sources git-check --ref REF
--source N --path FILE`. Checking does not attach new anchors to the record.

`source_checks` in each card contains source, check and omission
counts, `not_checked_reasons`, a `results` array and `results_omitted`.
Results use `unchanged`, `changed`, `deleted` or `unavailable`;
the number of unchecked Git sources is grouped by reason in
`not_checked_reasons`. Ordinary sources are counted separately in
`non_git_source_count` and do not count as Git checks. In `results`,
`source_index` is one-based within the full original array, including ordinary URLs.
`record_updated_at` binds the indices to the card version that was read.
The card's `read_more` opens the full record, while a result's `read_more` repeats
the check of a specific source. After the card changes, reread
the record and verify the index before a separate check.

`source_check_coverage` separately describes byte-check coverage.
Its `complete` does not become true when a source is unavailable, a Git source
is unchecked or results are omitted. It does not replace knowledge-selection `coverage`:
an empty search and incomplete source checking mean different things.
`resolved_targets` contains the full SHA and comparison mode for each repository
read; `counts` separates results by status.

Checks share a 20-second time limit for Git operations and the existing overall
brief JSON budget. Limit and timeout stopping reasons are reflected in coverage.
During packing, check details are removed first; `results_omitted` shows
omitted results even if they count toward completed checks.
A tight budget does not turn an omission into a successful check.

`--worktree` reads actual working-copy bytes. A change of HEAD by the end of checking
yields `unavailable`; an unchanged HEAD does not guarantee that files stayed unchanged during
the read. This is not an atomic snapshot of the entire working tree. For a stored
uncommitted source, its captured snapshot remains the original side.

`changed`, `deleted`, unavailable and unchecked Git sources add a
`needs_recheck` reason to the response. `unchanged` does not clear existing recheck reasons.
Changed bytes are a reason to revisit a conclusion's applicability; unchanged bytes
do not prove knowledge truth, dependency state or release status. Checking
does not promote `verified_at`, `knowledge_state` or `work_state` and does not replace
substantive reading of a source.

The command does not write memory, update `verified_at`, run models,
procedures, fetch or external URL reads. It combines available evidence;
the agent assesses semantic applicability within the current task.
