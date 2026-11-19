<a id="установка-одной-командой"></a>
# One-command installation

English | [Русский](SETUP.ru.md)

The example connects Codex. For Claude Code, replace `codex` with `claude`.

```sh
curl -fsSL --proto '=https' --proto-redir '=https' https://github.com/Hidanio/Richi/releases/latest/download/install.sh | sh -s -- --agent codex --yes
```

Local Cursor Agent requires Richi 1.2.0 or later. To check the latest stable
installer, replace `--agent codex --yes` in the command above with `--help`.
If the agent list includes `cursor`, run the command with `--agent cursor --yes`.
If `cursor` is missing, [install Richi from source](INSTALLATION.md).

You need macOS or Linux, curl and Python 3.9+ with sqlite3, venv and ensurepip.
On Debian/Ubuntu you may also need `python3-venv`. Git is required for repository history.

The installer puts a fixed release in `~/.local/share/richi/venv`, creates the
`~/.local/bin/richi` command and connects Project Memory to the agent you chose.
Omitting `--agent` selects Codex. Omitting `--yes` lets you inspect the plan
and confirm it in a terminal.

Open a new chat in the selected agent and choose its workspace. The agent can
initialize an empty store after selection. Open a new terminal for the PATH
change or use `~/.local/bin/richi` immediately. The installer can run before you have a workspace database. It leaves existing
workspace settings, chat bindings and data in place.

<a id="область-действия-и-существующие-настройки"></a>
## Scope and existing settings

For Codex, setup links `~/.agents/skills/project-memory` and covers local chats
in the selected profile, including projectless chats. It uses `${CODEX_HOME:-$HOME/.codex}/AGENTS.md`, unless a nonempty
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

For a preview, download the installer and inspect its supported agents. This example uses Codex.

```sh
curl -fL --proto '=https' --proto-redir '=https' https://github.com/Hidanio/Richi/releases/latest/download/install.sh -o richi-install.sh
less richi-install.sh
sh richi-install.sh --help
sh richi-install.sh --agent codex --dry-run
sh richi-install.sh --agent codex --yes
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
the installed command checks for integration conflicts. The examples below use
Codex. For other clients, pass the same `--agent` as at first installation.

```sh
richi setup --dry-run
richi setup --yes
richi setup --status
```

Existing custom skills require explicit adoption:

```sh
sh richi-install.sh --agent codex --yes --replace-skill
# Or, with a supported existing installation:
richi setup --yes --replace-skill
```

For Codex, existing directories/links are moved to `~/.agents/richi-skill-backups/`, outside
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
```

To return to the previous release later, use `richi update rollback`.

The complete skill and references are bundled from `skills/project-memory/` into
the wheel. Update and rollback switch its managed link to the matching package.
A recoverable file journal coordinates the skill link, ownership metadata and
runtime pointer. Interrupted activation is completed or undone on the next CLI
call other than help, `richi --version` or a read-only `richi setup --status`,
`richi setup --dry-run` or `richi agents --status`. Edited skill contents or
replaced links block activation and are preserved.
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
connected later follows the links of the agents already connected. An existing link that already
points to exactly the managed target is adopted without `--replace-skill`; other
existing skills still require it. Setup never edits the source checkout.

<a id="офлайн-установка"></a>
## Offline installation

Download `richi-installer.pyz`, its checksum, `richi-release.json` and the wheel
from the same trusted release. Verify the archive checksum. This example connects
Codex. For another agent, add the option listed by `python3 -I richi-installer.pyz --help`.

```sh
python3 -I richi-installer.pyz --yes --wheel richi-X.Y.Z-py3-none-any.whl --manifest richi-release.json
```

Offline mode uses ensurepip without an online pip upgrade. Repeating the same
initial wheel is idempotent; a different wheel requires a new prefix or the
verified `richi update apply --version X.Y.Z` path. Online installer `--version`
pins the wheel version, not the bootstrap download. The release workflow tests
the exact wheel and installer on Python 3.9, 3.12, 3.13 and 3.14, macOS/Linux
before publishing; see [RELEASING.md](RELEASING.md).

<a id="резервные-копии-восстановление-и-удаление"></a>
## Backups, recovery and removal

`richi setup --status` is read-only and reports managed paths and pending recovery.
Setup output gives the private `setup-backups` directory. Its JSON files retain
original bytes (base64), modes, links and adoption backup paths. They can contain
private instructions: do not publish them. Keep a pending `setup-pending.json`
and installation generations intact for recovery. If files changed after a
crash, back up both versions and reconcile the reported path before retrying;
automatic recovery refuses to overwrite those edits.

