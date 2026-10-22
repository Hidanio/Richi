# Audit selected memory without changing it

Use for an explicit cleanup, consolidation or audit request. Stay within the
requested project/topic/records; ordinary project work does not call for a
whole-store review. An audit-only request authorizes reading and recommendations,
not writes. Actual cleanup authorizes justified reversible edits in its scope;
use [MAINTENANCE.md](MAINTENANCE.md) before applying them. An audit needs no backup.

## Select and read candidates

When the user names records, read those full `entry/entity/edge get` payloads
directly. Add history only when prior values or chronology matter. When candidates
are not known, start with:

```sh
richi --compact-json --project-path /work/example punisher \
  --project service-example --limit 20 --max-chars 16000
```

Omit project/path only for an intentionally workspace-wide review. Punisher
returns SQLite health and review candidates without changing anything. Its
project scope is strict (entry memberships and direct sourced confirmed
`used_in` from entity to project). `recall --project` boosts relevance;
`graph export --project` is a connected component that can cross projects.
Neither expands authorization to edit other knowledge.

Findings such as duplicate text, shared aliases, missing dates/sources, old
verification or long summaries are prompts to inspect, not proven defects.
`--summary-warn-chars` defaults to 6000; length alone does not establish mixed
history, poor quality or a need to split. For exact finding contracts and scan
caps see [DIAGNOSTICS.md](DIAGNOSTICS.md#кандидаты-для-ручного-обслуживания).

Check `coverage.complete`, `truncated`, `budget`, omitted findings and graph
limits before claiming coverage. Work in small groups; read full candidate
payloads and relevant incident links via [NEIGHBORS.md](NEIGHBORS.md). A bounded
report does not prove no other candidates exist. For full-scope review, narrow
or partition the scope, or enumerate metadata from an authorized consistent
read-only snapshot; do not dump the whole database into model context.

Check external sources only where needed for a conclusion. Missing files are
source gaps, not automatic disproof of historical observations. Prefer an
already known stable replacement; do not claim unread evidence was checked.

## Decide whether content should be combined

| Finding | Decision |
|---|---|
| Same meaning, scope/version and duplicated or complementary content | Propose one canonical record preserving every useful claim and its provenance |
| Investigation and a definition derived from it | Retain both roles; link if supported |
| Different contracts, versions, meanings or experimental conditions | Keep separate; add only evidence-backed relations |
| Conflicting results | Keep both with their conditions and sources; do not average them |
| Task with useful independent details | Preserve stable task ID; consider a split only if retrieval and conditions can be preserved |
| Repetitive prose | Remove repetition only, preserving claims, numbers, conditions and limitations |
| Unclear value or uncertain explanation | Leave unchanged and identify the uncertainty |

A common tag, Jira, alias, source or graph path is not evidence of duplication
or causality. Prefer stable IDs; a short Jira task must not be absorbed into an
investigation or concept merely because it is short. Scoped term variants:
[ENCYCLOPEDIA.md](ENCYCLOPEDIA.md).

Preserve unsuccessful experiments, reasons for rejecting approaches, historical
measurements and reproduction conditions. Keep hypothesis/confirmed separate;
editing does not refresh verification or promote confidence. Implemented,
merged and released remain distinct. A later result does not automatically
supersede an earlier result in different conditions. Supersession is justified
only after a real replacement preserves useful content and remains discoverable.

Report the reviewed scope, evidence, coverage limits, recommended changes and
cases left separate/unresolved. If no change is justified, say so. Card count,
SQLite size and shorter text alone do not prove improved usefulness.
