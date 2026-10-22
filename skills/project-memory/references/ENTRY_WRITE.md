# Create or update an entry

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
| `jira_key` | Optional actual Jira key; never invent one |
| `knowledge_state` | `confirmed`, `hypothesis` (default), `superseded` |
| `work_state` | `planned`, `in_progress`, `implemented`, `merged`, `released`, `blocked`, `done` or `null` |
| `sources` | Objects with required `reference`; optional `label`, `type`, `observed_at`, `revision`, `locator`, `sha256`, `git`; default `[]` |
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

## Write useful current knowledge

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
