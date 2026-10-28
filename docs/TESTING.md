# Try Richi with colleagues

English | [Русский](TESTING.ru.md)

This guide targets local Codex on macOS or Linux and the corrected stable
release **1.0.1**. Start after its [release workflow](https://github.com/Hidanio/Richi/actions/workflows/release.yml)
has finished successfully and the [release page](https://github.com/Hidanio/Richi/releases/tag/v1.0.1)
contains the wheel, `richi-release.json`, `richi-installer.pyz`, its SHA256 file,
and `install.sh`. The earlier empty `v1.0.0` release cannot install or update Richi.

## Install and connect Codex

You need Python 3.9+ with `sqlite3`, `venv` and `ensurepip`, plus `curl` and network
access. On Debian/Ubuntu a missing `venv` may require `python3-venv`.
In an ordinary terminal, download the official installer wrapper and run it:

```sh
(
  set -eu
  richi_install_script=$(mktemp)
  trap 'rm -f "$richi_install_script"' EXIT HUP INT TERM
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    https://github.com/Hidanio/Richi/releases/latest/download/install.sh \
    -o "$richi_install_script"
  sh "$richi_install_script" --yes
)
```

This installs the latest stable release in a dedicated environment and configures
the command, Project Memory skill, and global Codex instructions for this OS user.
It does not import old conversations or create a workspace database. If an existing
command or custom skill conflicts, stop and follow [setup recovery](SETUP.md);
do not overwrite an old installation or add `--replace-skill` blindly.

Open a **new terminal and new Codex chat**. In the terminal, check:

```sh
command -v richi
richi --version
richi setup --status
richi update check
```

Expect `Richi 1.0.1` (or a later stable version), setup `configured`, and an update
check without errors. If PATH has not refreshed, use `~/.local/bin/richi`.
Existing managed installations use `richi update check` then `richi update apply`
to update; workspace dev mode can still select local sources. See [updates](UPDATES.md).
An older bootstrap without setup support needs the separate-environment procedure
in [setup](SETUP.md). Do not use pip to overwrite a managed staged installation.

## Check memory between two chats

Open a new projectless Codex chat and send:

> Use only workspace `richi_test` for this chat. Create it if needed, bind this
> chat to it and initialize its empty store if necessary. Use the installed
> release (`dev false`) in this workspace. Do not read other workspaces. Create
> synthetic project `test_demo` without a repository path. Save a confirmed note
> `note:orion-test` titled “Orion test”: “Synthetic test: Orion uses a 30-second
> timeout. This is not production.” Assign it to `test_demo` and cite the source
> as manually supplied synthetic test data. Show the workspace and saved ID.

If `richi_test` already contains unrelated work, choose another test name and use
it consistently. Explicit workspace selection is required in each new chat.
Then, in an ordinary terminal:

```sh
richi -w richi_test check
richi -w richi_test entry get note:orion-test
richi -w richi_test recall Orion --project test_demo
richi -w richi_test map
```

Expect a valid database and the note in both recall and the map. In a **second
new chat**, explicitly choose `richi_test` and ask the agent to find and read the
Orion note, report its timeout and whether it describes production, without
changing memory. Expect 30 seconds and a clear statement that it is synthetic.
The test checks persistence; Richi does not import conversation history.

## Send feedback

Include OS, Python version, `richi --version`, setup status, the failing step,
expected/actual behavior and a minimal synthetic reproduction. Mention whether
the second chat successfully used the saved note. Remove private paths and
content; do not send databases, setup backups or personal logs.
