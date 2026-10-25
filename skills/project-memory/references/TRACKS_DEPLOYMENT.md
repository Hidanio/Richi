# Enable schema-3 research after acceptance

This is an explicit deployment procedure, separate from ordinary research use.
Development/testing a branch does not authorize executing it against live memory.
Keep implementation, merge and deployed states distinct.

## Inspect the selected environment

Follow the chat binding procedure from [SKILL.md](../SKILL.md), then run:

```sh
richi chat current
richi workspace list
richi config show
richi project check /path/to/accepted/Richi --id richi
```

Use the confirmed bound workspace, configured store and accepted checkout. Do not
run global `richi use`, override storage to bypass binding, or select another
workspace to make a command work. Existing schema 1/2 stores remain readable by
the candidate's legacy commands; research operations require explicit migration.

## Verify an isolated installation first

Create a disposable virtual environment outside live runtime directories and
install the exact accepted checkout normally (not editable):

```sh
python3 -m venv /tmp/richi-acceptance-venv
/tmp/richi-acceptance-venv/bin/python -m pip install --upgrade pip
/tmp/richi-acceptance-venv/bin/python -m pip install /path/to/accepted/Richi
/tmp/richi-acceptance-venv/bin/python -I -m richi capabilities describe 'task context'
```

Run the repository tests/evaluations with this installed Python and their
synthetic fixtures. To test a copied real store, obtain task authorization,
use SQLite backup rather than copying an open database file, preserve the
corresponding `artifacts/git/`, and keep the copy private. Do not use an unbound
copy or `--db` to circumvent the active chat's workspace selection. For a fully
synthetic integration fixture, a synthetic home/registry/chat ID is test setup,
not an operational workaround for a real chat.

## Back up, install and migrate the authorized workspace

After acceptance and authorization to deploy, stop writers and map processes for
the selected store. Run `richi backup`; record its verified output path. Preserve
`artifacts/git/` and referenced Git objects separately because SQLite backup does
not contain captured Git bytes. Keep the pre-migration backup unchanged.

Install the accepted revision in the configured fixed runtime environment using
that environment's Python:

```sh
/path/to/configured/venv/bin/python -m pip install /path/to/accepted/Richi
richi config set dev false
richi config show
richi migrate
richi check
richi capabilities list --group research
```

If deployment deliberately uses development mode instead, configure the accepted
checkout with `richi config set development.source /path/to/accepted/Richi` and
`richi config set dev true`, then verify `config show` before migration. These
commands change runtime selection and must not be run merely to read memory.
Source edits in dev affect new CLI calls and the map, so use a stable accepted
checkout. Launcher changes require reinstalling the package.

`migrate` upgrades schema 1 through 2 to 3, or 2 to 3, in one transaction. Schema 3
adds typed research, integer revisions, a notification journal and request
receipts; existing canonical records, history and FTS remain. An error rolls
back the migration. Repeating it on schema 3 is a no-op. Selecting the runtime,
read commands and `search-index build` do not migrate. New `init` creates schema 3.

Copy the entire accepted `skills/project-memory/` source directory to the agent's
skill location only as part of deployment, preserving its `references/` files.
Do not edit just the installed SKILL.md or mix references from different releases.
Refresh any open map after interface/runtime changes. Existing map cards keep
their entry kind; roles do not require a new frontend.

Verify an existing legacy card, a standalone task context, `check`, and catalog
syntax. Creating production test tracks is unnecessary; use accepted real work
only when a write is authorized. Record the deployed runtime commit, workspace,
schema version and backup location privately. Do not infer deployment from merge.

## Rollback boundary

There is no schema downgrade command. Old runtimes reject schema 3 rather than
silently removing typed fields. Switching runtime alone cannot roll back the
database. Stop all users of the store before an authorized restoration of its
verified pre-migration backup and matching artifacts. Writes made after migration
are absent from that backup; preserve them before any restoration. Do not replace
the live database or discard new knowledge as an automatic recovery action.
