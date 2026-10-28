<a id="установка-одной-командой"></a>
# One-command installation

English | [Русский](SETUP.ru.md)

For **macOS/Linux with Python 3.9+, curl, sqlite3, venv and ensurepip**:

```sh
curl -fsSL --proto '=https' --proto-redir '=https' https://github.com/Hidanio/Richi/releases/latest/download/install.sh | sh -s -- --yes
```

This URL becomes available after a stable release containing the installer is
published; merging the code alone does not publish it. On Debian/Ubuntu, Python
may also need `python3-venv`. The installer explains missing prerequisites and
never invokes sudo or installs system Python. Git is needed for repository history.

The command installs a fixed release in `~/.local/share/richi/venv`, adds
`~/.local/bin/richi`, installs Project Memory in `~/.agents/skills/`, and adds
a managed block to the effective global Codex instructions. Existing instruction
bytes, permissions, data and workspace settings are preserved. Without `--yes`,
the installer displays its plan and asks for confirmation in a terminal.

Open a **new Codex chat** so it discovers the skill and instructions. Open a new
terminal for PATH changes, or use `~/.local/bin/richi` immediately. The first chat
asks which workspace to use and binds that choice. It initializes an empty
workspace only after selection. Installation never reads, creates, migrates or
merges knowledge databases, or imports old chats.

<a id="область-действия-и-существующие-настройки"></a>
## Scope and existing settings

Setup covers local Codex chats for this OS user and Codex profile, including
projectless chats. It uses `${CODEX_HOME:-$HOME/.codex}/AGENTS.md`, unless a nonempty
`AGENTS.override.md` takes precedence. Other profiles, remote hosts, cloud agents
and other agent products need their own setup. Open chats may retain old instructions.

The instruction block authorizes scoped memory use and saving useful outcomes
during substantive work. Explicit read-only requests and workspace isolation
still apply. Project/user instructions can narrow behavior. Setup never edits
`config.toml`, weakens approval settings or bypasses disabled skills. Success
verifies installed files, not whether an agent follows every instruction.
Setup refuses instructions exceeding Codex's default 32 KiB discovery budget;
custom lower budgets and disabled skills must be reconciled in Codex settings.
See official [skills](https://learn.chatgpt.com/docs/build-skills) and
[instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

PATH setup supports zsh (`$ZDOTDIR/.zshrc` or `~/.zshrc`), bash (`~/.bashrc` plus
its first existing login profile, defaulting to `~/.bash_profile` on macOS or
`~/.profile` on Linux), and sh/dash (`~/.profile`).
Existing bytes are preserved. For another shell or an already managed PATH, use
`--no-path` and configure `~/.local/bin` yourself. Desktop agents can use the
stable absolute command included in the instruction block.

<a id="проверка-повторный-запуск-и-принятие-существующих-skills"></a>
## Inspect, repeat and adopt existing skills

To inspect the script before execution:

```sh
curl -fL --proto '=https' --proto-redir '=https' https://github.com/Hidanio/Richi/releases/latest/download/install.sh -o richi-install.sh
less richi-install.sh
sh richi-install.sh --dry-run
sh richi-install.sh --yes
```

The bootstrap downloads a standalone installer over HTTPS and verifies its
SHA256 before execution. The installer verifies the official wheel's checksum,
size, identity and compatibility. Checksums detect corruption; authenticity
relies on the official GitHub repository and HTTPS. No PyPI package named Richi
is fetched. Online first installation upgrades pip in the dedicated environment.

Repeating the command reuses an installer-owned environment, checks for a newer
stable release and reconciles setup without duplicate instruction blocks.
Interrupted first environments are retained as `.interrupted-*` backups before
retrying creation. Installer `--dry-run` previews paths and prerequisites only;
the installed command performs the complete integration conflict check:

```sh
richi setup --dry-run
richi setup --yes
richi setup --status
```

Existing custom skills require explicit adoption:

```sh
sh richi-install.sh --yes --replace-skill
# Or, with a supported existing installation:
richi setup --yes --replace-skill
```

Existing directories/links are moved to `~/.agents/richi-skill-backups/`, outside
skill discovery. Symlink targets remain untouched. Legacy `${CODEX_HOME}/skills/project-memory`
copies are backed up under `${CODEX_HOME}/richi-skill-backups/`, avoiding duplicate skills. Unrelated command files,
symlinked instructions/parent directories and edited managed blocks cause an
actionable error without overwriting them. Edits outside managed blocks survive.

An environment without the installer's ownership marker is never overwritten.
Use its `richi setup` when supported. A permanent bootstrap predating setup needs
a new empty `--prefix`; keep the old environment and reconcile its command symlink
before connecting the replacement. Workspace data is independent of environments.
Never use `pip install -U` on a managed installation with staged releases.

<a id="обновления-откат-и-разработка"></a>
## Updates, rollback and development

```sh
richi update check
richi update apply
richi update rollback
```

The complete skill and references are bundled from `skills/project-memory/` into
the wheel. Update and rollback switch its managed link to the matching package.
A recoverable file journal coordinates the skill link, ownership metadata and
runtime pointer. Interrupted activation is completed or undone on the next CLI
call. Edited skill contents or replaced links block activation and are preserved.
Releases without the setup protocol cannot replace a managed integration. Open
a new chat after updating, since running agents may retain loaded skill content.

Automatic installation stays off unless explicitly enabled with
`richi update auto enable --interval 6h --install`; see [UPDATES.md](UPDATES.md).
Workspace dev settings are preserved. CLI/map can use development sources, but
the managed **global skill follows the installed release**. For skill development,
retain a separately managed source symlink instead of adopting it with
`--replace-skill`. Setup never edits the source checkout.

<a id="офлайн-установка"></a>
## Offline installation

Download `richi-installer.pyz`, its checksum, `richi-release.json` and the wheel
from the same trusted release. Verify the archive checksum, then run:

```sh
python3 -I richi-installer.pyz --yes --wheel richi-X.Y.Z-py3-none-any.whl --manifest richi-release.json
```

Offline mode uses ensurepip without an online pip upgrade. Repeating the same
initial wheel is idempotent; a different wheel requires a new prefix or the
verified `richi update apply --version X.Y.Z` path. Online installer `--version`
pins the wheel version, not the bootstrap download. The release workflow tests
the exact wheel and installer on Python 3.9/3.12, macOS/Linux before publishing;
see [RELEASING.md](RELEASING.md).

<a id="резервные-копии-восстановление-и-удаление"></a>
## Backups, recovery and removal

`richi setup --status` is read-only and reports managed paths and pending recovery.
Setup output gives the private `setup-backups` directory. Its JSON files retain
original bytes (base64), modes, links and adoption backup paths. They can contain
private instructions: do not publish them. Keep a pending `setup-pending.json`
and installation generations intact for recovery. If files changed after a
crash, back up both versions and reconcile the reported path before retrying;
automatic recovery refuses to overwrite those edits.

To disconnect manually, first disable automatic updates. Remove only the
`richi:project-memory` marker block from the reported global instruction file
and `richi:path` blocks from reported profiles. Verify targets before removing
the managed skill and command symlinks; optionally restore the reported skill
backup. Then remove the dedicated environment and its installation metadata if
unused. Keep workspace data. This release has no automatic uninstaller; removing
the integration never requires deleting the whole AGENTS.md file.
