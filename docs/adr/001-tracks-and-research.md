# Optional tracks and explicit research context

Status: implementation decision, 2026-10-25. Base: `208432183f0f01b6b514475b5f1534fa09cb29ae`
(merged PRs #11 and #12). No deployment or live-store migration is part of this change.

Canonical entries receive an optional validated `research` extension in schema 3.
Roles are task, track, hypothesis, experiment, run and decision; base entry kinds,
IDs, history, sources, graph nodes and project membership remain canonical. A
track uses base kind task, a hypothesis investigation, and a run experiment.
This avoids rebuilding existing constrained tables or introducing duplicate cards.
Business goals are sourced document entities connected by `pursues_goal`; zero,
one or several links are valid. They remain independent of tracks and projects.

`in_track` is explicit entry-to-track membership, with a contribution and source
for confirmed assertions. Superseding this relation detaches it without deleting
its history. There is no inheritance through part_of, shared projects, tags or
similarity. Strict track and project filters intersect. Context may follow the
explicit semantic relations informs, tests and run_of; its coverage says so.

Execution stays in work_state. Scoped hypothesis assessment, observed run outcome,
observation credibility (knowledge_state), and applicability are separate. A
not_completed run does not refute a hypothesis; a changed source does not establish
falsehood. Motivation retains its sources, and direction changes have dated reasons.
No task count proves a goal effect. Historical records are evidence, not instructions.

A research bundle writes entries and required relations in one caller transaction.
Request ID and payload hash make retries stable. Integer revision CAS protects typed
entry/edge writes; legacy timestamp CAS remains available. Existing generic writers
must validate typed invariants and emit notifications, or reject the operation.

The append-only event journal stores sequence, object reference, revision, kind and
recorded_at, not another copy of payloads. Immutable track scope rows retain detach
and before/after scope. Events are notifications; later card reads are explicitly
current state. Feed cursor binds store UUID, scope, event-kind filter, last sequence
and a fixed upper sequence. A completed poll returns a cursor which can start the
next bounded snapshot. Timestamp ties do not affect ordering. Read operations do
not update a global watermark or verified_at. No retention is implemented.

Schema migration is explicit and atomic. Old runtimes refuse schema 3 before reads
or writes; choosing a runtime never migrates. Migration preserves existing rows and
histories. Derived search indexes stay rebuildable and their existing fallback
contract remains in force. Native research metadata and membership access use SQL
indexes. Bounded output measures the actual serialized JSON plus newline; omitted
large fields retain refs, counts and a full-read route. Context pages use independent
read snapshots, unlike the fixed-upper event feed.

Acceptance and measured limits are recorded in TRACKS_EVAL.md. Public fixtures are
synthetic. Deployment requires separate package upgrade, backup, explicit migration
and optional index build; the installed skill and runtime are not switched here.
