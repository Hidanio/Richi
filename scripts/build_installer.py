#!/usr/bin/env python3
"""Build a standalone stdlib-only installer from the tested wheel."""
import argparse
import hashlib
from pathlib import Path
import zipfile


def build(wheel, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as target:
        names = [name for name in source.namelist() if name.startswith("richi_launcher/") or name == "richi_bootstrap.py"]
        for name in ("richi_launcher/installer.py", "richi_launcher/agent_setup.py", "richi_launcher/agent_skill/SKILL.md"):
            if name not in names:
                raise ValueError("Wheel lacks installer resource: " + name)
        for name in sorted(names):
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, source.read(name))
        licenses = [name for name in source.namelist() if name.endswith(".dist-info/licenses/LICENSE.md")]
        if len(licenses) != 1:
            raise ValueError("Wheel must include its license for the standalone installer")
        target.writestr(zipfile.ZipInfo("LICENSE.md", (2020, 1, 1, 0, 0, 0)), source.read(licenses[0]))
        target.writestr(zipfile.ZipInfo("__main__.py", (2020, 1, 1, 0, 0, 0)),
                        "from richi_launcher.installer import main\nraise SystemExit(main())\n")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(destination.suffix + ".sha256").write_text(digest + "  " + destination.name + "\n", encoding="ascii")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--output", type=Path, default=Path("dist/richi-installer.pyz"))
    args = parser.parse_args()
    build(args.wheel, args.output)
