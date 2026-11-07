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
Workspace dev settings are preserved. Dev mode switches the runtime **and the
managed skill links**: `richi config set dev true` points every managed skill to
`<development.source>/skills/project-memory`, and `richi config set dev false`
points it back to the installed release, each in one recoverable transaction
reported as `agent_skills`. This applies to every connected agent. Instruction
blocks, adapter files such as hooks, PATH blocks and the launcher stay on the
installed release. Links are user-wide and follow
the latest `config set dev` in any registered workspace; ad hoc `--config` or
`RICHI_CONFIG` files do not move them. Updates and rollbacks keep checkout
links; before rolling back to 1.1.0 or older, run `richi config set dev false`.
Details: [what dev switches](INSTALLATION.md#what-dev-switches).

`richi setup --status` reports `skill_mode` (`release` or `dev`) and the expected
`skill_link`. A dev link whose checkout lost its skill is `repair_required` with
`dev_skill_error`; `richi setup --yes` then restores the release skill. An agent
connected while dev is on follows the checkout. An existing link that already
points to exactly the managed target is adopted without `--replace-skill`; other
existing skills still require it. Setup never edits the source checkout.

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

## Claude Code and agents installed later

Run `richi agents` to discover and connect Codex or Claude Code, or
`richi agents --status` for read-only status. Discovery checks executables/profile
folders; it is not a client health check. The menu can rescan after a new client
is installed. Existing default behavior remains Codex.

```sh
richi setup --agent claude --dry-run
richi setup --agent claude --yes
richi setup --agent claude --status
```

The installer also accepts `--agent claude`. Run setup separately for each client
to connect both. Claude setup installs the bundled skill under
`~/.claude/skills/project-memory`, a managed block in `~/.claude/CLAUDE.md`, and a
SessionStart hook in `settings.json`, preserving unrelated settings and hooks.
`CLAUDE_CONFIG_DIR` selects an alternate profile. Existing copied skills require
`--replace-skill`, which backs them up outside skill discovery.

Start a new local Claude Code session. The hook uses the actual session ID as
`RICHI_CHAT_ID=claude:<session_id>` and provides an explicit `--chat` fallback in
session context. Resumed sessions retain identity; new sessions choose a workspace
independently. Setup does not override disabled hooks or permission policies.
This supports Claude Code, not ordinary Claude chat or MCP. Managed skills and
Claude's session script follow package updates/rollback together. Releases that
cannot maintain any configured agent are blocked before activation.
Launcher changes require installing a new package, even with dev mode on.

Claude contracts: [profile layout](https://code.claude.com/docs/en/claude-directory)
and [SessionStart hooks](https://code.claude.com/docs/en/hooks#sessionstart).

## Adding another agent to Richi

The menu rescans clients supported by the installed Richi package. Supporting a
new client requires a new package; discovery does not install third-party code.

Register a built-in adapter in `launcher/richi_launcher/agent_adapters.py`.
This literal catalog supplies the setup/installer choices and menu entries.
Put agent-specific behavior in its module, following `codex_setup.py` or
`claude_setup.py`:

- `LABEL` and `INSTRUCTION_LIMIT` describe the client and its instruction budget
  (`None` if no smaller limit is known).
- `detected()` checks the client's executable/profile without writes.
- `paths(current=None)` returns the profile, effective instruction file and
  skill path. Validate overrides and an existing record's profile here.
- `setup_operations(ctx, profile, current, replace_skill)` and
  `activation_operations(ctx, record, site)` return `(extra_metadata, operations)`.
  Plan changes with `setup_files`; never write directly. The activation method
  reads the selected package's resources, including during rollback, and uses
  stored paths rather than the current shell's profile override.

Shared code owns locks, skill links, instruction/PATH blocks, backups, manifests
and recovery. Dev mode is shared as well: `config set dev` and activation move the
skill path returned by `paths()` for every configured adapter, so an adapter needs
no dev-specific code; its own files keep following the installed release. Adapter metadata must not overwrite shared fields. The catalog's
version describes the adapter's persisted metadata/resource contract, not the
Richi release. Compatible releases retain it; incompatible versions fail closed
until an explicit migration exists. Release inspection reads literal data without
executing candidate code. Preserve literal `AGENTS`, `DEV_SKILL_PROTOCOL` and
Claude `HOOK` assignments.

Each adapter must provide a stable chat identity and preserve the user's explicit
workspace choice. Test its profile/settings behavior in a synthetic home;
`tests/test_agent_adapters.py` verifies the shared flow with a third test adapter.
