# Agents and their skills

English | [Русский](AGENT_SKILLS.ru.md)

A standing rule may name a skill. Agents can read some of the same folders,
but each has its own discovery and naming rules. Richi lists the skills available
to each agent on this computer. A rule
then names one skill for every agent or a different one per agent (see
[RULES.md](RULES.md)), and `rules show` gives each agent its own.

Richi defines the skill sources for its **built-in** agents Claude Code, Codex
and Cursor. The user may **adjust** a built-in agent by switching off a skill
source or adding skill folders. The user may also **describe another agent**
by its name, its skill folders and how to recognize it. These settings belong
to the user and are shared by every workspace of this computer; they are not
knowledge and are not copied with a workspace.

## See the agents

```sh
richi --compact-json skill-sources list
```

`agents` holds every agent in order: `id` (used in `--skill ID=NAME` and
`rules show --agent ID`), `name`, `builtin`, `detected` (it looks installed for
this user), `skills` (how many it can use) and `sources`. Richi gets `detected`
from the installed launcher for agents it can connect. If an older launcher has
no Cursor adapter, `connection` is null. Richi then checks Cursor’s own profile
folders for detection and returns null if both of those sources are disabled.
For a custom agent, Richi checks its enabled skill folders and returns null when
all are disabled. A source has a `key`,
its `path`, whether the folder `exists` (`denied` when this user may not read
it; neither is checked for a source switched off), whether it is `enabled`,
whether the user `added` it, and how many `skills` it gives. `chat_prefix` says
whether the agent's chats are named `ID:…` in `RICHI_CHAT_ID`, which then also
tells Richi who it is. Cursor's hook supplies `--chat cursor:<id>`. Codex's
chat identity is its thread ID alone.
`updated_at` is the version of
the user's settings for the agent, null when there are none. `presets` lists
well-known agents that could be added, with the folder each reads. `problem` is
set when the settings file cannot be used, and `problem_code` says why:
`damaged` (fix or remove the file), `unreadable` (it could not be read just
now), `newer` (a newer Richi wrote it: update Richi; this version does not
change it) or `large` (larger than this version reads: update Richi if a newer
one wrote it, or else make it smaller; this version does not change it). Richi then works with the built-in agents only, without the user's
settings. A change that would make the file larger than Richi reads is refused.
`unknown_agents` lists settings this version keeps as they are without using
them: of an agent that only a newer Richi has built in, or settings of one
agent it cannot use (`error` says why: a newer Richi wrote them, they are
damaged, or there are more own agents than this version uses, which keeps the
ones described first). `next`, where given, is the command that removes them;
the other agents' settings keep working. It names the first 20, and
`unknown_agents_more` counts the rest; a key longer than 200 characters, which
only a hand edit makes, is cut and given its length and a hash.

## Describe another agent

```sh
richi --compact-json skill-sources add gemini-cli --name "Gemini CLI" \
  --skills ~/.gemini/skills --variable GEMINI_CLI=1
```

Add an agent only when the user asks for it. The ID is 1–32 lowercase letters,
digits or `-`, starting with a letter, and never changes afterwards. `--skills`
is a folder with one skill per subfolder, each with a `SKILL.md`; give an
absolute path or one under `~`, and repeat the option for several folders.
`--variable NAME` or `NAME=VALUE` names an environment variable that the
agent's shells set, so that `rules show` recognizes the agent; without it the
agent passes `--agent ID`, or its chat ID starts with `ID:`. A process started
from another agent's terminal carries the variables of both agents and may
carry the other's chat identity; whenever the signs point to different agents,
Richi names neither, and the agent passes `--agent ID`. `--manual-skills
front-matter` leaves out skills whose `SKILL.md` says
`disable-model-invocation: true`, because a rule cannot make the agent use a
skill that only the user may invoke.

Richi installs its integration (`richi setup`) only for built-in agents. For
another agent, the user adds the `richi rules show --agent ID` step to that
agent's own standing instructions.

## Adjust an agent

Cursor reads skills from `~/.cursor/skills`, `~/.agents/skills`, `~/.claude/skills`
and `~/.codex/skills`. Its built-in skills are in `~/.cursor/skills-cursor`.
Codex reads personal skills in `~/.agents/skills` and the `skills` directory
of its own profile. Both clients discover skills in nested directories. Codex
follows linked directories and requires a regular `SKILL.md` entry.
Richi also searches folders you add for these agents recursively. It examines up to 100,000
directory entries per root. A source marked `truncated` may contain more skills
than its count shows.

If you added `cursor` in an earlier version, the upgrade
uses Cursor’s built-in name, detection and skill filtering. Your additional
folders still appear, and a disabled folder stays disabled even if it is now a
built-in source. Use the source key from `skill-sources list` to enable it.
Richi leaves the saved settings file intact on upgrade. To restore the defaults,
reset Cursor as described below.

```sh
richi --compact-json skill-sources update claude --disable claude-app --add-skills ~/work/team-skills
richi --compact-json skill-sources update gemini-cli \
  --expected-updated-at 2026-01-02T03:04:05.000000Z --name "Gemini" --manual-skills front-matter
```

`--disable SOURCE` and `--enable SOURCE` take a source `key` from `skill-sources list`,
or an added folder. `--add-skills` and `--remove-skills` change the added
folders. For an own agent, `--name`, `--variable` (an empty value removes it)
and `--manual-skills` change its description; a built-in agent refuses them.
Pass the agent's `updated_at` as `--expected-updated-at`; omit the option when
it is null, which is a built-in agent nobody has adjusted yet. A newer version
is a conflict and nothing is written: read the list again.

## Remove an agent or its adjustments

```sh
richi --compact-json skill-sources remove gemini-cli --expected-updated-at 2026-01-02T03:04:05.000000Z
```

For an own agent this deletes its description; rules that name a skill for it
keep that name. For a built-in agent it drops the user's adjustments and
restores the agent as Richi defines it. Skill folders on disk are never
touched.

## Agents in the map

The map's **Agents** tab shows the same list: each agent's skill sources with
their folders and skill counts, how Richi recognizes the agent, and the rules
that give it a skill, marking a skill the agent does not have. A page opened by
`richi map` switches sources, adds folders and agents and saves at once; any
other page composes the `richi skill-sources` command instead.
