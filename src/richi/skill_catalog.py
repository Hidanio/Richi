"""Agent skills installed for this user, for choosing the skill a standing rule names.

The map lists them so that a rule such as "use this skill for pull requests"
names a skill that exists. Only each skill's SKILL.md front matter is read:
its name and description. Nothing is executed, and the list is a convenience:
a rule may still name a skill this computer does not have.

Each agent in the agents registry says where it finds skills: its user-wide
skill directories, skills built into it, and the plugins it has installed
(for Claude Code also the claude.ai plugins the Claude app gives it). A plugin
skill (or a plugin command, for an agent that lists those as skills) is named
as the agents name it, "plugin:skill". Skills an agent never chooses by
itself, only when the user invokes them, are left out for it.
"""
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


def front_matter(text, cut=False):
    """The top-level scalar fields of a SKILL.md YAML header (a small, safe subset).

    cut: the text is only the beginning of the file. A header that does not
    end within it still gives the fields read so far, without the last line,
    which may be cut in two: a skill that says it is not for the agent to
    choose says so near the top. Prose read so far, a line that is no field,
    means the file opens with a Markdown rule, not a header.
    """
    if cut:
        text = text[:text.rfind("\n") + 1]
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields, index, prose = {}, 1, False
    while index < len(lines):
        line = lines[index]
        # A separator inside an indented YAML value is content. Only a top-level one ends the header.
        if line.rstrip(" \t") in {"---", "..."}:
            return fields
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
    return fields if cut and not prose else {}


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
                     re.search(rb"(?m)^(?:---|\.\.\.)[ \t]*\r?\n", rest))):
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


def _json(path):
    """A small JSON file's value, or None when it is missing, too large or not JSON."""
    try:
        data = _read_regular(path, MAX_MANIFEST + 1)
        return None if len(data) > MAX_MANIFEST else agents.parse_json(data.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        # RecursionError: brackets nested deeper than the parser goes. A plugin's author wrote the file.
        return None


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


def _plugins(layout, root):
    """(name, directory, enabled skill names or None) of each plugin under a plugin root."""
    if layout == "claude-app":
        yield from _claude_app_plugins(root)
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


def _candidates(agent, source, report):
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
    for name, directory, enabled in _plugins(layout, root):
        for child in _children(directory / "skills"):
            yield name + ":", child / "SKILL.md", child.name, enabled
        if agent.plugin_commands:
            # Some agents, such as Claude Code, list a plugin's commands among its skills.
            for command in _children(directory / "commands", files=".md"):
                yield name + ":", command, command.stem, enabled


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
    agent. On a rules read, skip plugin sources (their names are qualified)
    and unrelated skills.
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
            if rule_aliases_only and source.layout not in ("skills", "nested-skills"):
                continue
            state = folder_state(source.path) if source.enabled else None
            entry = {"agent": agent.id, "key": source.key, "kind": source.kind, "layout": source.layout,
                     "path": str(source.path), "exists": state == "ok", "denied": state == "denied",
                     "enabled": source.enabled, "added": source.added, "skills": 0}
            seen_roots.append(entry)
            if not entry["exists"] or not source.enabled:
                continue
            found = set()
            for prefix, path, fallback, enabled in _candidates(agent, source, entry):
                try:
                    text, cut = _read_header(path, frontmatter_only=True)
                    fields = front_matter(text, cut)
                except OSError:
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
            incomplete |= bool(entry.get("truncated"))
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
