# Tools catalog

English | [Русский](TOOLS.ru.md)

The **Tools** tab sits alongside the map and Encyclopedia. Open a tool to read
its purpose and practical guidance, select a project, then inspect only tasks
with recorded evidence of using that tool. Reusable scenarios without a task
are shown separately. A project association alone does not attach every task
in that project to the tool.

The catalog loads short descriptions and project/usage counts. Full instructions,
sources and related tasks load when a tool is selected; an unloaded task count
does not mean there are no tasks.

Tools can be linters, profilers, test clients or internal utilities. A single
scenario can combine several tools and cover several projects. The tool card
holds common guidance; the scenario holds contextual configuration; a dated
experiment or investigation holds a particular attempt and its result.
Definitions remain in the Encyclopedia when independently useful.

The catalog uses the same SQLite store, sources and history. `tool` entities
require schema 3. Existing stores need an explicit backup and migration; reading
the catalog or switching runtime never migrates them automatically. See
[graph and migration](../skills/project-memory/references/GRAPH.md).

The complete CLI and agent authoring procedure is
[Tools and their use](../skills/project-memory/references/TOOLS.md). It covers
canonical link directions, scope, historical outcomes and conflict protection.
There is no automatic import from repositories, tasks or another workspace.
