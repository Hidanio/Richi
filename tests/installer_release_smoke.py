"""Release gate: exercise the exact downloaded installer and wheel, offline."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from cli_environment import cli_environment


def main():
    distribution = Path(sys.argv[1]).resolve()
    archive = distribution / "richi-installer.pyz"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == archive.with_suffix(".pyz.sha256").read_text().split()[0]
    manifest = distribution / "richi-release.json"
    release = json.loads(manifest.read_text())
    with tempfile.TemporaryDirectory(prefix="richi-release-install-") as directory:
        environment = cli_environment(directory, SHELL="/bin/zsh")
        command = [sys.executable, "-I", str(archive), "--yes", "--no-path",
                   "--wheel", str(distribution / release["wheel"]["filename"]),
                   "--manifest", str(manifest)]
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        installed = json.loads(result.stdout)
        console = Path(installed["prefix"]) / "bin/richi"
        result = subprocess.run([str(console), "setup", "--status"], env=environment,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["status"] == "configured"
        assert Path(installed["skill"], "SKILL.md").is_file()
        assert not list(Path(directory).rglob("*.sqlite3"))
    print("Exact release installer smoke test passed")


if __name__ == "__main__":
    main()
