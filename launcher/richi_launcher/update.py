"""Explicit installation maintenance, independent of workspace and dev code."""
import re

from . import installation, releases
from .config import ConfigError


def _interval(value):
    match = re.fullmatch(r"([1-9][0-9]*)(m|h|d)", value)
    if match is None:
        raise ConfigError("Interval must use whole minutes, hours or days, for example 15m, 6h or 1d")
    seconds = int(match.group(1)) * {"m": 60, "h": 3600, "d": 86400}[match.group(2)]
    if not 900 <= seconds <= 604800:
        raise ConfigError("Automatic update interval must be between 15m and 7d")
    return seconds


def command(argv):
    from .cli import Parser
    parser = Parser(prog="richi update", description="Manage stable GitHub releases for this installation; keeps current and previous versions")
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("check", "apply"):
        action = actions.add_parser(name)
        action.add_argument("--version", help="Stable X.Y.Z release; defaults to latest")
    actions.add_parser("rollback", help="Switch to the previous installed version without downloading")
    actions.add_parser("cleanup", help="Retry removing retired versions after their processes exit")
    auto = actions.add_parser("auto", help="Configure background release checks and optional automatic installation")
    automatic = auto.add_subparsers(dest="auto_action", required=True)
    enable = automatic.add_parser("enable", help="Enable background checks; installation requires explicit --install opt-in")
    enable.add_argument("--interval", default="6h", help="Check interval: 15m to 7d (default: 6h)")
    enable.add_argument("--install", action="store_true", help="Also install releases automatically (off by default)")
    automatic.add_parser("disable", help="Disable background checks and installation; remove the owned OS schedule")
    automatic.add_parser("status", help="Inspect the schedule and recent attempts without fetching a release")
    automatic.add_parser("run", help="Perform one check only when enabled and due; used by the OS scheduler")
    args = parser.parse_args(argv)
    if args.action == "auto":
        from . import auto_update
        if args.auto_action == "enable":
            return auto_update.enable(interval_seconds=_interval(args.interval), auto_install=args.install)
        return {"disable": auto_update.disable, "status": auto_update.status,
                "run": auto_update.run}[args.auto_action]()
    if args.action == "rollback":
        return installation.rollback()
    if args.action == "cleanup":
        return installation.cleanup()
    local = installation.status()
    if args.action == "apply" and not local.get("supported"):
        raise ConfigError(local.get("reason", "This installation does not support managed updates"))
    release = releases.fetch_release(args.version)
    if release is None:
        return {"status": "no_release", "installation": local,
                "message": "No stable release has been published yet"}
    if args.action == "apply":
        return installation.apply_release(release)
    current = local.get("current") or {}
    version = current.get("version")
    candidate = release["version"]
    try:
        comparison = releases.version_tuple(candidate) > releases.version_tuple(version)
        older = releases.version_tuple(candidate) < releases.version_tuple(version)
    except ConfigError:
        comparison, older = True, False
    verified_same = (candidate == version and current.get("wheel_sha256") ==
                     release["manifest"]["wheel"]["sha256"])
    state = ("up_to_date" if verified_same else "older_release" if older else
             "update_available" if comparison else "release_available")
    return {"status": state, "installation": local,
            "release": {"version": candidate, "tag": release["tag"], "url": release["html_url"]},
            "can_apply": bool(local.get("supported")) and not older and not verified_same,
            "scope": "All release-mode workspaces using this installation; dev settings and data stay unchanged",
            "message": ("This installation has no verified wheel identity for this release"
                        if state == "release_available" else state.replace("_", " "))}
