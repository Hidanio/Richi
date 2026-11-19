<a id="применение-обслуживания-памяти"></a>
# Apply memory maintenance

English | [Русский](MAINTENANCE.ru.md)

Use only for authorized cleanup mutations in the selected workspace/scope.
Start with [MEMORY_AUDIT.md](MEMORY_AUDIT.md) to select candidates and decide
which records actually represent the same knowledge. An audit-only request
stops there. Preserve sources, distinctions, history and searchable knowledge;
no physical deletion is part of this workflow.

<a id="обоснованное-объединение"></a>
## Justified consolidation

Before the first change to live memory, run `richi backup` in the bound workspace
without `--project-path` and retain the returned path. The audit itself needs no
backup. For a reviewed group, you can save
`sources snapshot --ref REF --output NEW.json` (repeat `--ref` for other cards).
Before applying the plan, run `sources check --manifest NEW.json`: it detects
card changes, changes to the set of links and local-file drift. A manifest
records the versions read, but does not replace CAS or block later concurrent
writes. Prepare a local `old refs → canonical ref` plan: what matches, what
moves, why this is the same scope/version, and what the links require. The plan
supports result verification and recovery; it is not a mandatory approval stop.
Do not consolidate disputed groups.

1. Read the full current payloads and `updated_at`. Prepare the canonical record,
   preserving sources with labels/dates, semantic synonyms, project memberships,
   measurements, qualifications and useful earlier attempts in searchable text.
   History alone is insufficient: ordinary search does not read it.
2. Save the canonical card with `expected_updated_at`, then verify it with `get`.
   For a new ID, use `expected_updated_at: null`. A conflict means new concurrent
   knowledge: reread and reconstruct the group instead of overwriting it.
3. Read all incoming/outgoing candidate links with the required state flags.
   `neighbors` is bounded: check truncation; if needed, use a saved complete
   catalog/snapshot to enumerate links. Derived memberships change through
   `entry.project_ids`; do not duplicate them.
4. Save or refine justified relationships of the canonical node with sources
   and the same direction and state semantics. Do not automatically transfer
   causality or `used_in` scope between contexts. New links must not create
   meaningless self-loops. Edges with different evidence/states are not
   duplicates merely because their endpoints match.
5. Record a confirmed `supersedes` edge from the canonical node to the duplicate.
   A saved comparison report with original refs/versions and their sources can
   provide the evidence. The edge describes replacement but creates no redirect.
6. Only after verifying content and required-link preservation, mark the
   duplicate `superseded`, retaining its text and sources. Add an explicit
   canonical ref to its summary; do not rewrite its original history. Add
   appropriate old names as aliases of the canonical card without conflating meanings.

There are currently no automatic redirects. `get OLD_ID` reads the old card,
while ordinary `recall OLD_ID` may stop returning it after supersession. An edge
to a superseded endpoint is also hidden from the ordinary graph. Check original
queries and incoming references; keep important stable IDs active if moving
breaks workflows. Do not conceal that breakage with a claim that history remains.

A batch in `entry put`, `entity put` or `edge put` is atomic within its own
command; multiple commands are not one transaction. On failure, leave the old
knowledge active if the replacement is incomplete and report completed steps.
Do not undo other tasks' concurrent writes by restoring an old backup over the
whole database. For a targeted reversal, read history and apply full payloads
through the CLI with CAS.

<a id="разделение-текущей-задачи-и-подробностей"></a>
## Separating current task state from details

This procedure applies to future explicitly requested cleanup of selected cards.
Diagnostics and rule development do not authorize bulk rewriting of live memory.
Use a separate synthetic database to test the procedure itself. There is no new
split/compact command, automatic splitting or LLM summarization.

The main `task:KEY` remains a coherent current description: decision and scope,
reason for the choice, material conditions/exceptions, verified facts and exact
implementation state, unknowns and next step. It retains active constraints and
exact refs to detailed cards. The 1000–3000-character guide is not an API limit.
Do not fragment a clear card merely because of its length; headings and paragraph
order are optional and the runtime does not parse them. The separate Templates
pilot is unchanged by this procedure; old summaries need no reformatting.

1. Verify the selected workspace, project and path using [SKILL.md](../SKILL.md).
   Read the parent's full payload and required incoming/outgoing links with
   appropriate state flags, check coverage and retain `updated_at`. Before
   changing live memory, run `richi backup` as described above. For the selected
   group, use `sources snapshot/check` if needed; a manifest does not replace CAS.
