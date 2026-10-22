# Read links around a known record

```sh
richi --compact-json --project-path /work/example graph neighbors \
  entry:task:EXAMPLE-42 --depth 1 --limit 30
```

Refs are `project:<project-id>`, `entry:<full-entry-id>` or
`entity:<full-entity-id>`; preserve everything after the first colon.
`neighbors` follows incoming and outgoing links. Depth is 1–3 (default 1),
limit is 1–200 nodes including the root (default 200); cycles are deduplicated.
Start with the scope needed for the question. Do not export the whole graph
when a local neighborhood suffices.

Only confirmed nodes/edges appear by default. Independently add
`--include-hypotheses` and/or `--include-superseded` when needed. Filtered endpoints
hide their edges; a filtered root produces an empty graph, not proof of no links.
Check `truncated`, `limits`, nodes/edges omissions and `scope`. The chosen depth
is scope, not reported truncation; absence beyond it is not evidence of no relation.

Direction, kind and sources determine meaning. Shared membership or a graph path
does not establish causality. `belongs_to_project` derives from `entry.project_ids`;
its presence is not technical influence. Read relevant full node/edge payloads
before an update or a condition-dependent conclusion. For an explicit edge,
pass its native ID to `edge get`, removing the export-added `edge:` prefix only.
Other origins (`membership:`, `relation:`) are derived, not `edge get` IDs.

Schema/edge writing, complete graph export, migrations and map rendering:
[GRAPH.md](GRAPH.md). Cleanup mutations: [MAINTENANCE.md](MAINTENANCE.md).
Do not create reverse edges just for navigation or duplicate membership manually.
