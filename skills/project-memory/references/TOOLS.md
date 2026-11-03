# Tools and their use

English | [Русский](TOOLS.ru.md)

Use this procedure for linters, test clients, profiling utilities and other
tools whose practical use should be reusable across tasks and projects. Follow
the bound workspace and repository guard in [SKILL.md](../SKILL.md). Tool cards
use `entity.kind: tool` and require schema 3; migration is explicit, as described
in [GRAPH.md](GRAPH.md). Reading or selecting a runtime never migrates a store.

## Choose the record

| Knowledge | Record |
|---|---|
| What an instrument does, basic commands, general limitations | `tool` entity |
| Definition of a concept such as linting or sampling | `concept` entity, if independently useful |
| A repeatable recipe, project configuration or scenario using several tools | `procedure` entry |
| A particular attempt, its parameters, observations and outcome | `experiment` or `investigation` entry |
| Current goal and decision for the surrounding work | `task` entry |

A tool does not require a duplicate concept. A procedure can use several tools
and belong to several projects through its actual `project_ids`. Keep one shared
recipe when its conditions are the same; separate incompatible configurations
with explicit scope. There is no separate profile or run type.

## Tool card and practical details

Prefer a stable ID such as `tool:tracekit`. Put its purpose first in `summary`,
then include useful commands, inputs/outputs, configuration references and
limitations. These are ordinary summary sections, not parsed fields. Add observed
names to `aliases`; do not invent synonyms merely to broaden search.

Put project-specific commands, configuration paths, versions, environment and
roles into the procedure. Link the authoritative configuration file rather than
copying the whole file into memory. A recipe using two projects should explain
which is the target and which supplies the test environment. Put a particular
run's date, candidate, parameters, result and artifact references into its
experiment/investigation. `work_state: done` means the work ended; it does not
mean the candidate passed or was adopted. State the actual outcome in `summary`.

Version-specific CLI behavior belongs with its version and verified conditions;
an observed exception must not become an unrestricted tool property. Keep parallel
recipes and historical results searchable. A newer record for a different
candidate does not automatically supersede an older baseline. `verified_at` is
the evidence-check date, distinct from a run date or release date.

## Canonical usage links

```text
entry:procedure:example-check --uses_tool--> entity:tool:tracekit
entry:procedure:example-check --uses_tool--> entity:tool:benchkit
entry:experiment:example-attempt --uses_tool--> entity:tool:benchkit
entry:experiment:example-attempt --part_of--> entry:task:EXAMPLE-42
entity:tool:benchkit --used_in--> project:service-example
```

The IDs are synthetic. Each edge needs a meaningful description and checked
source. Usage is established by a **confirmed, sourced `uses_tool` edge from an
entry to a tool**. A direct task link is allowed when the task itself provides
the evidence. A linked detail may lead to its task through a confirmed, sourced
`part_of` edge in the direction shown. Record actual project memberships on the
entries; neither a shared tag, the same Jira key nor arbitrary graph proximity
proves usage. For a child-linked task to appear under a project, both the usage
entry and task must belong to that project; a parent's membership does not
silently broaden the child's scope.

The optional `tool --used_in--> project` edge asserts scoped use, such as
testing that project. It does not establish a runtime dependency, attach every
project task to the tool or prove that any particular task used it. Describe
the role and conditions in the edge. Use `depends_on` only when dependency is
itself supported by evidence.

The Tools view follows tool → project → related tasks. It includes tasks only
through their explicit tool-use evidence or linked details, within the selected
project. It shows standalone scenarios separately. A project with only `used_in`
evidence may have no linked tasks; this is incomplete task evidence, not a reason
to enumerate every task in that project. Unknown project scope stays unknown.

## Read and write

```sh
richi --compact-json tools list --limit 100
richi --compact-json tools get tool:tracekit
richi --compact-json --project-path /work/example entity get tool:tracekit
```

