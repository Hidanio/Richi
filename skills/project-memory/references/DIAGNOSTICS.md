<a id="диагностика-поиска-знаний-и-источников"></a>
# Search, knowledge and source diagnostics

English | [Русский](DIAGNOSTICS.ru.md)

All commands run explicitly, operate locally and call neither models nor the
network. SQLite remains the canonical store. Schema 2 is unchanged; knowledge
writes still use `entry/entity/edge put` with history and CAS. Read commands open
the database with `mode=ro` and `query_only=ON`.

The examples below use the installed `richi` command. IDs are illustrative;
use `project list` to obtain real projects.

<a id="почему-запись-не-найдена"></a>
## Why a record was not found

The procedure for investigating a known miss and the semantics of
`--expect`/`--explain` are in [RECALL_DEBUG.md](RECALL_DEBUG.md). Additional
implementation counters are described below.

<a id="счётчики-предварительного-отбора-entries"></a>
### Entry preselection counters

With FTS available, a full payload is read by primary key for a record in the
FTS shortlist or a match under ID/Jira/alias rules. After an explicit
`search-index build`, route lookup uses the index and reads only the selected
records' metadata (`id`, `jira_key`, `aliases`). Without the index, a linear scan
remains; without FTS, a full lexical fallback applies. The index also narrows
lexical entity candidates. Records not refreshed by an older runtime and records
too large for the index use a fallback path. Normalization and scoring are
unchanged. A composed `--where` applies before the FTS shortlist, to direct and
graph results. Commands and semantics: [FILTERS.md](FILTERS.md).

The full `explain.counts` distinguishes:

| Field | What it counts |
|---|---|
| `entries_scanned` | Entries actually visited after filters and available index selection: metadata with FTS, full records without FTS |
| `entities_scanned` | Full entities passed to lexical scoring after filters and available index selection |
| `entries_scored` | Full records processed by direct-search lexical scoring, including those rejected by later guards |
| `entries_prefiltered` | Entries excluded by preselection, including an excluded expected record |
| `expected_entry_probes` | Separate full reads and lexical checks of an excluded entry named by `--expect`; at most one |

`entries_scored + entries_prefiltered = entries_scanned`. The diagnostic probe
of the expected record is not included in `entries_scored`: it is already counted
among `entries_prefiltered`. These are not counts of results, physical SQLite
reads or all SQL rows: graph expansion, expected-node state checks and FTS have
separate read paths. A lower `entries_scored` does not mean O(N) work disappeared
or physical reads became constant.

For visited records excluded by routing, `direct_rejected` contains
`routing_prefilter`: their full lexical guard did not run. Records the index
never visited are not added to rejection counters. If an excluded entry is
explicitly named with `--expect`, one additional read by ID reports the actually
tested `weak_lexical`, `subject_guard`, `short_subject_guard`, `jira_guard` or
`fts_not_shortlisted` relative to the FTS shortlist already produced. This probe
does not add the record to candidates or change ranking; possible inclusion
through the graph is reported separately. Missing or state-filtered targets do
not receive an additional full probe. `filtered_where` marks an expected record
excluded by the strict composed filter.

New diagnostic fields consume the shared `--max-chars` budget. With a tight
budget, they may be omitted with `details_omitted_for_budget`, or the number of
shown cards may change. An absent counter does not mean zero. Compare ordinary
results separately from `--explain`/`--expect`; preserving their behavior does
not promise byte-identical diagnostic JSON at the same budget.

<a id="кандидаты-для-ручного-обслуживания"></a>
## Candidates for manual maintenance

The diagnostic command is `punisher`.

```sh
richi punisher --project service-example --limit 20 --max-chars 16000
richi --project-path /actual/checkout punisher --project service-example \
  --summary-warn-chars 6000
richi punisher --stale-days 180 --as-of 2026-10-01
```

`health` contains SQLite status and counters; `findings` contains short candidates
for review. Checks cover missing sources/dates, verification age, isolated
entities, exact duplicate text, shared aliases and summary length. A hypothesis,
shared alias or old date alone does not imply an error. Even identical text can
refer to different tasks or contracts.

