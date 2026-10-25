# Exact metadata operations

Use the bound workspace and the usual `--project-path PATH` guard. These read-only
operations execute inside SQLite and never require building the optional index.
Use them for stored metadata questions, not for conclusions about record bodies.

```sh
richi --compact-json query count --project example --where "kind = 'decision'"
richi --compact-json query exists --project example --where "jira_key = 'DEMO-42'"
richi --compact-json query facets --project example --by kind --by work_state
richi --compact-json query select --project example \
  --fields ref,title,work_state,verified_at --limit 50
```

`count` and `exists` cover **all** matching current records, with no lexical or
FTS shortlist. `exact: true` refers to that stored scope, not external reality.
They do not read summaries, sources, revision history or Git artifacts. `exists`
stops after the first match. Use it to check metadata membership; a failed
lexical search still cannot prove that work never occurred.

`--project` is **strict** for every `query` operation. This differs from the
ranking boost on recall/discover. `--where` shares the bounded composed grammar
and project-provenance/date/NULL semantics in [FILTERS.md](FILTERS.md); it combines
with the project and state selectors. Default family is `entry`; use
`--family entity` or `--family all`. Entities have NULL work_state and jira_key.
Default knowledge state is confirmed; include hypotheses/superseded deliberately
with their separate flags. A filter cannot override these state gates. Schema-1
stores support entries; all uses available families, entity requires schema 2.
Every response states its actual scope.

`facets --by FIELD` returns parallel `columns` and `rows`, grouped by one to three
distinct fields: family, kind, knowledge_state, work_state, jira_key. Repeat
`--by` to form combined groups. Counts are exact for each returned group. NULL
is a distinct group, not an empty string. Groups sort by descending count, then
binary field order; `--limit` (1–200, default 20) and the character budget bound
the returned groups. `has_more: true` means omitted groups; their counts are not
zero. Narrow the scope or increase limits if all groups are needed. A group that
cannot fit the JSON budget is omitted whole, even when it is the first group;
`has_more` remains true. Keys above the separate hydration cap (four times
max-chars in UTF-8 bytes) fail explicitly before loading into Python. Narrow
the predicate or increase the budget. This operation has no pagination cursor.

`select` projects only requested scalar fields: id, kind, title, knowledge_state,
work_state, jira_key, created_at, updated_at, verified_at. `ref` is always first;
default fields are ref,kind,title,knowledge_state,work_state,verified_at. The
compact table contains `columns` and aligned `rows`; fields are never silently
truncated. Full records, arrays and arbitrary SQL expressions are not projections.
Use `fetch --ref REF` for full evidence, including sources and caveats.

Pages sort by full qualified ref in binary order. `--limit` is 1–200 (default 20).
While `has_more` is true, repeat the **same scope and fields** with
`--after next_after`. An `oversized` item is a matching record whose requested
metadata could not fit; it carries an exact fetch pointer and counts as consumed
for continuation. It is not a missing record. Collect rows and oversized outcomes
when enumerating a scope. No row count is implied by a page's `returned` value.
A page uses one read snapshot; each continuation opens a new one. Concurrent
writes can change the enumeration. Reread before updating and use the exact
current version for optimistic concurrency.

Every operation accepts `--max-chars` 2000–100000 (default 8000). The budget covers
actual JSON including the final newline; `--compact-json` fits more metadata.
The SQL work itself can scan matching records: grouping, arbitrary LIKE, array
membership and date predicates are not universally indexed. Projection avoids
loading bodies; keyset pagination uses native ID ranges. SQLite may choose a
filter index plus sorting instead of the primary-key index. No retention,
automatic deletion, schema migration or live index construction occurs on reads.
