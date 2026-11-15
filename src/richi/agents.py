"""The coding agents Richi knows, each described once as data.

A rule may name a skill for every agent or one for a particular agent. What
differs between agents lives only here: how to recognize the agent whose shell
runs a command, where it keeps the skills it can use, how a skill says the
agent never chooses it by itself, and whether plugin commands count as skills.
Rules, the skill catalog and the map work with whatever agents registry()
returns. It has three layers, each refining the one before:

1. AGENTS: the agents built into Richi. Everything special about one, such as
   an unusual skill layout, its policy file or the order of detection, is code
   here and changes only with Richi (plus its setup in the launcher, if Richi
   installs an integration for it).
2. The user's adjustments of a built-in agent: skill sources switched off and
   extra skill folders. Removing them restores the built-in agent.
3. The user's own agents, described with generic parts: a name, skill
   folders, an environment variable that marks its shells. An agent that needs
   more than that becomes a built-in entry.

Layers 2 and 3 are the user's settings, kept in agents.json in Richi's
configuration directory and shared by every workspace of this user; `richi
skill-sources` and the map's Agents tab change them (agent_commands).

Stored rules may name agents this registry does not list, such as rules
written by a newer Richi; they are kept and shown as they are.
"""
import json
import os
from pathlib import Path
import re
import sys
from typing import Callable, Dict, NamedTuple, Optional, Tuple


ID = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
ID_HELP = "Agent ID: 1-32 lowercase letters, digits or '-', starting with a letter"
SETTINGS_FILE = "agents.json"
SETTINGS_VERSION = 1
MAX_SETTINGS = 1024 * 1024
MAX_CUSTOM = 50
MAX_FOLDERS = 20
MAX_NAME = 60
MAX_FOLDER = 1000
NAME_HELP = "Agent name: one line of 1-%d characters" % MAX_NAME
FOLDER_HELP = "Skill folder: an absolute path or one under ~, at most %d characters" % MAX_FOLDER
VARIABLE_HELP = ("Environment variable NAME or NAME=VALUE: NAME marks the agent's shells when it is set, "
                 "NAME=VALUE when it has that value")
# How an agent's skill says that only the user may invoke it.
MANUAL_SKILLS = ("none", "front-matter")
_SOURCE_KEY = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}(=[^\x00-\x1f\x7f]{1,100})?\Z")
_ENTRY_KEYS = {"created_at", "updated_at", "name", "folders", "disabled", "variable", "manual_skills"}
# Where the Claude desktop app keeps the claude.ai plugins and built-in skills
# it passes to Claude Code (CLAUDE_CODE_PLUGIN_ATTRIBUTION and
# CLAUDE_CODE_DESKTOP_SKILLS_PLUGIN_DIR in that process; shells do not get them).
CLAUDE_APP = Path("Library", "Application Support", "Claude", "local-agent-mode-sessions")


class SkillSource(NamedTuple):
    """A directory where an agent finds skills.

    key names the source within its agent: a short word for a built-in
    source, the folder as the user wrote it for an added one. kind groups
    skills in the map: "skills", "system" (built into the agent) or "plugins".
    layout says how the directory holds them: "skills" has one skill per
    subdirectory; "cache" is a plugin cache,
    <marketplace>/<plugin>/<version>/skills/<skill>; "claude-app" is the Claude
    app's <account>/<organization>/rpm/<plugin> with rpm/manifest.json, and
    skills-plugin/<account>/<organization> with its manifest of enabled skills.
    """
    key: str
    kind: str
    layout: str
    path: Path
    # False when the user switched the source off.
    enabled: bool = True
    # True for a folder the user added.
    added: bool = False


