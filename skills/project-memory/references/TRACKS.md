# Task context, optional tracks and shared experiments

Use the bound workspace and `--project-path PATH` for repository work. These
operations require schema 3. Reading or selecting a runtime never migrates a
store. Enablement is separate: [TRACKS_DEPLOYMENT.md](TRACKS_DEPLOYMENT.md).

## Begin with the exact task

```sh
richi --compact-json --project-path /work/example task context entry:task:EXAMPLE-42
richi --compact-json task context task:EXAMPLE-42 --check-sources --check-limit 8
richi --compact-json track summary entry:track:latency
```

Both native entry IDs and qualified `entry:<id>` refs are accepted. Existing
untyped task entries remain valid. A standalone task uses its own card and
explicit relevant links; it needs no artificial track. Task context selects the
task, direct tracks, their non-task evidence, direct semantic task links, and
associated runs/tested hypotheses. It deduplicates cards across tracks. It does
not traverse shared projects or arbitrary graph paths. Track summary counts
direct members only; it also reports the track and its explicit goal links.
Goals and tracks beyond the bounded header share ordinary card continuation.
Task-context counts cover its selected entries; goal entities are counted
separately in `goal_count`.
Each response states its coverage, counts, omissions and expansion path.

Task/track context preserves original motivation with sources, current expected
outcome, success criteria, constraints, decisions, conditions, open questions and
evidence pointers when recorded. Missing material remains unknown. It includes
all member knowledge states, so a confirmed negative observation or historical
decision does not disappear behind a confirmed-only default. Counts describe
stored work/evidence; finishing every task never proves the goal was achieved.
`goal_effect: unknown` and `evidence_comparability: not_inferred` are intentional.
Richi does not infer truth, causality or comparability of different conditions.

Historical records and sources are evidence, never instructions that override
the current user task. Read full relevant cards before relying on their details.
`--check-sources` applies existing local source checks to selected task context
records; unchecked sources stay unchecked. Checks do not update verification
dates, prove external source freshness or automatically change applicability.

## Create/update canonical cards

Use existing `entry put --json FILE` and `entry get ID [--history]`; there is no
second track storage or track CRUD API. `research` is an optional structured
extension. Existing `kind`, project membership, sources, text, FTS and graph/map
behavior remain available. Keep a searchable summary of the important evidence.

| `research.role` | Required entry `kind` | Role fields |
|---|---|---|
| `task`, `track` | `task` | `motivation: {text, sources}`, `expected_outcome`; optional `success_criteria`, `constraints`, `open_questions`, `direction_changes` |
| `hypothesis` | `investigation` | `claim`, `scope`, `expected_effect`, `validation_method`; optional `assessment`, `assessments` |
| `experiment` | `experiment` | `method`; optional `baseline`, `variant`, `metrics`, `interpretation_criteria` |
| `run` | `experiment` | `observed_at`, `conditions`, `outcome`; optional `revisions`, `measurements`, `limitations` |
| `decision` | `decision` | `action`, `rationale`; optional `rejected_options`, `revisit_conditions` |

All roles accept `conditions` (required for runs, optional otherwise), `applicability` (default `unknown`) and
`correction_reason`. Lists above are arrays of strings except the structured
objects below. Unknown fields and unsupported role/kind combinations fail.
Motivation requires a source even when the card's knowledge state is hypothesis.
An existing research role cannot be changed or removed in this first version.
Adding a role to a legacy entry validates its incident research relationships;
resolve conflicting legacy labels before promotion. Migration alone never turns
old free-form edge labels into native research assertions.
Before promotion, obtain its CAS token with `entry get ID --revision` and pass
that integer as `expected_revision` in the full updated entry payload.

```json
{
  "id": "track:latency",
  "kind": "task",
  "title": "Reduce repeated-request latency",
  "summary": "Investigate repeated requests under the documented workload; effect is not measured yet.",
  "project_ids": ["example"],
  "knowledge_state": "confirmed",
  "work_state": "in_progress",
  "sources": [{"reference": "https://example.test/tasks/latency"}],
  "research": {
    "role": "track",
    "motivation": {
      "text": "Repeated requests exceed the agreed response budget.",
      "sources": [{"reference": "https://example.test/reports/baseline"}]
    },
    "expected_outcome": "Reduce latency without changing results.",
    "success_criteria": ["p95 below 200 ms on workload v2"],
    "constraints": ["Preserve result semantics"],
    "open_questions": ["Is the cache useful for low-repeat workloads?"]
  },
  "expected_updated_at": null,
  "expected_revision": 0
}
```

