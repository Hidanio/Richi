#!/usr/bin/env python3
"""Build the public release manifest from a wheel and its source checkout."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "launcher"))

from richi_launcher.config import ConfigError
from richi_launcher.releases import (MANIFEST_FILENAME, MAX_WHEEL_SIZE, PROTOCOL,
                                     _source_version, validate_manifest,
                                     verify_wheel, version_tuple)


def build_manifest(wheel, source_root=ROOT, tag=None):
    """Validate source and wheel versions and return deterministic public metadata."""
    source_root = Path(source_root)
    wheel = Path(wheel)
    project = (source_root / "pyproject.toml").read_text(encoding="utf-8")
    sections = re.findall(r"(?ms)^\[project\]\s*\n(.*?)(?=^\[|\Z)", project)
    if len(sections) != 1:
        raise ConfigError("pyproject.toml must have one [project] section")
    versions = re.findall(r"(?m)^version\s*=\s*['\"]([^'\"]+)['\"]\s*(?:#.*)?$", sections[0])
    if len(versions) != 1:
        raise ConfigError("pyproject.toml must have one literal project version")
    version = versions[0]
    version_tuple(version)
    source_version = _source_version((source_root / "src/richi/__init__.py").read_bytes())
    if source_version != version:
        raise ConfigError("pyproject.toml and richi.__version__ disagree")
    if tag is not None and tag != "v" + version:
        raise ConfigError("Release tag does not match the package version")
    size = wheel.stat().st_size
    if not 0 < size <= MAX_WHEEL_SIZE:
        raise ConfigError("Release wheel exceeds the allowed size")
    digest = hashlib.sha256()
    with wheel.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    manifest = validate_manifest({
        "format_version": 1,
        "version": version,
        "updater_protocol": PROTOCOL,
        "python_min": [3, 9],
        "database_schemas": [1, 2],
        "map_schema": 2,
        "wheel": {"filename": wheel.name, "sha256": digest.hexdigest(), "size": size},
    })
    verify_wheel(wheel, manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--output", type=Path, default=Path("dist") / MANIFEST_FILENAME)
    parser.add_argument("--tag", help="Exact vX.Y.Z tag being released")
    args = parser.parse_args(argv)
    try:
        manifest = build_manifest(args.wheel, tag=args.tag)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (ConfigError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
