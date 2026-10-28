<a id="граф-знаний-схема-2"></a>
# Knowledge graph: schema 2

English | [Русский](GRAPH.ru.md)

The graph extends existing memory. Projects and entries stay in their own tables;
`graph_nodes` stores only references to them. Create an entity when an object—a
PR, metric, component, concept, document or Jira task—needs its own card. If a task
already exists as an entry, use that node; no duplicate `jira` entity is needed.

Node references have a strict format:

| Node | Reference |
|---|---|
| Project `service-example` | `project:service-example` |
| Entry `task:ABC-123` | `entry:task:ABC-123` |
| Entity `pr:example-42` | `entity:pr:example-42` |

The identifier after the first colon is preserved in full. Project and entry
content is not copied into the graph. INSERT triggers create references for new
records, including records added by older CLI commands. Foreign keys prevent
edges from referring to missing nodes.

<a id="переход-с-версии-1"></a>
## Migrating from version 1

After installing Richi, from any working directory:

```sh
richi backup
richi migrate
richi check
```

`migrate` performs an explicit atomic 1→2 migration: it checks the source
database, adds tables/indexes/triggers, creates references to existing projects
and entries, then changes `user_version`. Old tables, records, history and FTS
remain in place. An error rolls back the entire transaction. Unknown versions
are rejected; repeating the command on version 2 returns `existing`.

Older read/write commands support versions 1 and 2. Graph commands require
version 2 and never migrate automatically. `init` creates a new version 2 database;
on an existing version 1 database it asks you to run `migrate`. A backup can be
made before migration. Migration acquires SQLite's write lock, so concurrent
writers wait for its release within their timeout.

`schema.sql` contains the shared version 1 schema; `graph_schema.sql` contains
the additive extension. `init` and `migrate` apply the extension in one
transaction. Do not run these SQL files manually over a live database.

<a id="сущности"></a>
## Entities

```sh
richi entity put --json - <<'JSON'
{
  "id": "pr:example-42",
  "kind": "pull_request",
  "title": "PR 42: кеш повторяющихся запросов",
  "summary": "Изменение ожидает ревью; это ещё не означает выпуск в production.",
  "sources": [{"reference": "https://example.com/repo/pull/42", "type": "pull_request"}],
  "knowledge_state": "confirmed",
  "verified_at": "2026-09-30",
  "tags": ["cache"],
  "aliases": ["кеширование", "кэширование"]
}
JSON

richi entity get pr:example-42
```

Required fields are `id`, `kind`, `title`, `summary`. Allowed kinds:
`pull_request`, `metric`, `component`, `concept`, `document`, `jira`. Other fields
are `sources`, `knowledge_state` (default `hypothesis`), `verified_at`, `tags`,
`aliases`, `expected_updated_at`.

Sources use the same format as entries: an array of objects with required
`reference` and optional `label`, `type`, `observed_at`. Every new `confirmed`
entity requires at least one source. This checks that a reference exists;
it does not automatically verify claims.

<a id="рёбра"></a>
## Edges

`kind: concept` concepts also appear in the Encyclopedia section. Their direct
project scope is defined by a confirmed `used_in` edge from concept to project,
with a source. It is not inferred from graph connectivity. Definition format,
same-named meanings and versions are described in [ENCYCLOPEDIA.md](ENCYCLOPEDIA.md).

```sh
richi edge put --json - <<'JSON'
{
  "id": "pr42-implements-abc123",
  "from_ref": "entity:pr:example-42",
  "to_ref": "entry:task:ABC-123",
  "kind": "implements",
  "description": "PR содержит реализацию задачи; состояние выпуска проверяется отдельно.",
  "sources": [{"reference": "https://example.com/repo/pull/42"}],
  "knowledge_state": "confirmed",
  "verified_at": "2026-09-30"
}
JSON

richi edge get pr42-implements-abc123
```

All example objects and references are synthetic; endpoints must be created
first. Required fields are `id`, `from_ref`, `to_ref`, `kind`. `kind` is a free
string, for example `implements`, `measures`, `depends_on`, `documents`,
`supersedes`. Additional fields are `description`, `sources`, `knowledge_state`,
`verified_at`, `expected_updated_at`. `confirmed` requires a source. Direction
matters to the relationship's meaning; neighbor traversal follows both directions.

`entity put` and `edge put` accept a single object or array and fully replace
fields by ID, like `entry put`. A batch is atomic. `get` returns the complete
current card; `get ID --history` additionally returns immutable `before`/`after`
history. For updates, pass `expected_updated_at` from the latest read: a stale
time rejects the change. `null` means the card is expected to be absent. An
identical normalized record returns `unchanged` without changing time or history.
The new entity/edge commands do not accept `event_id`; content provides retry
idempotency, while `expected_updated_at` protects against stale updates.

