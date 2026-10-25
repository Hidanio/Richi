# Discover routes, then fetch whole records

Use this two-step path when the relevant refs still need selection. For a small
entry set already defined by a lexical query and strict predicates, `search QUERY
--where EXPR` can return full matches in one call; see [FILTERS.md](FILTERS.md).
Search remains limited and lexical: use `query` for complete metadata counts,
existence and paginated inventories. A known ref goes straight to get/fetch.

```sh
richi --compact-json --project-path /work/example discover 'pool sizing' --project example
richi --compact-json --project-path /work/example fetch \
  --ref entry:experiment:pool-sizing --ref entity:concept:connection-pool --max-chars 16000
```

`discover` is a small routing view: default eight results, 8000 characters;
`--limit` 1–200, `--max-chars` 2000–100000. It uses the same ranking, issue-key
checks, explicit one-hop expansion and project boost as `recall`. Project boost
is not a strict filter. Use `--where "project = 'example' AND kind = 'decision'"`
for mandatory scope before candidate limits and graph expansion; see [FILTERS.md](FILTERS.md).
Only confirmed knowledge appears unless independently
including `--include-hypotheses` / `--include-superseded`.
The budget bounds returned text, not search CPU or peak memory: discovery still
uses recall's candidate scans and record loading.

Cards contain at most 300 characters of verbatim excerpt and no source payloads.
`source_count` / `sources_omitted` cover the full record. Edge routes retain
origin, direction, state and an exact evidence pointer. Follow `read_more.args`
as structured arguments; preserve everything after the reference's first colon.
**Discovery is insufficient for conclusions or updates**, even when an excerpt
fits completely. Read relevant full records and check authoritative sources for
current status. Scores rank matches; they are not truth probabilities.

`ambiguity.detected` and the total concept count survive a tight budget.
Omitted alternatives do not establish a single meaning. Empty results with
`no_match=false` mean matching details did not fit. `no_match=true` means no
indexed match was found, not proof that work never happened. Counts describe
retrieval candidates, not the whole database. See [RECALL.md](RECALL.md) for
ranking and coverage limits; [RECALL_DEBUG.md](RECALL_DEBUG.md) for a known miss.

`fetch` accepts up to 32 unique `entry:`, `entity:`, `edge:` or `project:` refs.
Duplicates are removed in first-occurrence order. It reads one consistent
snapshot and returns **whole current records without history**. Explicit refs
can return any knowledge state. A `found` record equals its full get payload;
project records equal the corresponding item in `project list`.

`missing`, `oversize` and `read_limit` outcomes are explicit and include an
exact read pointer. `oversize` means the whole record cannot fit this response;
raise the budget or use the individual get. Projects have no individual get;
for a project above the maximum budget, `project list` provides full project
records in the selected workspace and is unbounded. `read_limit` means encoded storage
is too large for the bounded read, not a proven serialized size. Inspect such
records individually only when needed. A default fetch uses 16000 characters;
the same 2000–100000 bounds include all JSON formatting and the final newline,
not tokens or UTF-8 bytes. Compact JSON usually fits more whole records.

If `remaining_count` is nonzero, repeat the **original ref list in the same
order** with `--offset next_offset`. Retain that input list: long remaining IDs
are not redundantly echoed. Each continuation observes a new snapshot; reread
before an update and use optimistic concurrency. `request_complete` means all
refs from this offset were processed; missing/oversize records are not successful
reads. `all_requested_records_returned` is true only when a single call starting
at offset zero returns every requested record in full. For writes follow
[ENTRY_WRITE.md](ENTRY_WRITE.md); never submit a discovery card as a replacement.