`tools list` returns lightweight tool cards and project/usage metadata. Its tool
limit is 1–200, default 100. Summaries are at most 1000 characters, with
`summary_is_excerpt`; sources are omitted with an explicit `sources_omitted`
count. Usage bodies, task ancestry and history are not loaded for the catalog.
Project cards contain IDs, names and navigation metadata; their full descriptions,
repository paths and sources are loaded only by `tools get`.
`detail` is false and task counts are `null`, meaning “not loaded,” not zero.
Follow `read_more` or call `tools get ID` for a native tool entity ID.

`tools get` returns the full current tool card and a top-level `records` map
keyed by qualified entry refs. Tool/project `records` arrays contain refs into
that map. Project task links contain `ref`, `usage_refs` and `usage_paths`, so
shared task and usage payloads are returned once. Task counts are calculated
in this detailed view. Usage records include their sourced `uses_tool` evidence;
`usage_paths` preserve `part_of` edge refs for separate `edge get` reads. Neither
command loads revision history; request `entity get ID --history` or
`entry get ID --history` when chronology is needed.

Both commands accept `--include-hypotheses` and `--include-superseded`; keep the
same state selection when opening detail. These flags do not turn an unconfirmed
or unsourced edge into usage evidence. Each call reads one snapshot; separate
list/detail calls may observe intervening writes. Limits are 200 usage records,
200 distinct tasks and 100 distinct projects per tool, and 200 distinct visited
ancestors per usage record while following `part_of`; the usage record itself
does not consume this ancestor limit. Parallel evidence edges do not consume
additional project or ancestor slots. Detailed records include at most 20
`uses_tool` evidence edges each. Project cards include at most 20 direct
`used_in` evidence refs each, reported as `limits.evidence_edges_per_project`.
Both evidence caps expose `usage_evidence_truncated`, which propagates to the
overall truncation flag.
Check `truncated` and `limits` before treating project/task/usage counts as totals;
they describe the returned selection when truncated. `total_tools` counts all
tools matching the selected state and ID filters.

The Tools tab reads a catalog of at most 200 tools independently of the map's
graph export, then loads detail when a tool is selected. `/api/tools?tool=ID`
serves that detail from the same scoped, read-only worker path. The interface
shows a completeness warning when a result is truncated. Schema 2 remains
readable, with an empty tool catalog until explicitly migrated and populated.

Search existing cards before creating one. Use names and aliases actually seen
in sources; general recall searches tools along with other entities. For a known
card, `entity get tool:tracekit` returns its full payload; use `entry get ID` and
`edge get ID` for its recipes, runs and usage evidence. Read the conditions and
sources before reusing a command or comparing results. The catalog and graph are
navigation views; a short description is not a complete evidence record.

For a new card and its edges, use `expected_updated_at: null`. For an update,
read the full current payload first, preserve unrelated fields and source
metadata, remove `created_at`, `updated_at`, `history`, and pass the retrieved
`updated_at` as `expected_updated_at`. On conflict reread and reconcile. Verify
each write with a full get. Entity/edge fields and batch contracts are in
[GRAPH.md](GRAPH.md); entry fields are in [ENTRY_WRITE.md](ENTRY_WRITE.md).

When separating an existing concept or long task, use
[MAINTENANCE.md](MAINTENANCE.md). Keep stable IDs and history where the object
remains the same; preserve contextual knowledge and all supporting sources.
Create and verify necessary procedure/run records and links before shortening
the original. Do not automatically reclassify all concepts mentioning a tool,
and do not copy another workspace's knowledge without an explicit request.

## Synthetic example

`TraceKit` describes a fictional tracing client; `BenchKit` describes a fictional
load client. A procedure “Compare cache candidates” uses both tools and belongs
to `service-example` and `test-sandbox`. Its summary specifies their roles,
configuration files and repeatable commands. An experiment records one dated
attempt, its observed result and artifacts, and links to `task:EXAMPLE-42` with
`part_of` and to the tools it actually used with `uses_tool`.

Opening BenchKit under `service-example` can expose EXAMPLE-42 through that
experiment. Another task in `service-example` remains absent without its own
tool-use evidence. “The attempt completed” and “the candidate passed” remain
separate claims. These names and circumstances illustrate the model; they are
not preloaded knowledge or evidence about real tools.