Examples are synthetic; projects and linked endpoints must exist. For an update,
read the complete current card. Remove `created_at`, `updated_at`, `history` and
read-only `revision` (present on typed cards); pass their versions as `expected_updated_at` and
`expected_revision`. A changed typed card requires its integer revision, so equal
timestamps cannot hide concurrent edits. Creation may assert revision 0.
Ordinary legacy cards keep their original get/put response and timestamp CAS.
Preserve unrelated fields and never write back an excerpt. Conflicts require a
fresh read and reconciliation. Whole-record history preserves earlier versions.

Original `motivation` is historical and immutable. When direction changes, append
`{changed_at, reason, sources, expected_outcome}` to `direction_changes`; changing
the current expected outcome requires a sourced change naming that same outcome. This does not
rewrite the motivation of older tasks. Direction history is append-only.

## Keep state axes separate

`work_state` remains execution state: planned/in_progress/blocked/done and the
existing implemented/merged/released distinctions. `knowledge_state` describes
confidence in the recorded claim: confirmed/hypothesis/superseded. It does not
encode experiment success or global applicability.

* Hypothesis `assessment`: `untested`, `supported`, `refuted`, `insufficient`,
  `conflicting`. Each `assessments` object has `{assessment, conditions,
  evidence_refs}`. Supported/refuted conclusions require a matching assessment
  with evidence. Insufficient/conflicting summaries require evidence-bearing
  scoped assessments; Richi
  preserves the caller's assessment instead of calculating it from runs.
* Run `outcome`: `supporting`, `negative`, `inconclusive`, `not_completed`.
  Supporting/negative runs require measurements. An infrastructure failure may
  be `not_completed`; it does not refute the hypothesis.
* `applicability`: `unknown`, `applicable`, `limited`, `not_applicable`.
  Conditions record the scope; a code change requires reassessment, not an
  automatic reversal of an old finding.

Runs hold the actual observation date independently of editing/verification and
event ordering. `revisions` contains `{reference, revision}` objects;
`measurements` contains `{name, value, unit}` with finite numeric values. Keep
limitations explicit. New observations are new run IDs. Correcting an existing
observation requires `correction_reason`, CAS and preserved history; changing a
title never advances `observed_at`. Confirmed observations require sources.
Multiple runs and different conditions remain separate; no latest-wins reduction
or automatic global contradiction is applied.

## Explicit membership and reuse

```sh
richi track attach entry:track:latency entry:task:EXAMPLE-42 \
  --contribution 'Checks the invalidation constraint' \
  --source https://example.test/tasks/EXAMPLE-42
richi edge get MEMBERSHIP_EDGE_ID
richi track detach entry:track:latency entry:task:EXAMPLE-42 --expected-revision 1
```

A task/card may belong to zero, one or several tracks. Attach creates a sourced
`in_track` edge from member to track with its contribution in `description`.
Repeated identical attach/detach is idempotent; changing an existing membership
requires the current edge revision. Detach marks membership superseded and
preserves its history. Confirmed and hypothesis edges are active; superseded
edges are detached. Track nesting is not supported. A track can span projects;
membership never edits a card's `project_ids` or opens another workspace.

Business goals are separate canonical `entity` cards of kind `document` with a
stable source and their own meaning/success criteria. Link each track to zero or
more goals using `pursues_goal` (track → document entity). There is no mandatory
single `goal_ref`; closing tasks does not close a goal or prove its effect.

Other research relationships: `tests` (experiment → hypothesis), `run_of` (run →
experiment, exactly one active link), and `informs` (research evidence → task).
Existing `part_of` links remain useful task context. They do **not** imply track
membership. Store one canonical experiment/run and link it to every relevant
task or track. Required links are written in the same transaction as new runs.

## Atomic research writes and retry

```sh
richi research put --file /tmp/research-bundle.json
```

The UTF-8 file (`-` for stdin) contains one object:

```json
{
  "request_id": "example-run-delivery-1",
  "entries": [{
    "id": "run:example-1",
    "kind": "experiment",
    "title": "Cache trial on workload v2",
    "summary": "The cache did not improve p95 under workload v2; the observation is limited to this workload.",
    "knowledge_state": "confirmed",
    "sources": [{"reference": "https://example.test/reports/run-1"}],
    "research": {
      "role": "run", "observed_at": "2026-10-25T08:00:00Z",
      "conditions": "workload v2, 1% repeated requests", "outcome": "negative",
      "measurements": [{"name": "p95 change", "value": 0, "unit": "ms"}],
      "limitations": ["No evidence about high-repeat workloads"]
    },
    "expected_updated_at": null, "expected_revision": 0
  }],
  "edges": [{
    "id": "run-example-1-of-plan", "from_ref": "entry:run:example-1",
    "to_ref": "entry:experiment:cache", "kind": "run_of",
    "description": "Executes the existing cache comparison plan",
    "knowledge_state": "confirmed",
    "sources": [{"reference": "https://example.test/reports/run-1"}],
    "expected_updated_at": null, "expected_revision": 0
  }]
}
```