To remove this Richi installation manually, first disable automatic updates. For Cursor, remove the
Richi `sessionStart` entry from `~/.cursor/hooks.json` and the dedicated
`~/.cursor/richi/session_start.py` and `~/.cursor/rules/richi-project-memory.mdc`
files. For Codex or Claude Code, remove only the `richi:project-memory` block
from the reported global instruction file. For Claude Code also remove the Richi
`SessionStart` entry from its `settings.json` and the managed session script.
Remove the `richi:path` blocks from the reported shell profiles. Verify targets before removing
the managed skill and command symlinks; optionally restore the reported skill
backup. Then remove the dedicated environment and its installation metadata if
unused. Keep workspace data. This release has no automatic uninstaller; removing
the integration never requires deleting the whole AGENTS.md file.

## Connecting Codex, Claude Code and Cursor

If Richi is already installed, start with `richi agents --status` in a terminal.
This read-only command lists the clients this package supports, whether it
detected each client and whether it is configured. Run `richi agents` to open
the interactive menu. Connect each client you want to use.

On Richi 1.0.1, use `richi setup --yes` to connect Codex and `richi setup --status`
to inspect it. That launcher predates the agents menu and the `--agent` option.
Claude Code requires Richi 1.1.0 or later, and local Cursor Agent requires 1.2.0
or later. Once a stable release supports your client, run `richi update apply`
on the older installation. Check that the client appears in `richi setup --help`
before connecting it. The installed launcher supplies the agent choices even
in dev mode.

### Local Cursor Agent

On macOS, Richi also finds Cursor in `/Applications` and `~/Applications`
before its first launch and without a shell command on PATH.

```sh
richi setup --agent cursor --dry-run
richi setup --agent cursor --yes
richi setup --agent cursor --status
```

Setup links `~/.cursor/skills/project-memory`, installs the user rule
`~/.cursor/rules/richi-project-memory.mdc` and adds a `sessionStart` hook to
`~/.cursor/hooks.json` alongside your existing hooks. The hook runs
`~/.cursor/richi/session_start.py`. [Cursor skills](https://cursor.com/docs/skills)
and [user rules](https://cursor.com/help/customization/rules).

Open a new local Cursor Agent chat. The hook gives the agent the exact
`--chat cursor:<id>` argument for every Richi command, including the first
`chat current`. Choose a workspace once per conversation. Richi reuses that
binding whenever Cursor supplies the same chat ID. If the agent has no
hook-provided ID, it waits for one before calling Richi. Check the Hooks output
channel in Cursor and open a new chat after fixing the hook.
[Cursor hooks](https://cursor.com/docs/hooks#sessionstart).

### Claude Code

```sh
richi setup --agent claude --dry-run
richi setup --agent claude --yes
richi setup --agent claude --status
```

Claude setup installs the bundled skill under
`~/.claude/skills/project-memory`, a managed block in `~/.claude/CLAUDE.md`, and a
SessionStart hook in `settings.json`, preserving unrelated settings and hooks.
`CLAUDE_CONFIG_DIR` selects an alternate profile. Existing copied skills require
`--replace-skill`, which backs them up outside skill discovery.

Start a new local Claude Code session. The hook uses the actual session ID as
`RICHI_CHAT_ID=claude:<session_id>` and provides an explicit `--chat` fallback in
session context. Resumed sessions retain identity; new sessions choose a workspace
independently. Setup does not override disabled hooks or permission policies.
[Claude profile layout](https://code.claude.com/docs/en/claude-directory)
and [SessionStart hooks](https://code.claude.com/docs/en/hooks#sessionstart).

### Package compatibility

Managed skills and session scripts follow package updates and rollbacks together.
Richi checks that each configured agent has a compatible adapter in the selected
release before activation. This applies to Cursor when rolling back to a package
that predates its adapter. Launcher changes require installing a new package,
even with dev mode on.

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
- An optional `instruction_file(block)` returns the complete file as bytes when
  an adapter owns a dedicated instruction file. Cursor uses this to add the MDC
  frontmatter to its user rule. Richi checks the complete file for local edits.
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
the session adapters' `HOOK` assignments.

Each adapter must provide a stable chat identity and preserve the user's explicit
workspace choice. Test its profile/settings behavior in a synthetic home;
`tests/test_agent_adapters.py` verifies the shared flow with a third test adapter.
