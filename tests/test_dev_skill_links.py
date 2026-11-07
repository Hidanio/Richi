"""Dev mode moves managed skill links to the checkout; every home is synthetic."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import richi_launcher
import test_agent_setup
from richi_launcher import agent_setup, cli, installation, setup_files

MULTI_AGENT = "PROTOCOL = 1\nMULTI_AGENT_PROTOCOL = 1\n"
DEV_CAPABLE = MULTI_AGENT + "DEV_SKILL_PROTOCOL = 1\n"


class DevSkillLinkTests(test_agent_setup.AgentSetupFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        (self.site / "richi_launcher/agent_setup.py").write_text(DEV_CAPABLE)
        self.checkout = self.home / "checkout"
        self.make_checkout(self.checkout, "alpha")
        self.codex = self.home / ".agents/skills/project-memory"
        self.claude = self.home / ".claude/skills/project-memory"
        self.release = str(self.site / "richi_launcher/agent_skill")

    @staticmethod
    def make_checkout(root, label):
        skill = root / "skills/project-memory"
        shutil.copytree(Path(richi_launcher.__file__).parent / "agent_skill", skill)
        with (skill / "SKILL.md").open("a") as stream:
            stream.write("\nSynthetic checkout " + label + "\n")
        return str(skill)

    def dev_link(self, checkout=None):
        return str((checkout or self.checkout) / "skills/project-memory")

    def links(self):
        return os.readlink(self.codex), os.readlink(self.claude)

    def status(self, agent):
        return agent_setup.command(["--agent", agent, "--status"])

    def configure_both(self):
        self.configure()
        self.configure("--agent", "claude")
        return {path: path.read_bytes() for path in (
            self.home / ".codex/AGENTS.md", self.home / ".claude/CLAUDE.md",
            self.home / ".claude/settings.json", self.home / ".claude/richi/session_start.py")}

    def next_state(self, agent_setup_source=DEV_CAPABLE):
        state = super().next_state()
        site = Path(state["generations"][state["current"]]["site"])
        (site / "richi_launcher/agent_setup.py").write_text(agent_setup_source)
        return state

    def activate(self, state, action="updated"):
        with patch.object(installation, "_collect", return_value={}):
            return installation._activate(self.ctx, state, action)

    def cli(self, config, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = cli.main(["--config", str(config), "config", *arguments])
        self.assertEqual(status, 0, err.getvalue())
        return json.loads(out.getvalue())

    def test_dev_switch_moves_every_agent_and_back_without_touching_instructions(self):
        protected = self.configure_both()
        report = agent_setup.switch_skills(True, self.checkout)
        self.assertEqual((report["status"], report["mode"], report["source"]), ("changed", "dev", str(self.checkout)))
        self.assertEqual(sorted(report["changes"]), sorted([str(self.codex), str(self.claude)]))
        self.assertEqual(self.links(), (self.dev_link(), self.dev_link()))
        self.assertIn("Synthetic checkout alpha", (self.claude / "SKILL.md").read_text())
        for agent in ("codex", "claude"):
            status = self.status(agent)
            self.assertEqual((status["status"], status["changes"], status["skill_mode"]), ("configured", [], "dev"))
            self.assertEqual(status["skill_target"], self.release)  # Older managers compare this.
        self.assertEqual(agent_setup.switch_skills(True, self.checkout)["status"], "unchanged")
        # Developer edits in the checkout are expected, not drift.
        with (self.checkout / "skills/project-memory/SKILL.md").open("a") as stream:
            stream.write("Work in progress\n")
        self.assertEqual(self.status("codex")["changes"], [])
        released = agent_setup.switch_skills(False, self.checkout)
        self.assertEqual((released["status"], released["mode"]), ("changed", "release"))
        self.assertEqual(self.links(), (self.release, self.release))
        self.assertNotIn("dev_source", agent_setup._load(self.ctx, "claude"))
        for agent in ("codex", "claude"):
            self.assertEqual(self.status(agent)["skill_mode"], "release")
            self.assertEqual(self.status(agent)["changes"], [])
        for path, data in protected.items():
            self.assertEqual(path.read_bytes(), data, str(path))
        self.assertFalse((self.root / setup_files.JOURNAL).exists())
        self.assertFalse(list(self.home.rglob("*.sqlite3")))

    def test_unmanaged_skills_are_never_switched(self):
        self.configure("--agent", "claude")
        personal = self.home / "personal-skill"
        personal.mkdir()
        self.codex.parent.mkdir(parents=True)
        self.codex.symlink_to(personal)
        agent_setup.switch_skills(True, self.checkout)
        agent_setup.switch_skills(False, self.checkout)
        self.assertEqual(os.readlink(self.codex), str(personal))
        self.assertEqual(os.readlink(self.claude), self.release)

    def test_missing_or_invalid_source_keeps_release_links_and_reports_why(self):
        self.configure_both()
        missing = agent_setup.switch_skills(True, self.home / "missing")
        self.assertEqual((missing["status"], missing["mode"]), ("unchanged", "release"))
        self.assertIn("no Project Memory skill", missing["reason"])
        unset = agent_setup.switch_skills(True, None)
        self.assertIn("development.source is not configured", unset["reason"])
        invalid = self.home / "invalid"
        (invalid / "skills/project-memory").mkdir(parents=True)
        (invalid / "skills/project-memory/SKILL.md").write_text("name: something-else\n")
        self.assertEqual(agent_setup.switch_skills(True, invalid)["mode"], "release")
        self.assertEqual(self.links(), (self.release, self.release))

    def test_edited_or_replaced_skills_block_switching_and_are_preserved(self):
        self.configure_both()
        bundled = self.site / "richi_launcher/agent_skill/SKILL.md"
        original = bundled.read_text()
        (self.codex / "SKILL.md").write_text(original + "Edited through the managed link\n")
        report = agent_setup.switch_skills(True, self.checkout)
        self.assertEqual(report["status"], "conflict")
        self.assertIn("edited", report["error"])
        self.assertEqual(self.links(), (self.release, self.release))
        self.assertTrue(bundled.read_text().endswith("Edited through the managed link\n"))
        bundled.write_text(original)
        self.claude.unlink()
        self.claude.symlink_to(self.home)
        report = agent_setup.switch_skills(True, self.checkout)
        self.assertIn("claude: Managed skill link was changed", report["error"])
        # All or nothing: Codex did not move either.
        self.assertEqual(self.links(), (self.release, str(self.home)))
        self.assertFalse((self.root / setup_files.JOURNAL).exists())

    def test_config_set_switches_links_and_dev_false_recovers_without_a_checkout(self):
        self.configure_both()
        config = self.home / "settings.json"
        source = self.cli(config, "set", "development.source", str(self.checkout))
        self.assertEqual((source["agent_skills"]["mode"], source["agent_skills"]["matches_config"]), ("release", True))
        self.assertEqual(self.links(), (self.release, self.release))
        enabled = self.cli(config, "set", "dev", "true")
        self.assertEqual((enabled["agent_skills"]["status"], enabled["agent_skills"]["mode"]), ("changed", "dev"))
        self.assertEqual(self.links(), (self.dev_link(), self.dev_link()))
        shown = self.cli(config, "show")["agent_skills"]
        self.assertEqual((shown["mode"], shown["matches_config"]), ("dev", True))
        # A new source while dev is on re-points the links.
        other = self.home / "other-checkout"
        self.make_checkout(other, "bravo")
        moved = self.cli(config, "set", "development.source", str(other))
        self.assertEqual(moved["agent_skills"]["source"], str(other))
        self.assertEqual(self.links(), (self.dev_link(other), self.dev_link(other)))
        shutil.rmtree(other)
        shown = self.cli(config, "show")["agent_skills"]
        self.assertFalse(shown["matches_config"])
        self.assertIn("no Project Memory skill", shown["reason"])
        disabled = self.cli(config, "set", "dev", "false")
        self.assertFalse(disabled["dev"])
        self.assertEqual((disabled["agent_skills"]["status"], disabled["agent_skills"]["mode"]), ("changed", "release"))
        self.assertEqual(self.links(), (self.release, self.release))
        for agent in ("codex", "claude"):
            self.assertEqual(self.status(agent)["status"], "configured")

    def test_config_is_saved_even_when_links_conflict(self):
        self.configure()
        self.codex.unlink()
        self.codex.symlink_to(self.home)
        config = self.home / "settings.json"
        self.cli(config, "set", "development.source", str(self.checkout))
        enabled = self.cli(config, "set", "dev", "true")
        self.assertEqual(enabled["agent_skills"]["status"], "conflict")
        self.assertEqual(enabled["agent_skills"]["next_command"], "richi config set dev true")
        self.assertIs(json.loads(config.read_text())["dev"], True)
        self.assertEqual(os.readlink(self.codex), str(self.home))

    def test_update_and_rollback_keep_dev_links_with_dev_capable_releases(self):
        self.configure_both()
        agent_setup.switch_skills(True, self.checkout)
        old = installation._state(self.ctx)
        new = self.next_state()
        updated = self.activate(new)
        self.assertEqual(updated["agent_skills"]["claude"], {"mode": "dev", "target": self.dev_link()})
        self.assertEqual(self.links(), (self.dev_link(), self.dev_link()))
        new_release = new["generations"][new["current"]]["site"] + "/richi_launcher/agent_skill"
        for agent in ("codex", "claude"):
            record = agent_setup._load(self.ctx, agent)
            self.assertEqual((record["generation"], record["skill_target"]), (new["current"], new_release))
            self.assertEqual(self.status(agent)["changes"], [])
        old["generations"].update(new["generations"])
        old["previous"] = new["current"]
        self.activate(old, "rolled_back")
        self.assertEqual(self.links(), (self.dev_link(), self.dev_link()))
        self.assertEqual(agent_setup._load(self.ctx, "codex")["skill_target"], self.release)
        agent_setup.switch_skills(False, None)
        self.assertEqual(self.links(), (self.release, self.release))

    def test_activation_without_dev_support_or_checkout_restores_release_links(self):
        self.configure_both()
        agent_setup.switch_skills(True, self.checkout)
        legacy = self.next_state(MULTI_AGENT)
        legacy_release = legacy["generations"][legacy["current"]]["site"] + "/richi_launcher/agent_skill"
        result = self.activate(legacy)
        self.assertEqual(result["agent_skills"]["codex"]["mode"], "release")
        self.assertEqual(self.links(), (legacy_release, legacy_release))
        self.assertEqual(self.status("claude")["status"], "configured")
        # Back on a dev-capable release with a removed checkout: release links.
        (Path(legacy["generations"][legacy["current"]]["site"]) / "richi_launcher/agent_setup.py").write_text(DEV_CAPABLE)
        agent_setup.switch_skills(True, self.checkout)
        shutil.rmtree(self.checkout)
        state = installation._state(self.ctx)
        state["previous"], state["current"] = state["current"], state["previous"]
        self.activate(state, "rolled_back")
        self.assertEqual(self.links(), (self.release, self.release))
        self.assertNotIn("dev_source", agent_setup._load(self.ctx, "codex"))

    def test_dev_switch_warns_when_previous_release_cannot_keep_checkout_links(self):
        (self.site / "richi_launcher/agent_setup.py").write_text(MULTI_AGENT)
        self.configure_both()
        self.activate(self.next_state())
        report = agent_setup.switch_skills(True, self.checkout)
        self.assertEqual(report["mode"], "dev")
        self.assertIn("previous release 0.1.0", report["rollback"])
        self.assertIn("richi config set dev false", report["rollback"])
        self.assertNotIn("rollback", agent_setup.switch_skills(False, None))

    def test_status_reports_broken_checkout_and_setup_repairs_to_release(self):
        self.configure()
        agent_setup.switch_skills(True, self.checkout)
        self.assertEqual(self.status("codex")["status"], "configured")
        shutil.rmtree(self.checkout / "skills")
        status = self.status("codex")
        self.assertEqual(status["status"], "repair_required")
        self.assertIn(str(self.codex), status["changes"])
        self.assertIn("no Project Memory skill", status["dev_skill_error"])
        self.configure()
        self.assertEqual(os.readlink(self.codex), self.release)
        self.assertEqual(self.status("codex")["status"], "configured")

    def test_new_agent_follows_dev_links_and_adopts_an_identical_manual_link(self):
        self.configure("--agent", "claude")
        agent_setup.switch_skills(True, self.checkout)
        self.codex.parent.mkdir(parents=True)
        self.codex.symlink_to(self.dev_link())
        result = self.configure()
        self.assertEqual((result["skill_mode"], result["skill_backups"]), ("dev", []))
        self.assertNotIn(str(self.codex), result["changes"])
        self.assertFalse((self.home / ".agents/richi-skill-backups").exists())
        self.assertEqual(self.status("codex")["status"], "configured")
        agent_setup.switch_skills(False, None)
        self.assertEqual(self.links(), (self.release, self.release))

    def test_interrupted_switch_recovers_and_later_switch_completes(self):
        self.configure_both()
        manifest = (self.root / agent_setup.MANIFEST).read_bytes()
        ops, _ = agent_setup._switch_operations(self.ctx, installation._state(self.ctx), self.checkout)
        setup_files.begin(self.ctx, ops)
        self.assertEqual(self.links(), (self.dev_link(), self.dev_link()))
        # The next CLI call recovers an uncommitted switch before doing anything else.
        report = agent_setup.switch_skills(False, None)
        self.assertEqual(report["status"], "unchanged")
        self.assertEqual(self.links(), (self.release, self.release))
        self.assertEqual((self.root / agent_setup.MANIFEST).read_bytes(), manifest)
        self.assertEqual(agent_setup.switch_skills(True, self.checkout)["status"], "changed")
        self.assertFalse((self.root / setup_files.JOURNAL).exists())

    def test_busy_installation_and_missing_setup_are_reported(self):
        self.assertEqual(agent_setup.switch_skills(True, self.checkout), {"status": "not_configured"})
        self.configure()
        with patch.object(installation, "_locked", side_effect=installation._InstallationBusy()):
            busy = agent_setup.switch_skills(True, self.checkout)
        self.assertEqual(busy["status"], "busy")
        self.assertEqual(os.readlink(self.codex), self.release)


if __name__ == "__main__":
    unittest.main()
