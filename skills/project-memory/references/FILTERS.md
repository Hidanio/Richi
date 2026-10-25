# Composed filters and indexed retrieval

Use `--where` on `discover`, `recall` or `search` when the question requires a strict scope.
The query still supplies the lexical anchor. For complete metadata operations without
a lexical shortlist, use [QUERY_TOOLS.md](QUERY_TOOLS.md). All existing state, provenance,
result-count and character-budget rules apply.

```sh
richi --compact-json recall 'connection pooling' \
  --where "project = 'example' AND kind IN ('decision', 'experiment') AND NOT work_state = 'planned'"
richi --compact-json recall 'cache' \
  --where "(tags HAS 'performance' OR aliases HAS 'local cache') AND verified_at IS NOT NULL"
richi search 'cache' --where "family = 'entry' AND title LIKE 'Cache%'"
```

`--project` on recall remains a ranking boost. `project = 'example'` inside
`--where` is mandatory membership. The filter runs **before** the FTS shortlist,
before payload loading and on graph additions; unrelated graph nodes cannot
escape it. A scoped result only describes that scope, including any reported
concept ambiguity. `search` returns entries only; recall also searches entities.

Supported operations:

| Operation | Meaning |
|---|---|
| `AND`, `OR`, `NOT`, parentheses | Precedence: NOT, then AND, then OR |
| `=`, `!=`, `<`, `<=`, `>`, `>=` | Scalar comparisons |
| `IN ('a', 'b')`, `NOT IN (…)` | Membership in a literal list |
| `IS NULL`, `IS NOT NULL` | Explicit missing-value checks |
| `LIKE`, `NOT LIKE` | `%` matches a run; `_` matches one character; backslash escapes them |
| `tags HAS 'x'`, `aliases HAS 'x'` | Exact array membership; `NOT HAS` also supported |

Fields: `family` (`entry` or `entity`), `id`, `kind`, `knowledge_state`,
`work_state`, `jira_key`, `title`, `project`, `tags`, `aliases`, `created_at`,
`updated_at`, `verified_at`. Values are single-quoted strings; double an embedded
quote (`'team''s decision'`). Raw SQL, subqueries, functions and comments are
rejected. The parser bounds expression length, nesting and list sizes.
`id` is the record's native ID; combine it with `family` when necessary.

Scalar equality and HAS are case-sensitive. LIKE follows SQLite's default:
ASCII letters are case-insensitive; non-ASCII letters are case-sensitive. These
explicit predicates differ from recall's Unicode-normalized lexical matching.
Date equality, ordering and IN compare ISO dates/timestamps through SQLite
`julianday`, including timezone offsets; date-only values mean midnight UTC.
A read-only fallback normalizes previously accepted timestamp forms that SQLite
cannot parse directly. Invalid stored dates raise an error instead of silently
disappearing from results. Date predicates may scan; this version indexes routing
keys and state/kind/work-state fields, not every supported filter operation.
Precision follows SQLite's date functions (milliseconds), so they are unsuitable
for distinguishing revisions within one millisecond. LIKE on a date field still
matches its stored text. Use the exact `updated_at` value with the existing
optimistic-concurrency write API when updating a record.
Entities have NULL `work_state` and `jira_key`. SQL three-valued logic applies:
`NOT work_state = 'planned'` does not select NULL; use
`work_state IS NULL OR work_state != 'planned'` when missing state is allowed.

Entry projects come from explicit memberships. Concept scope requires an
outgoing, confirmed `used_in` edge to a project with sources. Other entity kinds
require a confirmed sourced project edge in either direction. Multiple projects
are a set: positive project predicates mean any matching membership; negative
predicates mean none, including unscoped records. `project IS NULL` selects only
unscoped records. Hypotheses/superseded records remain excluded by recall unless their
existing inclusion flags are supplied, even when a filter names that state.

## Enable the derived index explicitly

```sh
richi search-index status
richi search-index build
```

These commands use the selected chat workspace. On a repository task retain the
usual `--project-path PATH` guard. Build creates or rebuilds derived lookup keys
and filter indexes atomically. It does not change knowledge, history, sources,
schema version or runtime selection. Existing stores are never upgraded by a
read command or by selecting a new runtime. Filters work without this index.

The index routes normalized IDs, Jira keys and aliases and narrows entity lexical
candidates. Full scoring, state checks and graph rules remain authoritative;
an exact hit does not discard other candidates. New writes refresh their keys
inside the write transaction. SQL triggers mark writes from older runtimes dirty,
so readers include those records through a fallback until refreshed or rebuilt.
Missing indexes, oversized inputs and unsupported index versions use the safe
scan path. The index stores no sources, history snapshots or captured Git bytes.

Use `--explain` to inspect actual visited/scored rows; `--expect REF` explains a
known missing record, including `filtered_where`. Diagnostics share the output
budget and should be measured separately from ordinary retrieval. Building an
index spends disk and write work to reduce reads; measure on a synthetic or
authorized copied store before making performance claims. Unfiltered response
tokens should stay unchanged. Token savings come from avoiding irrelevant
results and repeated narrowing calls, not from deleting evidence in matched
records. Full relevant records still need to be read before conclusions.