class Agent(NamedTuple):
    # Also the prefix of the chat identity its integration sets: RICHI_CHAT_ID "<id>:...".
    id: str
    name: str
    # Set only in this agent's own sessions, so they identify it even in a
    # process started from another agent's shell.
    session_variables: Tuple[str, ...]
    # Set in this agent's shells and inherited by everything started there:
    # {name: the value it has, or None when being set is enough}.
    shell_variables: Dict[str, Optional[str]]
    # How a skill says the agent never chooses it itself: "front-matter"
    # (disable-model-invocation: true in SKILL.md), "openai-yaml"
    # (policy.allow_implicit_invocation: false in agents/openai.yaml) or "none".
    invocation: str
    # Whether the agent lists its plugins' commands among their skills.
    plugin_commands: bool
    # (environ, home) -> [SkillSource]
    sources: Callable
    # False for an agent the user described.
    builtin: bool = True
    # When the user's settings for this agent last changed; None without any.
    updated_at: Optional[str] = None


def _home_variable(environ, home, variable, default):
    return Path(environ[variable]).expanduser() if environ.get(variable) else home / default


def _claude_sources(environ, home):
    profile = _home_variable(environ, home, "CLAUDE_CONFIG_DIR", ".claude")
    return [SkillSource("skills", "skills", "skills", profile / "skills"),
            SkillSource("plugins", "plugins", "cache", profile / "plugins" / "cache"),
            SkillSource("claude-app", "plugins", "claude-app", home / CLAUDE_APP)]


def _codex_sources(environ, home):
    profile = _home_variable(environ, home, "CODEX_HOME", ".codex")
    return [SkillSource("agents-skills", "skills", "skills", home / ".agents" / "skills"),
            SkillSource("codex-skills", "skills", "skills", profile / "skills"),
            # Codex's built-in skills; hidden, so the "skills" walk skips them.
            SkillSource("system", "system", "skills", profile / "skills" / ".system"),
            SkillSource("plugins", "plugins", "cache", profile / "plugins" / "cache")]


AGENTS = (
    Agent(id="claude", name="Claude Code", session_variables=(), shell_variables={"CLAUDECODE": "1"},
          invocation="front-matter", plugin_commands=True, sources=_claude_sources),
    Agent(id="codex", name="Codex", session_variables=("CODEX_THREAD_ID",), shell_variables={},
          invocation="openai-yaml", plugin_commands=False, sources=_codex_sources),
)

# Other agents a user may add as their own, with the user-wide skill folder
# each reads, as the open `skills` CLI lists them (vercel-labs/skills,
# docs/agents.md, November 2026). They are suggestions for `skill-sources add`, not
# agents Richi knows until the user adds one.
PRESETS = (
    ("cursor", "Cursor", "~/.cursor/skills"),
    ("gemini-cli", "Gemini CLI", "~/.gemini/skills"),
    ("opencode", "OpenCode", "~/.config/opencode/skills"),
    ("github-copilot", "GitHub Copilot", "~/.copilot/skills"),
    ("windsurf", "Windsurf", "~/.codeium/windsurf/skills"),
    ("cline", "Cline", "~/.agents/skills"),
    ("zed", "Zed", "~/.agents/skills"),
    ("amp", "Amp", "~/.config/agents/skills"),
    ("goose", "Goose", "~/.config/goose/skills"),
    ("kiro-cli", "Kiro CLI", "~/.kiro/skills"),
    ("qwen-code", "Qwen Code", "~/.qwen/skills"),
    ("roo", "Roo Code", "~/.roo/skills"),
    ("droid", "Droid", "~/.factory/skills"),
)


def valid_id(value):
    return isinstance(value, str) and ID.fullmatch(value) is not None


def _one_line(value, limit):
    return (isinstance(value, str) and 0 < len(value) <= limit and value == value.strip()
            and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value))


def valid_name(value):
    return _one_line(value, MAX_NAME)


def valid_folder(value):
    """A stored folder: absolute or under ~, never relative to a working directory."""
    return _one_line(value, MAX_FOLDER) and (value == "~" or value.startswith("~/") or value.startswith("/"))


