<a id="перенос-существующей-памяти-на-richi"></a>
# Migrating existing memory to Richi

English | [Русский](MIGRATION.ru.md)

All access should use the installed `richi` command: terminals, agents and the
map shortcut use one package and one configuration. Wrappers for the former
Python scripts are no longer installed. Migration preserves accumulated
knowledge, history, captured Git bytes and other local materials.

<a id="подготовка"></a>
## Preparation

1. Install Richi normally with `pip install .` in a dedicated Python environment
   using the [installation guide](INSTALLATION.md). If you previously used an
   editable installation, reinstall with `pip install --force-reinstall /path/to/Richi`.
   To develop features, set `development.source` and enable `dev` with
   `richi config set`; the installation remains fixed.
2. Check the existing database at its actual path:

   ```sh
   richi --db /path/to/old-data/memory.sqlite3 check
   richi --db /path/to/old-data/memory.sqlite3 project list
   richi --db /path/to/old-data/memory.sqlite3 recall 'известное решение'
   ```

3. Save the previous config, agent instructions and shortcut for diagnostics.
   Record the project list and several known entries to verify after migration.
4. Pause tasks writing to the old database and stop map servers using it. Do not
   let the old and new locations become two independently maintained copies of
   memory.

<a id="перенос-данных"></a>
## Moving data

Choose a permanent Richi directory outside the checkout and Python environment.
Platform defaults are listed under [storage settings](INSTALLATION.md#selecting-storage).
The following paths are synthetic: replace them with your own and create the
destination directory before running the command.

```sh
richi --db /path/to/old-data/memory.sqlite3 backup \
  --output /path/to/richi-data/memory.sqlite3
richi --db /path/to/richi-data/memory.sqlite3 check
```

`backup` uses SQLite's backup API and does not overwrite an existing file.
Copying only a live `.sqlite3` file through a file manager does not guarantee
consistency under WAL. Keep the source database as a backup until checks finish;
do not move its old `-wal` and `-shm` files to the new database.

Preserve the directory structure of associated data:

- `artifacts/git/` with all captured code bytes;
- existing `backups/`, archives and local reports;
- private evaluation sets, their original snapshots, expectations and results;
- documents, manifests and other materials referenced by sources.

A SQLite backup does not include these files. Compare file lists and hashes after
copying; then verify that several captured Git sources can still be read. Required
Git objects in registered repositories must remain available too. Do not move
PID files, sockets or locks from the old `runtime/`: the new server creates its
own state at startup.

Absolute references to previous local documents may require retaining an archive
at the old path or correcting specific verified references. Do not bulk-rewrite
historical provenance or assign it a new HEAD. Source-check manifests are bound
to the database path: retain the old manifests as history and create a new
baseline with `sources snapshot` for the new location. Do not rewrite private
evaluation snapshots or expectations to obtain passing results; keep the new
execution configuration separately.

<a id="переключение-команды-агента-и-карты"></a>
## Switching the command, agent and map

1. Put the new `database` path in Richi's configuration. Set `data_dir` if needed;
   update or remove `RICHI_DB` and `RICHI_CONFIG` overrides in the shell, IDE and
   agent environment. Configuration does not move data itself.
2. Run `richi config show` and `richi check` from both the terminal and agent
   environment. They must select the same migrated database.
3. Install the entire current `skills/project-memory` directory after preserving
   local additions to the previous skill. Replace calls to old `memory.py`,
   `serve.py` and `launch_map.py` scripts in project instructions with direct
   `richi` commands. Preserve write permissions and source-verification rules.
4. Point the map shortcut to `richi map`. Select development mode with the shared
   `richi config set dev true` after configuring `development.source`; the shortcut
   and agent commands do not change when switching modes. A GUI shortcut may need
   the command's absolute path if it does not inherit the terminal's PATH.
5. Repeat known queries, compare projects and entries, and open the map, sources,
   history and diffs. Confirm that the new server uses the new database.
6. Remove previous executable scripts and wrappers from active paths. You can
   retain an archive of the original tool as local historical material. Update
   instructions in open chats or explicitly tell them to use installed `richi`;
   old calls are no longer redirected.

If a registered project's checkout moves, update the complete `project` object
with `project upsert`, preserving the other fields. Independent clones are not
automatically treated as the same repository. See the
[Git reference](../skills/project-memory/references/GIT.md).

<a id="схема-базы-и-исправление-ошибок-переноса"></a>
## Database schema and migration recovery

Changing the invocation method or switching `dev` does not alter the SQLite
schema, selected database or Git-source format. Run `richi config set dev false`
to return to installed code; restoring an older database copy is unnecessary.
Richi continues reading existing knowledge, including previously recorded `git:`
references. This is data-format support, not support for old executable scripts.
If `check` reports an unknown schema, investigate the cause; do not delete the
database to reinstall. Schema 1 is upgraded to the graph separately, using a
backup and explicit `migrate`, as described in the
[graph reference](../skills/project-memory/references/GRAPH.md).

Before writes resume, an incorrect switch can be reversed by selecting the old
intact database in configuration. After new entries appear in the new store,
restoring the old copy over it would lose other agents' work. First stop writes
and preserve both databases, then investigate differences through normal commands
and history. Use `expected_updated_at` for targeted record corrections. An old
archive must not become the working store again because of a forgotten shortcut
or instruction.
