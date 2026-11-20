"""Explicit installation maintenance, independent of workspace and dev code."""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

from . import installation, maps, releases
from .config import ConfigError


def _interval(value):
    """Argument type of --interval: seconds of a 15m to 7d duration."""
    match = re.fullmatch(r"([1-9][0-9]*)(m|h|d)", value)
    if match is None:
        raise argparse.ArgumentTypeError("Interval must use whole minutes, hours or days, for example 15m, 6h or 1d")
    seconds = int(match.group(1)) * {"m": 60, "h": 3600, "d": 86400}[match.group(2)]
    if not 900 <= seconds <= 604800:
        raise argparse.ArgumentTypeError("Automatic update interval must be between 15m and 7d")
    return seconds


def _version(value):
    """Argument type of --version: a stable X.Y.Z release."""
    try:
        releases.version_tuple(value)
    except ConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def parser():
    from .cli import Parser
    from .help_format import complete, document
    parser = Parser(prog="richi update")
    actions = parser.add_subparsers(dest="action", required=True)
    pages = {}
    for name, summary in (("check", "Compare this installation with a stable release; nothing changes"),
                          ("apply", "Download, verify and switch to a stable release")):
        pages[name] = action = actions.add_parser(name, help=summary)
        action.add_argument("--version", type=_version, metavar="X.Y.Z", help="Stable X.Y.Z release (default: the latest)")
    pages["apply"].add_argument("--review-preferences", action="store_true",
                                help="After activation, show historical preference entries from the selected workspace")
    rollback = actions.add_parser("rollback", help="Switch back to the previous installed version without downloading")
    cleanup = actions.add_parser("cleanup", help="Remove retired versions once no running process uses them")
    auto = actions.add_parser("auto", help="Manage background release checks and optional automatic installation")
    automatic = auto.add_subparsers(dest="auto_action", required=True)
    enable = automatic.add_parser("enable", help="Turn on background release checks; installation needs --install")
    enable.add_argument("--interval", type=_interval, default="6h", metavar="DURATION", help="Check interval from 15m to 7d, e.g. 30m, 6h, 1d (default: 6h)")
    enable.add_argument("--install", action="store_true", help="Also install new releases automatically (off by default)")
    disable = automatic.add_parser("disable", help="Turn off checks and installation and remove the OS schedule")
    state = automatic.add_parser("status", help="Show the schedule and recent attempts without fetching a release")
    run = automatic.add_parser("run", help="Run one check if enabled and due; this is what the OS scheduler calls")
    document(parser, "Check, install or roll back stable Richi releases.", """
        Updates are installation-wide: they replace the code for every release-mode
        workspace and keep the previous version for rollback. Workspace data,
        chat bindings and dev settings stay unchanged, and no database is migrated.
        Workspace and storage options are rejected here.""",
             examples=(("Is a newer release available?", "richi update check"),
                       ("Install the latest stable release", "richi update apply"),
                       ("Something broke: go back", "richi update rollback"),
                       ("Check every 6 hours; install only with --install", "richi update auto enable --interval 6h")),
             see=("config", "setup"), guide="docs/UPDATES.md")
    document(pages["check"], about="""
        Fetches release metadata from GitHub and reports whether it can be applied
        to this installation.""",
             examples=(("", "richi update check"), ("A specific release", "richi update check --version 1.1.0")))
    document(pages["apply"], about="""
        The current version stays available for richi update rollback. Requires a
        managed installation in a dedicated virtual environment. To review historical
        preferences immediately, add --review-preferences or accept the terminal prompt.
        When upgrading from a launcher without this option, run update apply and then
        rules review-preferences as a separate command in the new version.
        The read uses the chat binding or terminal workspace selection.""",
             examples=(("", "richi update apply"), ("A specific release", "richi update apply --version 1.1.0"),
                       ("Show preferences awaiting confirmation", "richi update apply --review-preferences")))
    document(rollback, about="""
        Works even if the current version's files are damaged. Package rollback
        does not roll back databases, so a release that would drop support for the
        current schema is refused.""", examples=(("", "richi update rollback"),))
    document(cleanup, examples=(("", "richi update cleanup"),))
    document(auto, about="""
        Checks only record that a release is available; agents then mention it at
        the start of a chat. Automatic installation is a separate opt-in.""",
             examples=(("Check every 6 hours", "richi update auto enable --interval 6h"),
                       ("Also install automatically", "richi update auto enable --install"),
                       ("", "richi update auto status")))
    document(enable, about="""
        Installs an OS schedule owned by Richi. Without --install it only records
        available releases.""",
             examples=(("", "richi update auto enable --interval 6h"), ("", "richi update auto enable --interval 1d --install")))
    document(disable, examples=(("", "richi update auto disable"),))
    document(state, examples=(("", "richi update auto status"),))
    document(run, examples=(("", "richi update auto run"),))
    return complete(parser)