Existing `entry_projects` and `relations` appear automatically. An entry's
project link is named `belongs_to_project`: it expresses entry membership, not
a claim of technical or business impact. Edit these edges through
`entry put project_ids`; edit project relationships through `relation put`.
The API rejects a manual duplicate of an existing derived relationship. If a
derived relationship was created after a manual one, export preserves both with
different `id` and `origin`: their evidence and confidence can differ, so hiding
one merely because direction and kind match would be incorrect.

<a id="задача-и-подробные-записи"></a>
## Tasks and detailed records

A stable task entry contains current state. Independent investigations,
experiments and decision rationales use the existing entry kinds
`investigation`, `experiment`, `decision`; no separate investigation entity is
needed. Search indexes their current text, not the parent's older revisions.
Each record keeps its own sources, knowledge/work state, actual `project_ids`
and actual `jira_key` when it belongs to a Jira task.

The preferred relationship direction is detailed record → task:

```json
{
  "id": "pooling-experiment-part-of-abc123",
  "from_ref": "entry:experiment:pooling-abc123",
  "to_ref": "entry:task:ABC-123",
  "kind": "part_of",
  "description": "Эксперимент проверял вариант решения задачи ABC-123; отказ и условия сохранены в его карточке.",
  "sources": [{"reference": "https://example.com/reports/abc123-pooling", "type": "report"}],
  "knowledge_state": "confirmed",
  "expected_updated_at": null
}
```

This is a synthetic payload example for `edge put`; both endpoints must already
exist. A new ID with `expected_updated_at: null` is created only if absent. On
retry, first read and check the existing object, then reuse it or make a justified
edit with its current CAS. Do not submit an old update payload over a concurrent edit.

`part_of` asserts that material belongs to the task, not that the approach
succeeded. A failed outcome can be confirmed under verified conditions; an
untested approach remains a hypothesis. `supports`/`informs` are appropriate
only with a source supporting that meaning. Do not create a reverse edge solely
for navigation: `neighbors` reads both directions. Membership remains defined
by `project_ids`; graph adjacency does not expand strict `punisher --project` scope.

Before shortening the parent, create and verify all required detailed records,
edges and retrieval through original queries. Update the parent last with its
original `expected_updated_at`. Multiple entry/edge put commands do not form
one transaction; a CAS conflict requires rereading and reconciling new information.
The detailed procedure and provenance preservation are in [MAINTENANCE.md](MAINTENANCE.md).

<a id="экспорт-графа"></a>
## Graph export

For one known node, use [NEIGHBORS.md](NEIGHBORS.md).

```sh
richi graph export
richi graph export --project service-example
richi graph export --include-hypotheses
```

By default, export includes only `confirmed` entries, entities and edges.
Projects appear as confirmed items of the local catalog; this does not
additionally verify their descriptions. `--include-hypotheses` adds hypotheses;
`--include-superseded` adds superseded knowledge. The flags are independent.
If a filter excludes an endpoint, its edges are hidden as well.

Without a project filter, export covers all available nodes, starting with
projects. `--project ID` shows the selected project's connected component:
nearer nodes are added before farther ones. This can include several connected projects.

Each export reads one consistent snapshot in a transaction. Limits are 2000
nodes and 10000 edges. When a limit is reached, `truncated` and the corresponding
`limits` fields are set; edges whose endpoints are outside the returned nodes
are not emitted.

JSON result:

```json
{
  "schema_version": 2,
  "generated_at": "2026-09-30T00:00:00Z",
  "nodes": [],
  "edges": [],
  "truncated": false,
  "limits": {
    "max_nodes": 2000,
    "max_edges": 10000,
    "nodes_truncated": false,
    "edges_truncated": false
  },
  "scope": {
    "root": null,
    "depth": null,
    "selection": "all",
    "knowledge_states": ["confirmed"]
  }
}
```

A node contains `id` (the full reference), `native_id`, `node_type` (`project`,
`entry`, `entity`), `kind`, `title`, `summary`, `knowledge_state`, `work_state`,
`project_ids`, `sources`, `tags`, `aliases`, `verified_at`. Entries/entities also
have creation and update times. For entities without a direct canonical
membership, `project_ids` is empty: relationships are available through edges;
no additional memberships are invented.

An edge contains `id`, `from_ref`, `to_ref`, `kind`, `description`,
`knowledge_state`, `sources`, `verified_at`, `origin`, `provenance`. `origin` is
`explicit`, `project_relation` or `entry_project`; `provenance` identifies the
source table and ID. Export identifiers use the `edge:`, `relation:`,
`membership:` prefixes to avoid collisions. Pass the original ID to `edge get`,
without the export-added `edge:` prefix.

The interactive map is a separate view of this data: it draws nodes/edges and
shows sources when an element is selected. Export itself neither opens a server
nor changes knowledge. Visual proximity and a path between nodes do not establish
causality: the edge kind and its source define meaning. Older text `search`
continues searching entries; `recall` searches entries and entities and selects
a bounded neighborhood through explicit links. No separate FTS index for entities
has been added yet. See [RECALL.md](RECALL.md).
