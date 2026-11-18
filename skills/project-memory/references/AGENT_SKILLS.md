# Agents and their skills

English | [Русский](AGENT_SKILLS.ru.md)

A standing rule may name a skill, and agents do not share skills: each keeps
them in its own folders and names plugin skills its own way. Richi therefore
knows which agents work on this computer and where each finds skills. A rule
then names one skill for every agent or a different one per agent (see
[RULES.md](RULES.md)), and `rules show` gives each agent its own.

Agents come in three layers. **Built-in** agents, Claude Code and Codex, are
defined by Richi, including their plugin and built-in skills, and change only
with Richi. The user may **adjust** a built-in agent: switch one of its skill
sources off and add skill folders. And the user may **describe another agent**
by its name, its skill folders and how to recognize it. These settings belong
to the user and are shared by every workspace of this computer; they are not
knowledge and are not copied with a workspace.

## See the agents

```sh
richi --compact-json skill-sources list
```

`agents` holds every agent in order: `id` (used in `--skill ID=NAME` and
`rules show --agent ID`), `name`, `builtin`, `detected` (it looks installed for
this user), `skills` (how many it can use) and `sources`. A source has a `key`,
its `path`, whether the folder `exists` (`denied` when this user may not read
it), whether it is `enabled`, whether the user `added` it, and how many `skills`
it gives. `updated_at` is the version of
the user's settings for the agent, null when there are none. `presets` lists
well-known agents that could be added, with the folder each reads. `problem` is
set when the settings file cannot be used, because it is damaged or could not be
read just now; Richi then works with the built-in agents only. A change that
would make the file larger than Richi reads is refused. `unknown_agents` appears when the file has settings of an agent
that only a newer Richi has built in: this version keeps them without using
them, and `next` is the command that removes them.

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
