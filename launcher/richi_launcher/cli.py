"""Stable command entry point. Config recovery never imports development code."""
import argparse
import json
import os
import sys

from .config import ConfigError, resolve_settings, set_config_value
from .runtime import ACTIVE_RUNTIME, installed_runtime, resolve_runtime, runtime_command


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise ConfigError(message)


def _selectors(argv):
    # Parse only global selectors, plus map's explicitly supported local ones.
    # Never reinterpret option-looking search text or the body of another command.
    options = {}
    position = 0
    while position < len(argv):
        token = argv[position]
        key, sep, value = token.partition("=")
        if key not in {"--config", "--db"}:
            break
        if not sep:
            position += 1
            if position >= len(argv):
                raise ConfigError(key + " requires a value")
            value = argv[position]
        options["config_file" if key == "--config" else "db"] = value
        position += 1
    command = argv[position] if position < len(argv) else None
    tail = argv[position + 1:]
    if command == "map":
        index = 0
        while index < len(tail):
            key, sep, value = tail[index].partition("=")
            if key in {"--config", "--db", "--port"}:
                if not sep:
                    index += 1
                    if index >= len(tail):
                        raise ConfigError(key + " requires a value")
                    value = tail[index]
                options[{"--config": "config_file", "--db": "db", "--port": "port"}[key]] = value
            index += 1
    return options, command, tail


def configuration(argv, selectors):
    parser = Parser(prog="richi config", description="Inspect settings or select installed/development code")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("show")
    change = commands.add_parser("set")
    change.add_argument("key", choices=("dev", "development.source"))
    change.add_argument("value")
    args = parser.parse_args(argv)
    if args.action == "set":
        value = args.value
        if args.key == "dev":
            if value not in {"true", "false"}:
                raise ConfigError("dev must be true or false")
            value = value == "true"
        set_config_value(args.key, value, config_file=selectors.get("config_file"))
    settings = resolve_settings(**selectors)
    result = settings.as_dict()
    try:
        result["runtime"] = dict(resolve_runtime(settings).as_dict(), available=True)
    except ConfigError as exc:
        result["runtime"] = {"mode": "dev" if settings.dev else "release", "available": False, "error": str(exc)}
    try:
        result["installed_runtime"] = installed_runtime().as_dict()
    except ConfigError as exc:
        result["installed_runtime"] = {"available": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        selectors, command, tail = _selectors(argv)
        if command == "config":
            return configuration(tail, selectors)
        settings = resolve_settings(**selectors)
        selected = resolve_runtime(settings)
        execution = runtime_command(selected, argv)
        environment = dict(os.environ)
        environment.pop(ACTIVE_RUNTIME, None)
        os.execve(execution[0], execution, environment)
    except (ConfigError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
