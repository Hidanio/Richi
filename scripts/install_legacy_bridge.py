#!/usr/bin/env python3
"""Redirect legacy entry points to installed Richi, with verified rollback backups."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ENTRY_POINTS = {"memory.py": "richi", "serve.py": "richi.serve", "launch_map.py": "richi.launch_map"}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace(path, content, mode):
    fd, temporary = tempfile.mkstemp(prefix=".richi-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def wrapper(python, module, package_parent):
    return (
        '#!/usr/bin/env python3\n'
        '# Richi legacy compatibility bridge; rollback metadata is stored beside the old code.\n'
        'import os\nfrom pathlib import Path\nimport sys\n'
        'python = ' + repr(str(python)) + '\n'
        'database = str(Path(__file__).resolve().with_name("memory.sqlite3"))\n'
        'if __name__ == "__main__":\n'
        '    os.execv(python, [python, "-m", ' + repr(module) + ', "--db", database, *sys.argv[1:]])\n'
        'else:\n'
        '    from importlib import import_module\n'
        '    sys.path.insert(0, ' + repr(str(package_parent)) + ')\n'
        '    sys.modules[__name__] = import_module(' + repr('richi.memory' if module == 'richi' else module) + ')\n'
    ).encode("utf-8")


def install(legacy, python):
    legacy = legacy.expanduser().resolve()
    # Preserve the virtualenv executable symlink: resolving it would lose its environment.
    python = Path(os.path.abspath(str(python.expanduser())))
    if not legacy.is_dir() or not (legacy / "memory.sqlite3").is_file():
        raise ValueError("Legacy directory must contain the existing memory.sqlite3")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("--python must name an executable in the installed Richi environment")
    subprocess.run([str(python), "-m", "richi", "--db", str(legacy / "memory.sqlite3"), "check"],
                   check=True, capture_output=True, timeout=30)
    probe = subprocess.check_output(
        [str(python), "-c", "import richi; from pathlib import Path; print(Path(richi.__file__).resolve().parent.parent)"],
        text=True, timeout=10,
    )
    package_parent = Path(probe.strip())
    if not (package_parent / "richi" / "__init__.py").is_file():
        raise ValueError("Installed package must be accessible as ordinary files")
    for name in ENTRY_POINTS:
        path = legacy / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Expected a regular legacy entry point: " + str(path))
        if b"# Richi legacy compatibility bridge" in path.read_bytes():
            raise ValueError("Bridge already installed; restore it before changing the target environment")
    backup = Path(tempfile.mkdtemp(prefix="richi-bridge-backup-", dir=str(legacy)))
    records = []
    for name, module in ENTRY_POINTS.items():
        source = legacy / name
        saved = backup / name
        shutil.copy2(source, saved)
        content = wrapper(python, module, package_parent)
        records.append({"name": name, "original_sha256": digest(saved),
                        "installed_sha256": hashlib.sha256(content).hexdigest(),
                        "mode": source.stat().st_mode & 0o777})
    manifest = backup / "manifest.json"
    data = {"version": 1, "legacy_directory": str(legacy), "python": str(python),
            "created_at": datetime.now(timezone.utc).isoformat(), "files": records,
            "package_parent": str(package_parent)}
    manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    installed = []
    try:
        for record in records:
            name = record["name"]
            if digest(legacy / name) != record["original_sha256"]:
                raise ValueError("Legacy entry point changed during installation: " + name)
            replace(legacy / name, wrapper(python, ENTRY_POINTS[name], package_parent), record["mode"])
            installed.append(record)
    except Exception:
        for record in installed:
            path = legacy / record["name"]
            if digest(path) == record["installed_sha256"]:
                replace(path, (backup / record["name"]).read_bytes(), record["mode"])
        raise
    return {"status": "installed", "manifest": str(manifest), "database": str(legacy / "memory.sqlite3")}


def restore(manifest):
    manifest = manifest.expanduser().resolve()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("version") != 1 or {r["name"] for r in data["files"]} != set(ENTRY_POINTS):
        raise ValueError("Unrecognized bridge manifest")
    legacy = Path(data["legacy_directory"])
    # Validate all files before restoring any of them; never overwrite later edits.
    for record in data["files"]:
        current, saved = legacy / record["name"], manifest.parent / record["name"]
        if current.is_symlink() or saved.is_symlink():
            raise ValueError("Bridge files must be regular files")
        if digest(current) != record["installed_sha256"] or digest(saved) != record["original_sha256"]:
            raise ValueError("Bridge or backup changed: " + record["name"])
    for record in data["files"]:
        replace(legacy / record["name"], (manifest.parent / record["name"]).read_bytes(), record["mode"])
    return {"status": "restored", "manifest": str(manifest)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-dir", type=Path)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--restore", type=Path, help="Restore originals using an installation manifest")
    args = parser.parse_args()
    if args.restore and (args.legacy_dir or args.python):
        parser.error("Use --restore alone")
    if not args.restore and not (args.legacy_dir and args.python):
        parser.error("Use --legacy-dir PATH --python VENV_PYTHON, or --restore MANIFEST")
    try:
        result = restore(args.restore) if args.restore else install(args.legacy_dir, args.python)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