def valid_variable(value):
    return isinstance(value, str) and _VARIABLE.fullmatch(value) is not None


def folder_path(text, home):
    """The directory a stored folder names; ~ is the user's home."""
    if text == "~" or text.startswith("~/"):
        return Path(home) / text[2:]
    return Path(text)


def builtin_ids():
    return tuple(agent.id for agent in AGENTS)


def settings_path(environ=None, home=None):
    """agents.json in Richi's configuration directory (richi_launcher.config._platform_dirs)."""
    environ = os.environ if environ is None else environ
    home = Path(home) if home is not None else Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Richi" / SETTINGS_FILE
    configured = environ.get("XDG_CONFIG_HOME")
    base = Path(configured) if configured and Path(configured).is_absolute() else home / ".config"
    return base / "richi" / SETTINGS_FILE


def check_entry(identifier, entry):
    """Raise ValueError unless entry is a well-formed stored agent."""
    if not valid_id(identifier):
        raise ValueError(ID_HELP)
    if not isinstance(entry, dict) or set(entry) - _ENTRY_KEYS:
        raise ValueError("unexpected fields for " + identifier)
    if not _one_line(entry.get("updated_at"), 64) or ("created_at" in entry and not _one_line(entry["created_at"], 64)):
        raise ValueError("invalid timestamps for " + identifier)
    folders = entry.get("folders", [])
    if not isinstance(folders, list) or len(folders) > MAX_FOLDERS:
        raise ValueError("an agent may have at most %d skill folders" % MAX_FOLDERS)
    seen = set()
    for folder in folders:
        if (not isinstance(folder, dict) or set(folder) != {"path", "enabled"} or not valid_folder(folder["path"])
                or type(folder["enabled"]) is not bool or folder["path"] in seen):
            raise ValueError("invalid skill folder for " + identifier)
        seen.add(folder["path"])
    disabled = entry.get("disabled", [])
    if (not isinstance(disabled, list) or len(disabled) > MAX_FOLDERS or len(set(map(str, disabled))) != len(disabled)
            or not all(isinstance(key, str) and _SOURCE_KEY.fullmatch(key) for key in disabled)):
        raise ValueError("invalid switched-off sources for " + identifier)
    if "name" in entry and not valid_name(entry["name"]):
        raise ValueError(NAME_HELP)
    if entry.get("variable") is not None and not valid_variable(entry["variable"]):
        raise ValueError(VARIABLE_HELP)
    if entry.get("manual_skills", "none") not in MANUAL_SKILLS:
        raise ValueError("manual_skills must be one of " + ", ".join(MANUAL_SKILLS))


def check_settings(data):
    """Raise ValueError unless data is a well-formed settings document; returns its agents."""
    if not isinstance(data, dict) or set(data) != {"version", "agents"} or data["version"] != SETTINGS_VERSION \
            or not isinstance(data["agents"], dict):
        raise ValueError("expected {\"version\": %d, \"agents\": {...}}" % SETTINGS_VERSION)
    for identifier, entry in data["agents"].items():
        check_entry(identifier, entry)
    custom = [identifier for identifier in data["agents"] if identifier not in builtin_ids()]
    if len(custom) > MAX_CUSTOM:
        raise ValueError("at most %d own agents" % MAX_CUSTOM)
    return data["agents"]


_cache = {}


def load_settings(environ=None, home=None):
    """(stored agents by ID, problem): a missing file has none; an unusable one is reported, not raised.

    Every command reads the registry, so a damaged settings file must not
    break them: Richi then knows only its built-in agents and says why.
    """
    path = settings_path(environ, home)
    try:
        info = path.stat()
    except OSError:
        return {}, None
    signature = (info.st_mtime_ns, info.st_size)
    cached = _cache.get(str(path))
    if cached is not None and cached[0] == signature:
        return cached[1], cached[2]
    try:
        if info.st_size > MAX_SETTINGS:
            raise ValueError("the file is larger than %d bytes" % MAX_SETTINGS)
        entries, problem = check_settings(json.loads(path.read_text(encoding="utf-8"))), None
    except (OSError, ValueError) as exc:
        entries, problem = {}, "Agent settings are ignored: " + str(path) + ": " + str(exc) + "; fix or remove the file"
    _cache.clear()
    _cache[str(path)] = (signature, entries, problem)
    return entries, problem