2. Create a map of `claim / condition / number / rejection reason → field or
   stable ID → source`. Include exceptions, unknowns, observation dates, scope
   and the implemented/merged/released distinction. Record queries in advance
   for Jira/ID, a unique rejection reason, investigation detail, active constraint,
   historical measurement and hypothesis, with flags and a reasonable `limit`.
   This prevents changed expectations after the move from hiding lost knowledge.
3. Create or verify detailed records with stable IDs. Write a new ID with
   `expected_updated_at: null`: this expects absence and does not authorize
   replacing someone else's card. An `investigation` contains the question,
   conclusion, boundaries and sources; an `experiment` contains conditions,
   method, result and limitations; a `decision` contains the choice or rejection,
   rationale, applicability and alternatives. Extract independently useful
   material, not every line. Do not copy the whole long text into several new
   summaries. Each record is self-contained, with actual `project_ids`, sources
   and its own knowledge/work states. If it belongs to a Jira task, retain its
   actual `jira_key`; do not invent aliases or tags to force exact-key retrieval.
   Read the saved records in full.
4. Create or verify stable, justified `part_of` edges from detailed records to
   the task, with a source and explanation. Also use `expected_updated_at: null`
   for a new edge. `supports`/`informs` are appropriate when their meaning is
   supported. Do not add reverse edges for navigation or duplicate memberships
   from `project_ids`. Before shortening the parent, verify links and retrieval
   through original unique details; search hypotheses with an explicit flag.
5. Replace the parent's summary only as the final write, using its **original**
   `expected_updated_at`. Preserve its ID, Jira, memberships, important aliases,
   all historical sources and active constraints. Provenance cleanup is outside
   this move. Replace outdated current state while retaining useful earlier
   results in current detailed records. Parent history also keeps the previous
   full text, but ordinary search does not index it.
6. On a CAS conflict, reread the parent and reconcile new information; do not
   retry a stale payload over a concurrent edit. With a partial failure before
   the last step, the original text remains in the parent. Record created IDs,
   versions and completed steps, then continue from them. On retry, read existing
   objects, check expected content and reuse them without extra records, edges
   or revisions. For a required edit of an existing object, use its current
   `expected_updated_at`; an accidental ID collision does not authorize replacement.
   Do not restore the entire store from backup over other people's changes.
7. Read the complete result and parent history, full child records and links.
   Compare the meaning-and-source preservation map. The exact command
   `entry get task:KEY` must still read the same ID; a general Jira query at the
   predetermined `limit` must include the task but need not rank it first.
   Check retrieval of rejection reasons and unique details, states, historical
   dates and memberships. Measure the main card's length and necessary read
   volume for the selected scenario; do not call this proof of time/token savings
   or better model decisions.

An observed unsuccessful experiment can remain `confirmed` under its conditions;
a rejected decision does not automatically become `superseded`. An unverified
alternative remains `hypothesis`, and a historical measurement stays historical.
Moving or editing text does not itself advance `verified_at`. A sequence of
entry/edge put commands is not atomic: children and links are checked before
writing the parent, rather than protected by a shared transaction.

`entry read`, `recall` and `brief` bound output: even a short current card can be
truncated. Check excerpt/truncated, omissions, `read_more` and budget. Sentence
selection does not guarantee every qualification survives. For example, an
excerpt saying “the cache is enabled” cannot justify disabling a check when the
condition “only for idempotent reads” was omitted. Before a condition-dependent
action, read the relevant complete `get` results.

<a id="проверка-и-отчёт"></a>
## Verification and report

- Run `check` and read changed cards/links in full.
- Repeat original topic/synonym/Jira queries: required knowledge remains
  accessible, sources and limitations survive, and distinct meanings remain distinct.
- Historical cards remain readable through `get` and the appropriate state flags.
- If search code changed, use [EVALUATION.md](EVALUATION.md). Ordinary cleanup
  needs checks of affected cases; do not change old test expectations to hide
  lost knowledge.
- Report scope and coverage, backup, groups before/after, link edits, queries
  checked, disputed cases left untouched and remaining work. Card count alone
  does not establish improvement. If no suitable changes exist, say so.

For substantive changes, save one maintenance summary linking the report and
affected refs. Do not create a record for every checked card or a separate
“successful run” when nothing useful changed.

This workflow preserves data logically: confirmed duplicates leave ordinary
results but remain readable with history. The CLI provides no physical deletion.
Do not bypass it with direct SQL, delete history, sources, attachments, backups
or WAL/SHM, or run VACUUM, migrations or index rebuilds as ritual “optimization”
without a separate technical reason.
