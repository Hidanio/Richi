"""Agent skills installed for this user, for choosing the skill a standing rule names.

The map lists them so that a rule such as "use this skill for pull requests"
names a skill that exists. Only each skill's SKILL.md front matter is read:
its name and description. Nothing is executed, and the list is a convenience:
a rule may still name a skill this computer does not have.

Each agent in the agents registry says where it finds skills: its user-wide
skill directories, skills built into it, and the plugins it has installed
(for Claude Code also the claude.ai plugins the Claude app gives it). A plugin
skill (or a plugin command, for an agent that lists those as skills) is named
as the agent names it ("plugin:skill" in Claude Code, or unprefixed). Skills an
agent never chooses by itself, only when the user invokes them, are left out for it.
"""
import json
import os
from pathlib import Path
import re
import stat

from . import agents


# How many skills the list names. The counts go on past it, so that they stay true.
MAX_SKILLS = 1000
MAX_HEADER = 32768
MAX_DESCRIPTION = 600
MAX_MANIFEST = 4 * 1024 * 1024
# Maximum directory entries examined across one recursively searched skill source.
MAX_NESTED_ENTRIES = 100000
# Kinds of skill source, in the order the map groups their skills.
KINDS = ("skills", "system", "plugins")
# The distributed skill declares the rename in its YAML header. A user's own
# skill called "richi" does not take over rules that name "project-memory".
PREVIOUS_RICHI_SKILL = "project-memory"
_RENAME_MARKER = "# Previous Richi skill name: " + PREVIOUS_RICHI_SKILL


def folder_state(path):
    """"ok" for a directory this user can list, "missing" when there is none, "denied" otherwise.

    Path.is_dir raises for a folder behind a directory without access; one
    such skill folder must not take the whole list down.
    """
    try:
        mode = os.stat(path).st_mode
    except PermissionError:
        return "denied"
    except OSError:
        return "missing"
    if not stat.S_ISDIR(mode):
        return "missing"
    return "ok" if os.access(path, os.R_OK | os.X_OK) else "denied"


# YAML's white space is a space or a tab; other spaces, such as a no-break space, are text.
_COMMENT = re.compile(r"(?:^|[ \t])#")


def _scalar(value):
    """A YAML scalar as text: without its quotes, and without the comment after it."""
    value = value.strip()
    quote = value[:1]
    if quote not in ("'", '"') or not value[1:]:
        # In a plain scalar a comment begins with a "#" that follows white space.
        return _COMMENT.split(value, 1)[0].rstrip()
    index = 1
    while index < len(value):
        if quote == "'" and value.startswith("''", index) or quote == '"' and value[index] == "\\":
            # '' is a quote inside single quotes; a backslash escapes the next character inside double ones.
            index += 2
        elif value[index] == quote:
            rest = value[index + 1:].strip()
            if rest and not rest.startswith("#"):
                break
            inner = value[1:index]
            return inner.replace("''", "'") if quote == "'" else inner.replace('\\"', '"')
        else:
            index += 1
    return value


def _nested(line):
    """Whether an indented line opens a nested mapping or list rather than continuing a text."""
    line = line.strip()
    return line == "-" or line.startswith(("- ", "#")) or ": " in line or line.endswith(":")


def _parse_front_matter(text, cut=False, *, strict_fence=False):
    """(top-level scalar fields of a SKILL.md YAML header, whether a valid header block was present)."""
    if cut:
        text = text[:text.rfind("\n") + 1]
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, False
    end_markers = {"---"} if strict_fence else {"---", "..."}
    fields, index, prose = {}, 1, False
    while index < len(lines):
        line = lines[index]
        # A separator inside an indented YAML value is content. Only a top-level one ends the header.
        if line.rstrip(" \t") in end_markers:
            return ({}, False) if prose else (fields, True)
        index += 1
        if not line or line[0] in " \t#":
            continue
        if ":" not in line:
            prose = True
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if value[:1] in {">", "|"}:
            block = []
            while index < len(lines) and (not lines[index].strip() or lines[index][:1] in " \t"):
                block.append(lines[index].strip())
                index += 1
            joiner = " " if value[0] == ">" else "\n"
            fields[key] = joiner.join(part for part in block if part).strip()
        elif value or (index < len(lines) and lines[index][:1] in " \t" and lines[index].strip()
                       and not _nested(lines[index])):
            # A plain scalar may continue on indented lines, or begin on the line after its key.
            while index < len(lines) and lines[index][:1] in " \t" and lines[index].strip():
                value += " " + lines[index].strip()
                index += 1
            fields[key] = _scalar(value)
    return (fields, True) if cut and not prose else ({}, False)


