"""Release gate: the frozen 1.0.1 updater installs this release in place.

Builds the 1.0.1 wheel and installer from git tag v1.0.1, installs them into a
synthetic home and drives the commands a 1.0.1 user runs: its own update check
and apply (only GitHub transport and scheduler registration are replaced, as in
test_update_e2e), rollback through the original environment's console, Codex and
Claude Code setup. Needs git history with tags and the `build` package.

Usage: python tests/legacy_update_e2e.py DIST
DIST holds this release's wheel and its richi-release.json.
"""
import io
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from cli_environment import cli_environment  # noqa: E402
import test_update_e2e  # noqa: E402

LEGACY_TAG = "v1.0.1"
BASE = "https://github.com/Hidanio/Richi/releases/"
API = "https://api.github.com/repos/Hidanio/Richi/releases/"


def run(command, env=None, ok=True, stdin=None, cwd=None):
    result = subprocess.run([str(item) for item in command], env=env, input=stdin, cwd=cwd,
                            capture_output=True, text=True, timeout=600)
    if ok is not None and (result.returncode == 0) != ok:
        raise AssertionError("{} -> {}\n{}\n{}".format(command, result.returncode, result.stdout, result.stderr))
    return result


def build_legacy(work):
    """Build the published 1.0.1 artifacts with that tag's own scripts."""
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", LEGACY_TAG],
                             capture_output=True, check=True).stdout
    source = work / "legacy-source"
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(source)
    dist = work / "legacy-dist"
    run([sys.executable, "-m", "build", "--wheel", "--outdir", dist, source])
    wheel = next(dist.glob("richi-1.0.1-py3-none-any.whl"))
    # Explicit outputs and cwd: the scripts' defaults are relative to the caller.
    run([sys.executable, source / "scripts/release_manifest.py", wheel, "--tag", LEGACY_TAG,
         "--output", dist / "richi-release.json"], cwd=source)
    run([sys.executable, source / "scripts/build_installer.py", wheel,
         "--output", dist / "richi-installer.pyz"], cwd=source)
    return dist


def feed(root, dist):
    manifest_path = dist / "richi-release.json"
    manifest = json.loads(manifest_path.read_text())
    version, wheel = manifest["version"], dist / manifest["wheel"]["filename"]
    manifest_url = BASE + "download/v" + version + "/richi-release.json"
    wheel_url = BASE + "download/v" + version + "/" + wheel.name
    release = {"draft": False, "prerelease": False, "tag_name": "v" + version, "html_url": BASE + "tag/v" + version,
               "assets": [{"name": "richi-release.json", "state": "uploaded", "size": manifest_path.stat().st_size,
                           "browser_download_url": manifest_url},
                          {"name": wheel.name, "state": "uploaded", "size": wheel.stat().st_size,
                           "browser_download_url": wheel_url}]}
    release_path = root / (version + "-release.json")
    release_path.write_text(json.dumps(release))
    responses = {API + "tags/v" + version: str(release_path), API + "latest": str(release_path),
                 manifest_url: str(manifest_path), wheel_url: str(wheel)}
    path = root / (version + "-responses.json")
    path.write_text(json.dumps(responses))
    return path, version