`--project` defines a strict scope: entry memberships and direct confirmed
`used_in` edges with sources from an entity to the project. This differs from
recall priority boosting and the connected component of `graph export --project`.
SQLite integrity checks cover the whole database; semantic candidates cover the
selected scope. `--as-of` fixes the date for a reproducible report.

Check `coverage`, `truncated` and `budget`: a bounded report does not prove that
other candidates are absent. `candidate_count` and `finding_counts` count all
findings within the scope actually examined before output limits;
`budget.omitted_findings` counts findings excluded by `--limit` or `--max-chars`.
If the counters themselves are shortened, `finding_counts_omitted` is present.
The current candidate count does not expand scan caps: up to 5000 records per
table and 10000 explicit edges. Incomplete scanning is marked in
`coverage.complete`, separately from the number of findings shown. Ordering is
stable: level (`error`, `review`, `info`), code, refs. A finding may be omitted
entirely to fit the budget; identifiers are never shortened.

Punisher does not merge, delete or change records, history, links, states,
verification dates, FTS or the schema version. It supports schemas 1 and 2 and
does not read entry_history, source files, Git or the network. No data from
another workspace is used. Candidate decisions are covered in
[MEMORY_AUDIT.md](MEMORY_AUDIT.md); the change procedure is in
[MAINTENANCE.md](MAINTENANCE.md).

<a id="длинная-summary"></a>
### Long summaries

`--summary-warn-chars` is an integer from 1 through 1000000 inclusive, default
6000. Invalid values are rejected; the internal Python API also rejects `bool`.
This is an initial configurable readability guide, not a measured quality
boundary or a write limit.

Only summaries of entries and scoped entities already selected are checked.
Length is `len(summary)` of the stored Unicode string, without normalization or
`strip`: Python characters, not bytes, tokens or visual graphemes. At exactly the
threshold there is no finding; strictly above it, exactly one `long_summary`
appears per record. Its level is `review` for `confirmed`, and `info` for
`hypothesis` and `superseded`. Neither value establishes a data error or a need
to delete/split the record. It does not change `health`: SQLite integrity status
and editorial-review candidates are distinct.

The applied threshold is always returned in report metadata as
`summary_warn_chars`. Example finding for an illustrative task:

```json
{
  "code": "long_summary",
  "level": "review",
  "reason": "Read the full record and review whether current state and details would benefit from separation. Length alone does not establish mixed history, outdated knowledge, or reduced quality.",
  "refs": ["entry:task:EXAMPLE-42"],
  "details": {
    "summary_chars": 6001,
    "threshold_chars": 6000,
    "kind": "task",
    "knowledge_state": "confirmed",
    "read_arguments": ["entry", "get", "task:EXAMPLE-42"]
  }
}
```

For an entity, `read_arguments` is `["entity", "get", "<полный ID>"]`.
If the ID starts with a hyphen, a separate `--` argument is inserted before it
so the CLI reads it as an ID rather than an option; the ID itself is unchanged.
These are exact arguments after `richi`, not a shell string: do not concatenate
an unescaped ID into an executable command. The long summary itself is not
included in the finding. A shown finding retains all required details even
under a small budget; if it cannot fit in full, `omitted_findings` increases.
The absence of `long_summary` from a truncated report does not prove there are
no long cards. Read the full `get` first, then decide whether separating current
state from detail would help.

<a id="снимок-проверяемой-группы-и-повторная-проверка"></a>
## Snapshotting a review group and checking it again

```sh
richi sources snapshot \
  --ref entry:experiment:pooling --ref entity:connection-pool \
  --output work/pooling-review-01.json

richi sources snapshot --project service-example \
  --output work/service-example-review-01.json

richi sources check --manifest work/pooling-review-01.json \
  --max-chars 16000
```

A snapshot creates a separate JSON manifest without changing knowledge. It
records the complete selected cards, their project memberships, the set and
revisions of incident explicit edges, card/edge sources and hashes of accessible
local files. All knowledge states are included. Project scope is strict;
neighboring cards are not included automatically through graph connectivity.