def front_matter(text, cut=False):
    """The top-level scalar fields of a SKILL.md YAML header (a small, safe subset).

    cut: the text is only the beginning of the file. A header that does not
    end within it still gives the fields read so far, without the last line,
    which may be cut in two: a skill that says it is not for the agent to
    choose says so near the top. Prose read so far, a line that is no field,
    means the file opens with a Markdown rule, not a header.
    """
    return _parse_front_matter(text, cut)[0]


def _read_regular(path, limit, *, frontmatter_only=False):
    """Up to limit bytes of a regular file.

    A skill folder holds what a plugin's author put there. Anything that is
    not a file, such as a named pipe, which would block the scan for ever, is
    refused like a missing one.
    """
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("not a regular file: " + str(path))
        handle = os.fdopen(descriptor, "rb", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise
    with handle:
        if frontmatter_only:
            prefix = handle.read(min(1024, limit))
            first, newline, rest = prefix.partition(b"\n")
            if (len(prefix) < 1024 or newline and
                    (first.lstrip(b"\xef\xbb\xbf").strip() != b"---" or
                     re.search(rb"(?m)^---[ \t]*\r?\n", rest))):
                return prefix
            return prefix + handle.read(limit - len(prefix))
        return handle.read(limit)


def _read_header(path, *, frontmatter_only=False):
    """(the beginning of a text file, whether the file goes on past it)."""
    data = _read_regular(path, MAX_HEADER + 1, frontmatter_only=frontmatter_only)
    text = data[:MAX_HEADER].decode("utf-8", errors="replace")
    # Some editors begin a file with a byte order mark, which is not part of its first line.
    return text[1:] if text.startswith("\ufeff") else text, len(data) > MAX_HEADER


def _declares_richi_rename(text):
    """Only the marker in the branded skill's YAML header declares the alias."""
    for line in text.splitlines()[1:]:
        if line.rstrip(" \t") in {"---", "..."}:
            break
        if line == _RENAME_MARKER:
            return True
    return False


def _openai_yaml_allows(directory):
    """False when agents/openai.yaml keeps the agent (such as Codex) from choosing the skill itself."""
    try:
        text, _cut = _read_header(directory / "agents" / "openai.yaml")
    except OSError:
        return True
    section = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] not in " \t":
            section = line.split(":", 1)[0].strip()
        elif section == "policy" and ":" in line:
            key, value = line.split(":", 1)
            if key.strip() == "allow_implicit_invocation":
                return _scalar(value).lower() != "false"
    return True


def _agent_may_choose(agent, directory, fields):
    # A rule asks the agent to use the skill on its own; a skill only the
    # user may invoke stays out of the list (its name can still be typed).
    if agent.invocation == "front-matter":
        return str(fields.get("disable-model-invocation", "")).strip().lower() != "true"
    if agent.invocation == "openai-yaml":
        return _openai_yaml_allows(directory)
    # "none": the agent has no such mark, so every skill is offered.
    return True


def _children(directory, files=None):
    """Visible subdirectories by name, or files with this suffix; hidden ones hold agents' internal files."""
    try:
        children = sorted(directory.iterdir(), key=lambda child: child.name)
    except OSError:
        return []
    found = []
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            if (files is None and child.is_dir()) or (child.suffix == files and child.is_file()):
                found.append(child)
        except OSError:
            # A link into an unreadable directory does not hide its readable siblings.
            continue
    return found


