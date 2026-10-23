# Read links around a known record

```sh
richi --compact-json --project-path /work/example graph neighbors \
  entry:task:EXAMPLE-42 --depth 1 --limit 30 --view compact --max-chars 12000
```

Refs are `project:<project-id>`, `entry:<full-entry-id>` or
`entity:<full-entity-id>`; preserve everything after the first colon.
`neighbors` follows incoming and outgoing links. Depth is 1–3 (default 1),
limit is 1–200 nodes including the root (default 200); cycles are deduplicated.
Start with the scope needed for the question. Do not export the whole graph
when a local neighborhood suffices.

Use `--view compact` for bounded reading. Its rendered JSON budget defaults to
16000 characters (allowed 2000–100000), including formatting and final newline;
`--compact-json` leaves more room for data. `--limit` alone bounds node count,
not text size. The default `--view full` preserves full records and rejects
`--max-chars`; `graph export` and map output are unchanged.
The budget bounds response text, not database work or transient memory.

Only confirmed nodes/edges appear by default. Independently add
`--include-hypotheses` and/or `--include-superseded` when needed. Filtered endpoints
hide their edges; a filtered root produces an empty graph, not proof of no links.
Check `truncated`, `limits`, `budget`, excerpts/source omissions and `scope`.
`limits` describes selection caps and disconnected records omitted after an edge
cap; `budget` counts omissions from that connected selection. Counts do not claim
to cover unseen nodes/edges beyond selection caps. Compact output retains the
root and a path to every shown node, but may drop other edges or entire branches.
If a root payload cannot fit, its exact ref/state remain with `payload_omitted`;
repeat `graph neighbors` for `scope.root` with `--view full` when full details
are needed. The chosen depth
is scope, not reported truncation; absence beyond it is not evidence of no relation.

Direction, kind and sources determine meaning. Shared membership or a graph path
does not establish causality. `belongs_to_project` derives from `entry.project_ids`;
its presence is not technical influence. In compact output its `sources_ref`
points to the originating entry instead of duplicating that entry's evidence;
`provenance` preserves the derived origin. Other edges show whole source objects
with omission counts. Read relevant full node/edge payloads using `read_more`
before an update or a condition-dependent conclusion. For an explicit edge,
pass its native ID to `edge get`, removing the export-added `edge:` prefix only.
Other origins (`membership:`, `relation:`) are derived, not `edge get` IDs.
For a project relation, execute `read_more.args`: it uses
`context --project=ID --limit 1 --include-superseded`, then select the exact
relation identified by `read_more.select`. This preserves historical/hypothesis
rows and avoids graph node caps. Its limit bounds entries only; the relation
list is a full read and can be large.

Schema/edge writing, complete graph export, migrations and map rendering:
[GRAPH.md](GRAPH.md). Cleanup mutations: [MAINTENANCE.md](MAINTENANCE.md).
Do not create reverse edges just for navigation or duplicate membership manually.
