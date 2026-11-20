# Create or update an entry

English | [Русский](ENTRY_WRITE.ru.md)

Use this procedure for `entry put`, including a draft that will not be submitted.
Workspace selection, repository membership and write authorization follow
[SKILL.md](../SKILL.md). A draft-only request does not authorize a write.

## Preserve the complete payload

`entry put` replaces all persisted fields; omitted optional fields reset to defaults.
For an existing ID, read `entry get ID`, copy its full payload, remove only
`created_at`, `updated_at`, `history`, then apply the intended edits. Set
`expected_updated_at` to the exact `updated_at` just read. Preserve unrelated
fields, sources and their metadata, project memberships, states and dates.
Do not construct an update from recall, read, brief or a graph excerpt.
For a new ID, use `expected_updated_at: null` (create-only).

```sh
richi --compact-json --project-path /work/example entry get task:EXAMPLE-42
# Prepare a UTF-8 JSON file from that full result.
richi --compact-json --project-path /work/example entry put --json /tmp/entry-update.json
richi --compact-json --project-path /work/example entry get task:EXAMPLE-42
```

A conflict means the record changed: reread, reconcile, then retry with the new
version. Never overwrite parallel work with the old payload. Verify successful
writes by full get; report failed/unavailable saves. Use CLI transactions, not SQL.
A quoted heredoc with `--json -` is also supported. One object or an array of up to
1000 objects is atomic within one command; duplicate IDs are rejected, input is
limited to 10 million characters. Separate commands are not one transaction.

## Entry fields

Required: `id`, `kind`, `title`, `summary`. Caller-chosen stable IDs are at most
200 characters without control characters, e.g. `task:EXAMPLE-42`.

| Field | Contract |
|---|---|
| `kind` | `task`, `decision`, `fact`, `note`, `investigation`, `experiment`, `procedure` |
| `project_ids` | Existing project IDs; default `[]` for genuinely general knowledge |
| `jira_key` | Optional actual issue key of any tracker, spelled as the tracker writes it (`ABC-123`, `AB#123`, `#123`); never invent one. Recall treats a token as an issue key only when a record declares it: see [RECALL.md](RECALL.md#ключи-задач) |
| `knowledge_state` | `confirmed`, `hypothesis` (default), `superseded` |
| `work_state` | `planned`, `in_progress`, `implemented`, `merged`, `released`, `blocked`, `done` or `null` |
| `decision_card` | Optional structured decision context on a task. Omission or `null` in a replacement removes the current card |
| `sources` | Objects with required `reference` and optional `label`, `type`, `observed_at`, `revision`, `locator`, `sha256`, `git`, `measurements`. Defaults to `[]` |
| `tags`, `aliases` | Nonempty strings, normalized by sorting/deduplication; default `[]`; use observed terminology |
| `verified_at` | ISO date or timezone-aware ISO timestamp, or `null`; date of evidence check, never automatically advanced |
| `expected_updated_at` | Exact version from get for an update; explicit `null` for create-only; not persisted |
| `event_id` | Optional unique delivery ID, entry-only, not persisted |

Project, source, tag and alias arrays each allow 100 items. Unknown fields/types
are rejected. Confirmed facts and decisions require at least one source; presence
of a source does not verify truth. Generate structured `source.git` with
[GIT_CAPTURE.md](GIT_CAPTURE.md); exact manual anchors: [DIAGNOSTICS.md](DIAGNOSTICS.md#точные-источники-в-записях).

Identical normalized content returns `unchanged` without a new history revision,
even with stale CAS. Reusing `event_id` with the same normalized payload returns
`replayed`, even after later edits; different content with that ID is an error.
Use a new event ID or omit it for a new intended change. Other statuses are
`created`/`updated` in `results`. Entity/edge writes do not accept `event_id`;
their distinct payloads are in [GRAPH.md](GRAPH.md).

## Current decision on a task

When a task is waiting on a choice, maintain its `decision_card` alongside the
full history and sources. On an existing task, read its full payload and the
relevant experiment or decision first. Copy the reviewed evidence into a short
card, cite its source, and use the task's `expected_updated_at` on replacement.

```json
{
  "decision_card": {
    "question": "Enable batch encoding in production?",
    "observations": [{
      "text": "The encoder was faster. End-to-end latency is still unverified.",
      "reference": "entry:experiment:batch-trial"
    }],
    "recommendation": {
      "text": "Start with small batches.",
      "by": "agent",
      "reference": "entry:experiment:batch-trial"
    },
    "decision": {"status": "pending"},
    "reconsider_when": "Compare deadline completion and end-to-end tail latency in paired runs."
  }
}
```

`question` and `decision.status` are required. Record `pending` while the
choice is open. Record `accepted` or `rejected` only with the actual outcome in
`text`, the decision maker in `by`, and their approval or rejection in
`reference`. A proposed rollout and its author's name belong in
`recommendation`. Cite an original document or the stable `entry:<id>` of a
supporting record. Preserve conditions and unknowns in each observation.
The card allows up to eight observations and 6000 compact JSON characters.
Omitted observations default to an empty list. Missing recommendations and
review conditions default to `null`. Edits appear in the task's revision history.
If an older Richi edits the task, the previous card stays in its revision
history. Check the new task revision before recording another current card.

## Write useful current knowledge

For a numerical result, attach a `measurements` array to the source containing
the calculation. Each observation has four nonempty strings. Preserve the
original precision and comparison order in `value`, and include the population
and denominator in `sample`. Use `locator` to identify a table, sheet or
section within the source. A source can carry up to 100 observations.

After rollback, older writers can edit a title or summary while preserving
the cited evidence. If an older writer changes the source list, the earlier
observations remain in revision history. Review the new sources before
attaching observations to them.

```json
{
  "reference": "https://example.invalid/trials/latency.csv",
  "locator": "Completion by deadline, row 4",
  "observed_at": "2026-04-18",
  "measurements": [{
    "metric": "RPCs completed within 200 ms",
    "value": "87 → 92",
    "unit": "completed RPCs",
    "sample": "100 requests per mode, OFF followed by ON"
  }]
}
```

Keep a stable task ID and a connected summary of the current decision, reasons,
scope, material conditions/exceptions, evidence, implementation state, unknowns
and next step. A 1000–3000 character summary is an optional guide, not a limit
or parsed template. Replace obsolete current status rather than appending an
endless chronology. Markdown headings and a Templates runtime are not required.

An unsuccessful experiment can be confirmed within its observed conditions;
an unchecked explanation remains a hypothesis. Rejection is not supersession.
Keep implemented, merged and released distinct; do not advance `verified_at`
merely because text changed. Preserve measurement/event dates separately.
Use sources and revision/hash anchors actually checked, not missing files or guesses.

Useful independent details belong in `investigation`, `experiment` or `decision`
entries with their own sources, states, actual projects and Jira key. Keep their
refs and active constraints in the task summary. Current text is searchable;
history is not. History preserves complete before/after revisions and creation
but cannot replace accessible current evidence. Read it only when chronology is needed.
To split an existing task, consolidate records or change links during cleanup,
use [MAINTENANCE.md](MAINTENANCE.md); children and links must be verified before
shortening the parent. Ordinary single-record edits need no maintenance runbook.