def _folders(folders, home):
    return [SkillSource(key=path, kind="skills", layout="skills", path=folder_path(path, home), enabled=enabled,
                        added=True) for path, enabled in folders]


def _stored_folders(entry):
    return tuple((folder["path"], folder["enabled"]) for folder in entry.get("folders", ()))


def _adjusted(agent, entry):
    """A built-in agent with the user's adjustments applied."""
    disabled, folders = frozenset(entry.get("disabled", ())), _stored_folders(entry)

    def sources(environ, home, base=agent.sources):
        built = [source._replace(enabled=source.key not in disabled) for source in base(environ, home)]
        taken = {source.path for source in built}
        return built + [source for source in _folders(folders, home) if source.path not in taken]
    return agent._replace(sources=sources, updated_at=entry["updated_at"])


def _custom(identifier, entry):
    """The user's own agent, built from generic parts."""
    variable, folders = entry.get("variable") or "", _stored_folders(entry)
    name, separator, value = variable.partition("=")
    # The user's variable marks the agent's shells, so processes started there inherit it.
    return Agent(id=identifier, name=entry["name"], session_variables=(),
                 shell_variables={name: value if separator else None} if name else {},
                 invocation=entry.get("manual_skills", "none"), plugin_commands=False,
                 sources=lambda environ, home: _folders(folders, home), builtin=False,
                 updated_at=entry["updated_at"])


def registry(environ=None, home=None):
    """Every agent Richi works with: the built-in ones as the user adjusted them, then the user's own."""
    entries, _problem = load_settings(environ, home)
    known = set(builtin_ids())
    agents = [_adjusted(agent, entries[agent.id]) if agent.id in entries else agent for agent in AGENTS]
    # An entry without a name adjusts a built-in agent this Richi does not have.
    agents += [_custom(identifier, entry) for identifier, entry in sorted(entries.items())
               if identifier not in known and "name" in entry]
    return tuple(agents)


def ids(environ=None, home=None):
    return tuple(agent.id for agent in registry(environ, home))


def describe(environ=None, home=None):
    """The known agents in order, as commands report them."""
    return [{"id": agent.id, "name": agent.name} for agent in registry(environ, home)]


def variables(agent):
    """The variables that mark the agent, as people write them: NAME or NAME=VALUE."""
    return list(agent.session_variables) + [name if value is None else name + "=" + value
                                            for name, value in agent.shell_variables.items()]


def _in_shell(agent, environ):
    return bool(agent.shell_variables) and all(
        bool(environ.get(name)) if value is None else environ.get(name) == value
        for name, value in agent.shell_variables.items())


def detect(environ=None):
    """The agent whose shell runs this command, or None.

    The chat identity an integration set wins; then a variable only an
    agent's own session has; then one its shells pass on to child processes.
    A process started from an agent's shell inherits that agent's variables,
    so when the variables of several agents are present the answer is None:
    the caller then says who it is (rules show --agent ID) rather than get
    another agent's skills.
    """
    environ = os.environ if environ is None else environ
    agents = registry(environ)
    prefix, separator, _rest = (environ.get("RICHI_CHAT_ID") or "").partition(":")
    for agent in agents:
        if separator and agent.id == prefix:
            return agent.id
    for matches in ([agent.id for agent in agents if any(environ.get(name) for name in agent.session_variables)],
                    [agent.id for agent in agents if _in_shell(agent, environ)]):
        if matches:
            return matches[0] if len(matches) == 1 else None
    return None