def _offer_review(chat_id):
    from .cli import _interactive
    if (not _interactive(chat_id) or not sys.stderr.isatty()
            or any(os.environ.get(key) for key in ("RICHI_CHAT_ID", "CODEX_THREAD_ID", "CLAUDECODE", "CURSOR_AGENT"))):
        return False
    print("Richi has been updated. Show historical preferences from the selected workspace now? [y/N] ",
          file=sys.stderr, end="", flush=True)
    try:
        return sys.stdin.readline().strip().casefold() in {"y", "yes", "д", "да"}
    except (KeyboardInterrupt, OSError, ValueError):
        return False


def _preference_review(chat_id):
    # Re-enter the public bootstrap: it selects and leases the newly activated
    # launcher, which checks the chat binding and resolves the workspace runtime.
    command = [sys.executable, "-I", "-B", "-c", "from richi_bootstrap import main; raise SystemExit(main())"]
    if chat_id is not None:
        command += ["--chat", chat_id]
    command += ["--compact-json", "rules", "review-preferences", "--limit", "3"]
    try:
        # A development runtime can differ from this launcher. Bound its output even
        # if it ignores the requested page size or produces a large diagnostic.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            review = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=output, stderr=errors, timeout=30)
            stream = errors if review.returncode else output
            stream.seek(0)
            maximum = 16384 if review.returncode else 1024 * 1024
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            return {"error": "Preference review exceeded the output limit; use rules review-preferences --limit 1"}
        if review.returncode:
            try:
                problem = json.loads(data)
            except ValueError:
                problem = None
            if isinstance(problem, dict) and isinstance(problem.get("error"), str):
                return problem
            return {"error": data.decode("utf-8", errors="replace").strip()[:2000] or "Cannot read the selected workspace"}
        payload = json.loads(data)
        if isinstance(payload, dict) and isinstance(payload.get("candidates"), list):
            return payload
        return {"error": "The selected runtime did not return preference candidates"}
    except KeyboardInterrupt:
        return {"error": "Preference review cancelled"}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)}


def command(argv, *, chat_id=None):
    args = parser().parse_args(argv)
    if args.action == "auto":
        from . import auto_update
        if args.auto_action == "enable":
            return auto_update.enable(interval_seconds=args.interval, auto_install=args.install)
        return {"disable": auto_update.disable, "status": auto_update.status,
                "run": auto_update.run}[args.auto_action]()
    if args.action == "rollback":
        return maps.after_activation(installation.rollback())
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
        # Running maps move to the activated release once no update lock is held.
        result = maps.after_activation(installation.apply_release(release))
        if result["status"] in {"updated", "already_current"}:
            if args.review_preferences or (result["status"] == "updated" and _offer_review(chat_id)):
                result["preference_review"] = _preference_review(chat_id)
                result["preference_review"]["command"] = "richi rules review-preferences"
            elif result["status"] == "updated":
                result["preference_review"] = {"command": "richi rules review-preferences",
                                               "message": "The next rules load shows candidates for its scope."}
        return result
    current = local.get("current") or {}
    candidate = release["version"]
    assessment = installation.assess_release(release["manifest"], current)
    return {"status": assessment["status"], "installation": local,
            "release": {"version": candidate, "tag": release["tag"], "url": release["html_url"]},
            "can_apply": bool(local.get("supported")) and assessment["can_apply"],
            "scope": "All release-mode workspaces using this installation; dev settings and data stay unchanged",
            "message": assessment["message"]}