def _strip_jsonc(text):
    """JSONC without // or /* */ comments and without trailing commas before } or ]."""
    without_comments, index, in_string = [], 0, False
    while index < len(text):
        char = text[index]
        if in_string:
            without_comments.append(char)
            if char == "\\" and index + 1 < len(text):
                without_comments.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
        elif char == '"':
            in_string = True
            without_comments.append(char)
            index += 1
        elif text.startswith("//", index):
            end = text.find("\n", index + 2)
            index = len(text) if end < 0 else end
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                return ""
            without_comments.append(" ")
            index = end + 2
        else:
            without_comments.append(char)
            index += 1
    cleaned = "".join(without_comments)
    result, index, in_string, after_value = [], 0, False, False
    while index < len(cleaned):
        char = cleaned[index]
        if in_string:
            result.append(char)
            if char == "\\" and index + 1 < len(cleaned):
                result.append(cleaned[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
                after_value = True
        elif char == '"':
            in_string = True
            result.append(char)
        elif char == ",":
            look = index + 1
            while look < len(cleaned) and cleaned[look] in " \t\r\n":
                look += 1
            if not after_value or look >= len(cleaned) or cleaned[look] not in "}]":
                result.append(char)
                after_value = False
        else:
            result.append(char)
            if char in "{[:":
                after_value = False
            elif char not in " \t\r\n":
                after_value = True
        index += 1
    return "".join(result)


def _no_constants(value):
    raise ValueError("invalid JSON constant: " + value)


def _parse_strict_json(text):
    return json.loads(text, parse_int=agents._short_integer, parse_constant=_no_constants)


def _json_state(path, jsonc=False, *, strict_constants=False):
    """(parsed JSON value or None, True when absent or readable, False on read/parse failure)."""
    try:
        data = _read_regular(path, MAX_MANIFEST + 1)
        if len(data) > MAX_MANIFEST:
            return None, False
        text = data.decode("utf-8-sig")
        raw = _strip_jsonc(text) if jsonc else text
        if strict_constants:
            return _parse_strict_json(raw), True
        return agents.parse_json(raw), True
    except FileNotFoundError:
        return None, True
    except (OSError, ValueError, RecursionError):
        # RecursionError: brackets nested deeper than the parser goes. A plugin's author wrote the file.
        return None, False


def _json(path, jsonc=False):
    """A small JSON file's value, or None when it is missing, too large or not JSON."""
    return _json_state(path, jsonc)[0]


def _fold_char(ch):
    if ch == "\u0131":
        return ch
    upper = ch.upper()
    upper = upper if len(upper) == 1 else ch
    lower = upper.lower()
    return lower if len(lower) == 1 else upper


def _equal_fold(left, right):
    """Go strings.EqualFold parity: 1-to-1 code-point case folding without 1-to-many expansions."""
    if left == right:
        return True
    if len(left) != len(right):
        return False
    if left.isascii() and right.isascii():
        return left.lower() == right.lower()
    return all(_fold_char(a) == _fold_char(b) for a, b in zip(left, right))


def _antigravity_switches(config, config_ok):
    """(plugins mapping, whether ~/.gemini/config/config.json could not be trusted)."""
    if not config_ok or (config is not None and not isinstance(config, dict)):
        return None, True
    if config is None:
        return {}, False
    plugins = config.get("plugins")
    if plugins is None:
        return {}, False
    if not isinstance(plugins, dict):
        return None, True
    for cfg in plugins.values():
        if not isinstance(cfg, dict):
            return None, True
        if cfg.get("enabled") is not None and type(cfg.get("enabled")) is not bool:
            return None, True
        if "installDir" in cfg and "install_dir" in cfg:
            return None, True
        for key in ("installDir", "install_dir"):
            if cfg.get(key) is not None and not isinstance(cfg.get(key), str):
                return None, True
    return plugins, False


def _valid_install_dir(value):
    return isinstance(value, str) and bool(value) and value[0] != "." and not any(ch in value for ch in "/\\\x00")


def _plugin_install_dir(key, cfg):
    if not isinstance(cfg, dict):
        return ""
    raw = cfg.get("installDir") if "installDir" in cfg else cfg.get("install_dir")
    if not _valid_install_dir(raw):
        return ""
    if _equal_fold(raw, key):
        return raw
    at = raw.rfind("@")
    prefix = raw[:at] if at > 0 else ""
    return raw if _equal_fold(prefix, key) else ""


def _plugin_lookup(switches, target):
    if target in switches:
        return target, switches[target], True
    matches = [key for key in switches if _equal_fold(key, target)]
    if not matches:
        return "", None, False
    best = min(matches)
    return best, switches[best], True


def _resolve_antigravity_plugin(dir_name, switches):
    """Go plugindir.Resolve parity: (resolved config key, whether this plugin directory is active)."""
    if not switches:
        return dir_name, True
    key, cfg, found = _plugin_lookup(switches, dir_name)
    if found:
        installed = _plugin_install_dir(key, cfg)
        return key, (not installed or _equal_fold(installed, dir_name))
    by_install = [candidate for candidate, entry in switches.items()
                  if _equal_fold(_plugin_install_dir(candidate, entry), dir_name)]
    if by_install:
        return min(by_install), True
    at = dir_name.rfind("@")
    if at > 0:
        key, cfg, found = _plugin_lookup(switches, dir_name[:at])
        if found:
            installed = _plugin_install_dir(key, cfg)
            if not installed or _equal_fold(installed, dir_name):
                return dir_name, True
            return key, False
    return dir_name, True


def _antigravity_manifest(directory):
    """(parsed manifest dict or None, whether an unreadable directory/file left visibility uncertain)."""
    unreadable = False
    for candidate_dir in (directory / ".antigravity-plugin", directory / ".gemini-plugin", directory):
        try:
            entries = sorted(candidate_dir.iterdir(), key=lambda item: item.name)
        except (FileNotFoundError, NotADirectoryError):
            if candidate_dir == directory:
                return None, unreadable
            continue
        except OSError:
            unreadable = True
            if candidate_dir == directory:
                return None, True
            continue
        for entry in entries:
            if _equal_fold(entry.name, "plugin.json"):
                try:
                    data = _read_regular(entry, MAX_MANIFEST + 1)
                except PermissionError:
                    return None, True
                except OSError:
                    return None, unreadable
                if len(data) > MAX_MANIFEST:
                    return None, unreadable
                try:
                    loaded = _parse_strict_json(_strip_jsonc(data.decode("utf-8-sig")))
                except (UnicodeError, ValueError, RecursionError):
                    return None, unreadable
                return (loaded if isinstance(loaded, dict) else None), unreadable
    return None, unreadable


def _scan_ambiguous_antigravity_skills(directory, plugin_state):
    """Conservatively flag potential richi/project-memory skills when plugin state cannot be verified."""
    agent = plugin_state["agent"]
    skills_dir = directory / "skills"
    try:
        entries = sorted(skills_dir.iterdir(), key=lambda item: item.name)
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError:
        plugin_state["config_incomplete"] = True
        return
    for child in entries:
        if child.name.startswith("."):
            continue
        try:
            if not child.is_dir():
                continue
            text, cut = _read_header(child / "SKILL.md", frontmatter_only=True)
            fields, has_header = _parse_front_matter(text, cut, strict_fence=agent.requires_frontmatter)
        except PermissionError:
            plugin_state["config_incomplete"] = True
            continue
        except OSError:
            continue
        if not has_header:
            continue
        own = fields.get("name")
        own = own.strip() if isinstance(own, str) and own.strip() else child.name
        if own in {"richi", PREVIOUS_RICHI_SKILL} and _agent_may_choose(agent, child, fields):
            plugin_state["config_incomplete"] = True
            if own == "richi":
                plugin_state.setdefault("ambiguous_richi_paths", set()).add(str(child))


def _antigravity_plugins(root, home, plugin_state=None):
    """(name, directory, None) of enabled Antigravity plugins under a plugin root."""
    config, config_ok = _json_state(home / ".gemini" / "config" / "config.json", strict_constants=True)
    switches, config_unknown = _antigravity_switches(config, config_ok)
    seen = set()
    for directory in _children(root):
        manifest, manifest_unreadable = _antigravity_manifest(directory)
        if plugin_state is not None and (manifest_unreadable or (config_unknown and manifest is not None)):
            _scan_ambiguous_antigravity_skills(directory, plugin_state)
        if manifest is None:
            continue
        if not config_unknown:
            resolved_name, active = _resolve_antigravity_plugin(directory.name, switches)
            if not active:
                continue
        else:
            resolved_name = directory.name
        override = switches.get(resolved_name) if isinstance(switches, dict) else None
        if isinstance(override, dict) and type(override.get("enabled")) is bool:
            enabled = override["enabled"]
        else:
            enabled = manifest.get("disabled") is not True
        if not enabled:
            continue
        raw_name = manifest.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else resolved_name
        if name in seen:
            continue
        seen.add(name)
        yield name, directory, None


def _plugin_name(directory):
    manifest = _json(directory / ".claude-plugin" / "plugin.json")
    name = manifest.get("name") if isinstance(manifest, dict) else None
    return name if isinstance(name, str) and name else None


def _listed(manifest, key, field):
    """Values of field in the manifest's key list, or None without a readable list."""
    items = manifest.get(key) if isinstance(manifest, dict) else None
    if not isinstance(items, list):
        return None
    return {item[field] for item in items if isinstance(item, dict) and isinstance(item.get(field), str)
            and item.get("enabled") is not False}


def _claude_app_plugins(root):
    """(name, directory, enabled skill names or None) of the plugins the Claude app gives Claude Code."""
    for account in _children(root):
        if account.name == "skills-plugin":
            # Built-in skills of the app; its manifest says which ones are enabled.
            for owner in _children(account):
                for directory in _children(owner):
                    name = _plugin_name(directory)
                    if name:
                        yield name, directory, _listed(_json(directory / "manifest.json"), "skills", "name")
            continue
        for organization in _children(account):
            # claude.ai plugins: rpm/manifest.json lists those installed now.
            rpm = organization / "rpm"
            installed = _listed(_json(rpm / "manifest.json"), "plugins", "id")
            for directory in _children(rpm):
                name = _plugin_name(directory)
                if name and (installed is None or directory.name in installed):
                    yield name, directory, None


_VERSION = re.compile(r"\d+(\.\d+)*\Z")


def _installed_version(plugin):
    """The cached version an agent loads.

    The highest number when every folder is named like a version; a cache
    keyed by commit has no order, so there the folder created last wins. A
    folder's time changes whenever something is written into it, which is why
    it is only the fallback.
    """
    versions = _children(plugin)
    if versions and all(_VERSION.fullmatch(version.name) for version in versions):
        return max(versions, key=lambda version: tuple(int(part) for part in version.name.split(".")))
    dated = []
    for version in versions:
        try:
            dated.append((version.stat().st_mtime, version.name, version))
        except OSError:
            continue
    return max(dated)[2] if dated else None


def _plugins(layout, root, home, plugin_state=None):
    """(name, directory, enabled skill names or None) of each plugin under a plugin root."""
    if layout == "claude-app":
        yield from _claude_app_plugins(root)
        return
    if layout == "antigravity-plugins":
        yield from _antigravity_plugins(root, home, plugin_state)
        return
    for marketplace in _children(root):
        for plugin in _children(marketplace):
            version = _installed_version(plugin)
            if version is not None:
                yield plugin.name, version, None


def _nested_folders(root):
    """Folders containing SKILL.md in breadth-first order and whether the scan hit its entry budget."""
    try:
        info = root.stat()
    except OSError:
        return [], False
    seen = {(info.st_dev, info.st_ino)}
    directories = [root]
    skills = []
    remaining = MAX_NESTED_ENTRIES
    limited = False
    for directory in directories:
        children = []
        try:
            with os.scandir(directory) as entries:
                for child in entries:
                    if not remaining:
                        limited = True
                        break
                    remaining -= 1
                    if child.name == "SKILL.md":
                        skills.append(directory)
                    try:
                        if child.name.startswith(".") or not child.is_dir():
                            continue
                        info = child.stat()
                    except OSError:
                        continue
                    children.append((child.name, (info.st_dev, info.st_ino)))
        except OSError:
            pass
        for name, identity in sorted(children):
            if identity not in seen:
                seen.add(identity)
                directories.append(directory / name)
        if limited:
            break
    return skills, limited


def _candidates(agent, source, home, report, plugin_state=None):
    """(name prefix, file with front matter, name without one, enabled names or None) under a root."""
    layout, root = source.layout, source.path
    if layout in ("skills", "nested-skills"):
        if layout == "nested-skills":
            children, limited = _nested_folders(root)
            if limited:
                report["truncated"] = True
        else:
            children = _children(root)
        for child in children:
            yield "", child / "SKILL.md", child.name, None
        return
    prefix_for = (lambda _name: "") if layout == "antigravity-plugins" else (lambda name: name + ":")
    for name, directory, enabled in _plugins(layout, root, home, plugin_state):
        prefix = prefix_for(name)
        for child in _children(directory / "skills"):
            yield prefix, child / "SKILL.md", child.name, enabled
        if agent.plugin_commands:
            # Some agents, such as Claude Code, list a plugin's commands among its skills.
            for command in _children(directory / "commands", files=".md"):
                yield prefix, command, command.stem, enabled


def catalog(environ=None, home=None, registry=None, *, rule_aliases_only=False):
    """Skills by name with their description, kind and the agents that have them.

    registry: the agents when the caller has them already (agents.registry),
    so that one answer describes one version of the agent settings.

    roots lists every skill source of every agent, in registry order, with
    whether its directory exists (or may not be read: denied), whether the
    user switched it off and how many skills it gives its agent. A source
    switched off is not looked at, so neither is said of it: it may be on a
    share that no longer answers, and a stat would wait for it. totals has
    the number of skills each agent has in all. The list itself names at most
    MAX_SKILLS skills and says so in truncated; the counts include the rest.
    When a recursive source exceeds the scan budget, its root and the catalog
    are marked truncated and the counts include only discovered skills.

    rule_aliases maps a stored rule's skill to its installed name for each
    agent. On a rules read, skip qualified plugin sources and unrelated skills.
    """
    environ = os.environ if environ is None else environ
    home = Path(home) if home is not None else Path.home()
    skills, seen_roots, truncated, totals, aliases = {}, [], False, {}, {}
    richi_paths = {}
    listed = agents.registry(environ, home) if registry is None else registry
    for agent in listed:
        every = set()
        renamed = set()
        incomplete = False
        try:
            disabled, enabled_paths = agent.skill_path_settings(environ, home) if agent.skill_path_settings else ((), ())
        except (ValueError, OSError, RuntimeError):
            # An unreadable profile cannot prove that a conflicting skill is
            # disabled, or that a renamed skill is enabled for a standing rule.
            disabled, enabled_paths, incomplete = (), (), True
        for source in agent.sources(environ, home):
            if rule_aliases_only and source.layout not in ("skills", "nested-skills", "antigravity-plugins"):
                continue
            state = folder_state(source.path) if source.enabled else None
            entry = {"agent": agent.id, "key": source.key, "kind": source.kind, "layout": source.layout,
                     "path": str(source.path), "exists": state == "ok", "denied": state == "denied",
                     "enabled": source.enabled, "added": source.added, "skills": 0}
            seen_roots.append(entry)
            if not entry["exists"] or not source.enabled:
                continue
            found = set()
            plugin_state = {"agent": agent}
            for prefix, path, fallback, enabled in _candidates(agent, source, home, entry, plugin_state):
                try:
                    if source.kind != "plugins" and not agent.follows_skill_file_links and path.is_symlink():
                        continue
                    text, cut = _read_header(path, frontmatter_only=True)
                    fields, has_header = _parse_front_matter(text, cut, strict_fence=agent.requires_frontmatter)
                except OSError:
                    continue
                if agent.requires_frontmatter and not has_header:
                    continue
                own = fields.get("name")
                own = own.strip() if isinstance(own, str) and own.strip() else fallback
                name = prefix + own
                if rule_aliases_only and name not in {"richi", PREVIOUS_RICHI_SKILL}:
                    continue
                # Only names a rule can store, of skills the agent may choose and has enabled, are offered.
                if not agents.valid_skill(name) or not _agent_may_choose(agent, path.parent, fields) \
                        or (enabled is not None and own not in enabled and fallback not in enabled):
                    continue
                canonical = os.path.realpath(path) if disabled else None
                possibly_disabled = canonical in disabled
                if possibly_disabled and canonical not in enabled_paths:
                    continue
                found.add(name)
                if name == "richi":
                    renamed.add(not possibly_disabled and fields.get("name") == "richi" and _declares_richi_rename(text))
                    if rule_aliases_only:
                        richi_paths.setdefault(agent.id, set()).add(str(path.parent))
                if name not in skills and len(skills) >= MAX_SKILLS:
                    # Counted for its source and its agent, but the list is full.
                    truncated = True
                    continue
                skill = skills.setdefault(name, {"name": name, "description": "", "kind": source.kind, "agents": []})
                if not skill["description"]:
                    description = " ".join(str(fields.get("description") or "").split())
                    skill["description"] = (description[:MAX_DESCRIPTION - 1] + "…"
                                            if len(description) > MAX_DESCRIPTION else description)
                if agent.id not in skill["agents"]:
                    skill["agents"].append(agent.id)
            entry["skills"] = len(found)
            every |= found
            incomplete |= bool(entry.get("truncated")) or bool(plugin_state.get("config_incomplete"))
            if rule_aliases_only and plugin_state.get("ambiguous_richi_paths"):
                richi_paths.setdefault(agent.id, set()).update(plugin_state["ambiguous_richi_paths"])
        if renamed == {True} and PREVIOUS_RICHI_SKILL not in every and not incomplete:
            aliases[agent.id] = {PREVIOUS_RICHI_SKILL: "richi"}
        totals[agent.id] = len(every)
    ordered = sorted(skills.values(), key=lambda skill: (KINDS.index(skill["kind"]), skill["name"].lower()))
    scan_truncated = any(root.get("truncated") for root in seen_roots)
    result = {"skills": ordered, "roots": seen_roots, "totals": totals, "truncated": truncated or scan_truncated,
              "agents": [{"id": agent.id, "name": agent.name} for agent in listed], "rule_aliases": aliases}
    if rule_aliases_only:
        result["richi_paths"] = {agent: sorted(paths) for agent, paths in richi_paths.items()}
    return result
