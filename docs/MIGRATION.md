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
2. Record the existing database path. Use an ordinary terminal without
   `RICHI_DB`, `RICHI_CONFIG` or `RICHI_DATA_DIR` for the migration commands.
   The explicit `-w default` allows a database path for these commands even
   when the terminal normally selects a named workspace.

   ```sh
   richi -w default --db /path/to/old-data/memory.sqlite3 check
   richi -w default --db /path/to/old-data/memory.sqlite3 project list
   richi -w default --db /path/to/old-data/memory.sqlite3 recall 'known decision'
   ```

3. Save the previous config, agent instructions and shortcut for diagnostics.
   Record the project list and several known entries to verify after migration.
4. Pause tasks writing to the old database and stop map servers using it. Do not
   let the old and new locations become two independently maintained copies of
   memory.

<a id="перенос-данных"></a>
## Moving data

Choose the destination before copying the database.

- For `default`, choose a permanent directory outside the source checkout, the
  Python environment and other workspaces. Create the directory if needed.
- For a named workspace, run `richi workspace create NAME` if it is new, then
  `richi -w NAME workspace current`. Use the reported `database` path as the
  destination. It must still be unused (`initialized: false`). The backup
  creates the database, so skip `init`.

Replace the example paths below with the source file and your chosen destination.

```sh
richi -w default --db /path/to/old-data/memory.sqlite3 backup \
  --output /path/to/richi-data/memory.sqlite3
richi -w default --db /path/to/richi-data/memory.sqlite3 check
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

1. For `default`, use `richi -w default workspace current` to find its registered
   `config_file` and set the `database` field there to the new absolute path.
   Create the JSON file and its parent directory if needed. Preserve other
   settings in an existing file. A named workspace already points to its assigned
   database. Remove direct storage overrides from the agent environment too.
2. For Codex, Claude Code and Cursor, follow the [managed setup](SETUP.md) to
   install or adopt the skill. For a manually integrated client, preserve any
   local additions, copy the `skills/richi` directory into the client's skills folder,
   and move your old `project-memory` copy outside that folder. Update any project
   instructions that explicitly invoke the skill by its old name.
   Replace calls to old `memory.py`,
   `serve.py` and `launch_map.py` scripts in project instructions with direct
   `richi` commands. Preserve write permissions and source-verification rules.
3. In a terminal, run `richi -w NAME config show` and `richi -w NAME check`,
   replacing `NAME` with the destination workspace. In a new chat, choose that
   workspace and check the same database with `richi config show` and
   `richi check`. In Cursor, pass the exact `--chat` from this chat's
   `sessionStart` hook immediately after `richi` on every call.
4. Point the map shortcut to `richi -w NAME map` for that workspace. A GUI
   shortcut may need the absolute path to `richi` if it has a different PATH.
5. Repeat known queries, compare projects and entries, and open the map, sources,
   history and diffs. Confirm that the new server uses the new database.
6. Remove previous executable scripts and wrappers from active paths. You can
   retain an archive of the original tool as local historical material. Update
   instructions in open chats or explicitly tell them to use installed `richi`;
   old calls are no longer redirected.

If a registered project's checkout moves, update the complete `project` object
with `project upsert`, preserving the other fields. Independent clones are not
automatically treated as the same repository. See the
[Git reference](../skills/richi/references/GIT.md).

<a id="схема-базы-и-исправление-ошибок-переноса"></a>
## Database schema and migration recovery

Changing the invocation method or switching `dev` does not alter the SQLite
schema, selected database or Git-source format. Run `richi config set dev false`
to return to installed code; restoring an older database copy is unnecessary.
Richi continues reading existing knowledge, including previously recorded `git:`
references.
If `check` reports an unknown schema, preserve the database and investigate the
version it reports. To upgrade schema 1 or 2 to schema 3, take a backup and run
`richi migrate` on the intended workspace. See the
[graph reference](../skills/richi/references/GRAPH.md).

Before writes resume, you can return to the original workspace or restore the
previous `database` setting in `default`. After new entries appear in the new store,
restoring the old copy over it would lose other agents' work. First stop writes
and preserve both databases, then investigate differences through normal commands
and history. Use `expected_updated_at` for targeted record corrections. An old
archive must not become the working store again because of a forgotten shortcut
or instruction.
