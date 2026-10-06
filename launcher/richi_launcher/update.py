"""Explicit installation maintenance, independent of workspace and dev code."""
from . import installation, releases
from .config import ConfigError


def command(argv):
    from .cli import Parser
    parser = Parser(prog="richi update", description="Manage stable GitHub releases for this installation; keeps current and previous versions")
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("check", "apply"):
        action = actions.add_parser(name)
        action.add_argument("--version", help="Stable X.Y.Z release; defaults to latest")
    actions.add_parser("rollback", help="Switch to the previous installed version without downloading")
    actions.add_parser("cleanup", help="Retry removing retired versions after their processes exit")
    args = parser.parse_args(argv)
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