def main():
    distribution = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="richi-legacy-update-") as directory:
        root = Path(directory).resolve()
        legacy = build_legacy(root)
        env = cli_environment(root, SHELL="/bin/zsh")
        for key in [key for key in env if key.startswith("CLAUDE") or key in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}]:
            env.pop(key)
        home = Path(env["HOME"])
        profile = home / ".claude"
        profile.mkdir(parents=True)
        foreign = {"hooks": [{"type": "command", "command": "/usr/bin/true"}], "matcher": ""}
        (profile / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [foreign]}}))
        runner = root / "offline.py"
        runner.write_text(test_update_e2e._OFFLINE_COMMAND)
        latest, target = feed(root, distribution)

        manifest = json.loads((legacy / "richi-release.json").read_text())
        installed = json.loads(run([sys.executable, "-I", legacy / "richi-installer.pyz", "--yes", "--no-path",
                                    "--wheel", legacy / manifest["wheel"]["filename"],
                                    "--manifest", legacy / "richi-release.json"], env).stdout)
        seed = Path(installed["prefix"])
        python, richi = seed / "bin/python", seed / "bin/richi"

        def cli(*args, ok=True, stdin=None):
            return run([richi, *args], env, ok=ok, stdin=stdin)

        def cli_json(*args, **kwargs):
            return json.loads(cli(*args, **kwargs).stdout)

        def update(*args, ok=True):
            return run([python, "-I", "-B", runner, latest, "update", *args], env, ok=ok)

        def version():
            return cli("--version").stdout.strip()

        def setup_state(*extra):
            data = cli_json("setup", *extra, "--status")
            return data["status"], data.get("changes", [])

        def schema(database):
            with sqlite3.connect("file:" + database + "?mode=ro", uri=True) as conn:
                return conn.execute("PRAGMA user_version").fetchone()[0]

        assert version() == "Richi 1.0.1" and setup_state() == ("configured", [])
        cli("init")
        alpha = cli_json("workspace", "create", "alpha")
        cli("--workspace", "alpha", "init")
        cli("--chat", "legacy-chat", "chat", "bind", "alpha")
        assert schema(alpha["database"]) == 2

        checked = json.loads(update("check").stdout)
        assert checked["status"] == "update_available" and checked["release"]["version"] == target, checked
        assert json.loads(update("apply").stdout)["status"] == "updated"
        assert version() == "Richi " + target and setup_state() == ("configured", []), setup_state()
        current = cli_json("--chat", "legacy-chat", "chat", "current")
        assert current["workspace"] == "alpha" and schema(alpha["database"]) == 2, current

        # The user's rollback: the original environment's 1.0.1 bootstrap picks the manager.
        cli("update", "rollback")
        assert version() == "Richi 1.0.1"
        cli("--workspace", "alpha", "check")
        cli("update", "rollback")
        assert version() == "Richi " + target

        # Documented edge: a schema-3 store after a rollback performed by 1.0.1.
        cli("--workspace", "alpha", "backup")
        assert cli_json("--workspace", "alpha", "migrate")["schema_version"] == 3
        cli("update", "rollback")
        assert version() == "Richi 1.0.1"
        refused = cli("--workspace", "alpha", "check", ok=False)
        assert "Unsupported schema version 3" in refused.stdout + refused.stderr, refused.stderr
        assert schema(alpha["database"]) == 3
        assert json.loads(update("apply").stdout)["status"] == "updated"
        assert version() == "Richi " + target and cli_json("--workspace", "alpha", "check")["status"] == "ok"

        cli("setup", "--agent", "claude", "--yes", "--no-path")
        assert setup_state("--agent", "claude") == ("configured", []) and setup_state() == ("configured", [])
        starts = json.loads((profile / "settings.json").read_text())["hooks"]["SessionStart"]
        assert foreign in starts and len(starts) == 2
        command = [item for item in starts if item != foreign][0]["hooks"][0]["command"]
        env_file = root / "claude-env"
        run(shlex.split(command), dict(env, CLAUDE_ENV_FILE=str(env_file)),
            stdin=json.dumps({"hook_event_name": "SessionStart", "session_id": "legacy-session"}))
        assert env_file.read_text() == "export RICHI_CHAT_ID=claude:legacy-session\n"

        # 1.0.1 cannot maintain Claude Code, so it refuses the rollback and changes nothing.
        settings = (profile / "settings.json").read_bytes()
        cli("update", "rollback", ok=False)
        assert version() == "Richi " + target and (profile / "settings.json").read_bytes() == settings
        assert setup_state() == ("configured", []) and setup_state("--agent", "claude") == ("configured", [])
    print("Legacy 1.0.1 in-place update passed for Richi " + target)


if __name__ == "__main__":
    main()