A subsequent check detects card changes, cards entering/leaving the selected
project, link changes and local-file drift. A new edge is detected even when
the node's `updated_at` is unchanged. This matters when preparing a merge plan
in a database receiving writes from other tasks.

A snapshot is not a backup: it lacks the complete data needed for restoration.
It is not a lock either. Writing still requires reading the full current card
and using `expected_updated_at`. Changes can occur after the check; a manifest
does not make several write commands one transaction.

An existing manifest is never overwritten. A new comparison baseline requires
a new path. Unsupported/unavailable sources and limitations are detailed in
JSON. URLs are not fetched; Jira, commit and conversation links remain pointers
for the agent to check when needed. Regular local files are read with per-file
and aggregate size limits; devices, FIFOs and other special files are not hashed.

Default limits: 200 selected nodes, 1000 incident edges and 1000 unique
references; 8 MiB per file, 64 MiB total per run, 4 MiB per manifest. Exceeding
the selected graph limit requires a narrower group; skipped files are reported
as unavailable. `sources check` returns overall `status`, `has_changes`, counts
and coverage even when details are reduced for the budget. `unsupported` means
the remaining URLs were not checked, not that they are broken. This version
tracks `graph_edges`; legacy service relationships in `relations` are not
snapshotted separately, as explicitly stated in `coverage`. A manifest is bound
to its database path and refuses comparison against a different database.

An unchanged SHA-256 means the bytes match the manifest. It does not confirm
the claim, current service state or a production release. An unavailable source
means a verification gap, not an automatic refutation of historical knowledge.
None of these commands advances `verified_at`.

For Git sources, the manifest also preserves `source.git`, but comparing a local
file through `reference` still compares current bytes. A saved commit or
working-tree snapshot is checked separately with `sources git-check`. Code
navigation and reverse lookup are described in [GIT.md](GIT.md). Punisher checks
Git metadata structure and supplies a verification hint for the corresponding
card without reading the repository automatically.

<a id="точные-источники-в-записях"></a>
## Precise sources in records

`sources` for entries, entities and edges support these optional fields:

```json
{
  "reference": "/absolute/repo/module.py",
  "type": "code",
  "label": "Контракт поля в проверенной версии",
  "observed_at": "2026-10-01",
  "revision": "git:<actual-commit-sha>",
  "locator": "FunctionName, L42-L58",
  "sha256": "<actual-64-hex-whole-file-sha256>"
}
```

Values in angle brackets are placeholders; do not copy them as evidence.
`revision` and `locator` are nonempty strings of up to 1024 characters without
control characters. `sha256` is 64 hexadecimal characters, normalized to
lowercase: a hash of the entire file, not a line or excerpt. Populate these
fields from sources actually checked. They are preserved in the original JSON
and history; the module does not execute a locator or check out the given revision.

Older records without the new fields remain valid. No schema migration is needed.

<a id="контекст-workspace-и-пути-проектов"></a>
## Workspace context and project paths

Use `richi status` for chat/CLI selection, workspace storage, runtime and cwd membership.
Use `richi --project-path PATH status --project ID` for an expected repository.
Only the selected workspace is read; the same path in another store cannot satisfy the check.
Before chat binding, the report is `selection_required` and reads no runtime settings or database.
Broken selected runtime produces `unavailable`; no installed-runtime fallback occurs.

`richi status --projects --limit 50` also reports registered path problems. Output limit is
1–200; problems precede healthy paths. Check `total`, `checked`, `omitted`, `counts` and
`status` before claiming coverage. The audit has a 10-second/10000-registration budget
and a 30-second worker timeout. A remote-only project with no path is informational.
Missing, inaccessible, duplicate or invalid Git paths require review; no paths are changed.
Current-path matching can inspect other registered paths even without `--projects`.

Top-level status is `ok`, `attention`, `selection_required` or `unavailable`. Exit 0 means
a report was emitted, not a health assertion. Argument, selector and configuration errors
exit 1. `status` reads projects/schema metadata only; it does not replace knowledge `check`
or requested `punisher` maintenance. Continue using `--project-path` on knowledge operations
to recheck membership in their transaction.
