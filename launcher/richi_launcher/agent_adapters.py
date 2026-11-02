"""Built-in agent catalog shared by setup, installer and CLI help.

Keep AGENTS a literal: release compatibility is inspected without executing code
from the candidate package. Versions describe the stored adapter data contract.
"""
import ast
import importlib
from pathlib import Path

from .config import ConfigError

AGENTS = {"codex": ("codex_setup", 1), "claude": ("claude_setup", 1)}


def get(name):
    if name not in AGENTS:
        raise ConfigError("Unsupported agent: " + str(name))
    return importlib.import_module("." + AGENTS[name][0], __package__)


def literal(path, name):
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return ast.literal_eval(node.value)
    except (OSError, SyntaxError, ValueError, TypeError) as exc:
        raise ConfigError("Cannot inspect agent contract: " + str(path)) from exc
    raise ConfigError("Missing agent contract " + name + " in " + str(path))


def release_catalog(site):
    launcher = Path(site) / "richi_launcher"
    if (launcher / "agent_adapters.py").is_file():
        catalog = literal(launcher / "agent_adapters.py", "AGENTS")
        if not isinstance(catalog, dict) or not catalog:
            raise ConfigError("Invalid release agent catalog")
        for name, spec in catalog.items():
            if (not isinstance(name, str) or not isinstance(spec, (list, tuple)) or len(spec) != 2
                    or not isinstance(spec[0], str) or not spec[0].isidentifier()
                    or type(spec[1]) is not int or spec[1] < 1
                    or not (launcher / (spec[0] + ".py")).is_file()):
                raise ConfigError("Invalid release agent adapter: " + str(name))
        return catalog
    # Historical packages predate the catalog. These are compatibility facts,
    # not fallback defaults for newly added agents.
    module = launcher / "agent_setup.py"
    catalog = {}
    if literal(module, "PROTOCOL") == 1:
        catalog["codex"] = ("codex_setup", 1)
    try:
        if literal(module, "MULTI_AGENT_PROTOCOL") == 1:
            catalog["claude"] = ("claude_setup", 1)
    except ConfigError:
        pass
    return catalog


def require_supported(site, records):
    catalog = release_catalog(site)
    for name, record in records.items():
        if name not in catalog or catalog[name][1] != record.get("adapter_version", 1):
            raise ConfigError("The selected release cannot maintain agent " + name + "; activation was cancelled")
    return catalog
