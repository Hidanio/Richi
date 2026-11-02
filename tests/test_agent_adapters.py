"""A third, synthetic adapter exercises the shared flow without special cases."""
import contextlib
import io
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import MagicMock, patch

import test_agent_setup
from richi_launcher import agent_adapters, agent_setup, installation, installer, setup_files
from richi_launcher.config import ConfigError


class AgentAdapterTests(test_agent_setup.AgentSetupFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.profile = self.home / ".demo"
        adapter = ModuleType("richi_launcher.demo_setup")
        adapter.LABEL = "Demo agent"
        adapter.INSTRUCTION_LIMIT = None
        adapter.detected = lambda: True
        adapter.paths = lambda current=None: (self.profile, self.profile / "POLICY.md",
                                               self.profile / "skills/project-memory")
        adapter.setup_operations = lambda ctx, profile, current, replace: self.extra_operations("seed")
        adapter.activation_operations = lambda ctx, record, site: self.extra_operations(
            agent_adapters.literal(site / "richi_launcher/demo_setup.py", "DATA"))
        for item in (patch.dict(agent_adapters.AGENTS, {"demo": ("demo_setup", 1)}),
                     patch.dict(sys.modules, {adapter.__name__: adapter})):
            item.start()
            self.addCleanup(item.stop)

    def extra_operations(self, value):
        return {"demo_data": value}, [setup_files.change(
            self.profile / "session.cfg", setup_files.file_value(value.encode()))]

    def catalog(self, site, data):
        launcher = site / "richi_launcher"
        (launcher / "agent_adapters.py").write_text("AGENTS = " + repr(agent_adapters.AGENTS) + "\n")
        (launcher / "codex_setup.py").write_text("# fixture\n")
        (launcher / "demo_setup.py").write_text("DATA = " + repr(data) + "\n")

    def test_catalog_drives_discovery_menu_installer_and_setup(self):
        before = self.paths()
        items = agent_setup.agents_command(["--status"])["agents"]
        self.assertEqual(items[-1], {"agent": "demo", "detected": True, "status": "not_configured"})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(installer.main(["--agent", "demo", "--dry-run",
                                             "--prefix", str(self.home / "new-venv")]), 0)
        self.assertEqual(json.loads(output.getvalue())["instructions"], str(self.profile / "POLICY.md"))
        self.assertEqual(before, self.paths())
        terminal = MagicMock()
        terminal.__enter__.return_value = terminal
        terminal.readline.side_effect = ["4\n", "3\n"]  # rescan, then third agent
        with patch("builtins.open", return_value=terminal), patch.object(agent_setup, "command") as setup:
            agent_setup.agents_command([])
            setup.assert_called_once_with(["--agent", "demo"])
        self.configure()
        self.configure("--agent", "demo")
        first = (self.root / agent_setup.MANIFEST).read_bytes()
        self.configure("--agent", "demo")
        self.assertEqual(first, (self.root / agent_setup.MANIFEST).read_bytes())
        self.assertEqual(agent_setup.command(["--agent", "demo", "--status"])["status"], "configured")
        self.assertEqual(set(agent_setup._records(self.ctx)), {"codex", "demo"})
        self.assertEqual((self.profile / "session.cfg").read_text(), "seed")

    def test_third_agent_activation_and_rollback_include_its_files(self):
        self.catalog(self.site, "seed")
        self.configure("--agent", "demo")
        old = installation._state(self.ctx)
        new = self.next_state()
        site = Path(new["generations"][new["current"]]["site"])
        self.catalog(site, "next")
        with patch.object(installation, "_collect", return_value={}):
            installation._activate(self.ctx, new, "updated")
            self.assertEqual((self.profile / "session.cfg").read_text(), "next")
            self.assertEqual(agent_setup._load(self.ctx, "demo")["demo_data"], "next")
            self.assertIn("Synthetic release 0.2.0", (self.profile / "skills/project-memory/SKILL.md").read_text())
            old["generations"].update(new["generations"])
            old["previous"] = new["current"]
            installation._activate(self.ctx, old, "rolled_back")
        self.assertEqual((self.profile / "session.cfg").read_text(), "seed")
        self.assertIn("Synthetic release 0.1.0", (self.profile / "skills/project-memory/SKILL.md").read_text())

    def test_incompatible_catalogs_fail_before_writes_or_candidate_execution(self):
        self.configure("--agent", "demo")
        state = self.next_state()
        site = Path(state["generations"][state["current"]]["site"])
        self.catalog(site, "next")
        catalog = site / "richi_launcher/agent_adapters.py"
        manifest = (self.root / agent_setup.MANIFEST).read_bytes()
        for contents in ("AGENTS = {'codex': ('codex_setup', 1)}",
                         "AGENTS = {'demo': ('demo_setup', 2)}",
                         "AGENTS = {'demo': ('missing_setup', 1)}",
                         "raise RuntimeError('must not execute candidate')\nAGENTS = build_catalog()"):
            with self.subTest(contents=contents):
                catalog.write_text(contents + "\n")
                with self.assertRaises(ConfigError):
                    installation._activate(self.ctx, state, "updated")
                self.assertEqual(manifest, (self.root / agent_setup.MANIFEST).read_bytes())
                self.assertEqual((self.profile / "session.cfg").read_text(), "seed")
                self.assertFalse((self.root / "state.json").exists())

    def test_unsupported_stored_metadata_is_not_reinterpreted(self):
        self.configure("--agent", "demo")
        path = self.root / agent_setup.MANIFEST
        manifest = json.loads(path.read_text())
        record = manifest["agents"]["demo"]
        for invalid in (dict(record, adapter_version=2), []):
            with self.subTest(record=invalid):
                manifest["agents"]["demo"] = invalid
                path.write_text(json.dumps(manifest))
                before = path.read_bytes()
                with self.assertRaisesRegex(ConfigError, "Invalid agent setup metadata"):
                    self.configure("--agent", "demo")
                self.assertEqual(path.read_bytes(), before)