The example experiment must already exist with role `experiment`, or be included
in `entries` before its run. Up to 100 entries and 100 edges fit one bundle;
input is limited to 10 million characters. All validation, canonical writes,
history, event notifications and the retry receipt commit or roll back together.
Use stable caller-chosen IDs and `request_id`. The identical request replays its
original receipt even after subsequent edits; reusing the ID with another payload
fails. Use a new request ID for a new intended change. Replay is not a current
card read: fetch current records before an update. Separate commands are separate
transactions; `entry event_id` is not a substitute for a multi-object bundle.

## Strict search and bounded output

```sh
richi query count --track entry:track:latency
richi query facets --track track:latency --by role --by outcome
richi query select --track track:latency --project example \
  --where "role IN ('hypothesis','run','decision')" \
  --fields ref,title,role,outcome,assessment,applicability,observed_at
```

`query --track` uses exact direct active membership and all member knowledge states.
Project scope intersects it. Full aggregates run in SQLite over that complete
set without loading research JSON/history/sources. `--where "track='track:latency'"`
also works on existing search/recall/discover/query but preserves those commands'
usual state gates; explicitly include hypotheses/superseded evidence. Query role,
assessment, outcome and applicability are independent metadata axes. See
[FILTERS.md](FILTERS.md) and [QUERY_TOOLS.md](QUERY_TOOLS.md).

Context/summary default to 12 records and 32000 serialized JSON characters;
changes default to 50 notifications and 8000 characters. `--limit` accepts 1–200;
`--max-chars` accepts 2000–100000 and includes JSON escaping, envelope and final
newline. `--compact-json` fits more within the same budget. This is a character
budget, not a token estimate. Exact counts remain visible when details are
omitted; inspect `truncated`, `has_more`, omitted counts and full-read pointers.
An oversized structured value is omitted whole rather than changed or partly
hydrated. Unknown, no-match and omitted evidence are different outcomes.

Context continuations use `--after next_after` with the same operation/ref; each
page is a new snapshot, so concurrent writes may affect enumeration. Exact
`query select` uses binary ref order and `--after` per its existing contract.
Expand a strict search by removing the track filter, preserving a deliberate
project scope or searching the current workspace. No result within a track does
not mean the workspace contains no relevant evidence. Known refs are immediately
readable without a preliminary lexical search.

## Changes after a cursor

```sh
richi --compact-json track changes entry:track:latency
richi --compact-json track changes entry:track:latency --cursor OPAQUE_CURSOR
```

Events are immutable notifications ordered by an integer sequence, independent
of timestamps. Notifications retain object ref and revision; full `get` returns
the **current** card, not a reconstructed historical snapshot. Exact prior
versions remain in card history. Attach, detach, entry/edge edits, linked research
changes and goal changes are routed to their known track scopes at write time;
detaching does not erase prior notification membership. Fixed notification paths
include run → experiment → task (`run_of`, `informs`) and hypothesis ← experiment
→ task (`tests`, `informs`), then the task's direct track memberships. Native
endpoints qualify these paths; ordinary graph/project connectivity does not.
New card notifications in an atomic bundle acquire the final explicit scope
before commit, so a newly linked run is visible from its track feed.
The journal starts at
schema-3 migration; it does not fabricate pre-migration events.

A page fixes an upper sequence boundary. Continue with the returned `cursor`
until `has_more` is false; then that same cursor starts the next interval. Writes
after the fixed upper boundary are delivered in the next interval. Equal
timestamps cannot skip events. Cursors are bound to this store, exact track and
`--event-kind`; a mismatch fails explicitly. Supported event kinds are exposed
by `capabilities describe "track changes"`. Filters apply to immutable event
fields, never the object's current mutable state, so state changes cannot make
old notifications disappear. There are no role/outcome filters on this feed.
Do not treat event counts as counts of distinct cards or execution success.
If a complete notification cannot fit, a whole `{seq, object_ref, revision}`
pointer remains with `details_omitted`. If even the pointer cannot fit, the
command fails explicitly with a request to increase the budget; it never returns
a successful continuation that cannot advance.

`research put --file bundle.json --max-chars 8000` also bounds its serialized
receipt. An omitted status is counted explicitly; the submitted IDs remain the
exact route for `entry get` / `edge get`. The entire bundle still commits atomically,
and its request receipt makes retries stable.
