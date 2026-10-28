"""Bundle the canonical agent skill without maintaining a second source copy."""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithSkill(build_py):
    def run(self):
        super().run()
        source = Path(__file__).parent / "skills" / "project-memory"
        target = Path(self.build_lib) / "richi_launcher" / "agent_skill"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))


setup(cmdclass={"build_py": BuildWithSkill})
