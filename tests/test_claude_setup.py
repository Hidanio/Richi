"""Claude and Codex coexist in synthetic homes; hooks never access memory."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_agent_setup
from richi_launcher import agent_setup, installation
from richi_launcher.claude_setup import HOOK
from richi_launcher.config import ConfigError


class ClaudeSetupTests(test_agent_setup.AgentSetupFixture, unittest.TestCase):
    def test_claude_preserves_settings_and_codex_and_is_idempotent(self):
        self.configure()
        codex = (self.home / '.codex/AGENTS.md').read_bytes()
        profile = self.home / '.claude'
        profile.mkdir()
        settings = {'permissions': {'deny': ['Bash(rm *)']}, 'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'echo hello'}]}]}}
        (profile / 'settings.json').write_text(json.dumps(settings))
        self.configure('--agent', 'claude')
        first = (profile / 'settings.json').read_bytes()
        self.configure('--agent', 'claude')
        self.assertEqual(first, (profile / 'settings.json').read_bytes())
        data = json.loads(first)
        self.assertEqual(data['permissions'], settings['permissions'])
        self.assertEqual(data['hooks']['SessionStart'][0], settings['hooks']['SessionStart'][0])
        self.assertEqual(len(data['hooks']['SessionStart']), 2)
        self.assertEqual(codex, (self.home / '.codex/AGENTS.md').read_bytes())
        self.assertEqual(agent_setup.command(['--status', '--agent', 'claude'])['status'], 'configured')
        self.assertEqual(agent_setup.command(['--status'])['status'], 'configured')
        self.assertFalse(list(self.home.rglob('*.sqlite3')))

    def test_claude_dry_run_and_conflicts_preserve_home(self):
        before = self.paths()
        agent_setup.command(['--agent', 'claude', '--dry-run'])
        self.assertEqual(before, self.paths())
        self.configure('--agent', 'claude')
        hook = self.home / '.claude/richi/session_start.py'
        hook.write_text('custom')
        with self.assertRaisesRegex(ConfigError, 'hook was edited'):
            self.configure('--agent', 'claude')
        self.assertEqual(hook.read_text(), 'custom')

    def test_hook_session_identity_resume_and_new_chat(self):
        env_file = self.home / 'session.env'
        env_file.write_text('export EXISTING=1\n')
        for session in ('abc-123', 'abc-123', 'def-456'):
            result = subprocess.run([sys.executable, '-I', '-c', HOOK], input=json.dumps({'session_id': session, 'hook_event_name': 'SessionStart'}), text=True, capture_output=True, env=dict(os.environ, CLAUDE_ENV_FILE=str(env_file)))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('claude:' + session, result.stdout)
        self.assertEqual(env_file.read_text().count('claude:abc-123'), 2)
        self.assertTrue(env_file.read_text().startswith('export EXISTING=1\n'))
        result = subprocess.run([sys.executable, '-I', '-c', HOOK], input=json.dumps({'session_id': '$(bad)', 'hook_event_name': 'SessionStart'}), text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_old_release_cannot_drop_claude_integration(self):
        self.configure('--agent', 'claude')
        with self.assertRaisesRegex(ConfigError, 'cannot maintain'):
            installation._activate(self.ctx, self.next_state(), 'updated')

    def test_skills_and_hook_follow_activation_and_rollback(self):
        self.configure()
        self.configure('--agent', 'claude')
        old = installation._state(self.ctx)
        (self.site / 'richi_launcher/agent_setup.py').write_text('PROTOCOL = 1\nMULTI_AGENT_PROTOCOL = 1\n')
        state = self.next_state()
        site = Path(state['generations'][state['current']]['site'])
        (site / 'richi_launcher/agent_setup.py').write_text('PROTOCOL = 1\nMULTI_AGENT_PROTOCOL = 1\n')
        new_hook = HOOK + '\n# Synthetic next hook\n'
        (site / 'richi_launcher/claude_setup.py').write_text('HOOK = ' + repr(new_hook) + '\n')
        hook = self.home / '.claude/richi/session_start.py'
        settings = self.home / '.claude/settings.json'
        original_settings = settings.read_bytes()
        with patch.object(installation, '_collect', return_value={}):
            installation._activate(self.ctx, state, 'updated')
            self.assertEqual(hook.read_text(), new_hook)
            for skill in ('.agents/skills/project-memory', '.claude/skills/project-memory'):
                self.assertIn('Synthetic release 0.2.0', (self.home / skill / 'SKILL.md').read_text())
            old['generations'].update(state['generations'])
            old['previous'] = state['current']
            installation._activate(self.ctx, old, 'rolled_back')
        self.assertEqual(hook.read_text(), HOOK)
        self.assertEqual(settings.read_bytes(), original_settings)
        self.assertEqual(agent_setup.command(['--agent', 'claude', '--status'])['status'], 'configured')

    def test_edited_hook_or_settings_prevent_activation(self):
        self.configure('--agent', 'claude')
        state = self.next_state()
        site = Path(state['generations'][state['current']]['site'])
        (site / 'richi_launcher/agent_setup.py').write_text('PROTOCOL = 1\nMULTI_AGENT_PROTOCOL = 1\n')
        for name, content in (('richi/session_start.py', 'custom hook'), ('settings.json', '{}')):
            with self.subTest(name=name):
                path = self.home / '.claude' / name
                original = path.read_bytes()
                path.write_text(content)
                with self.assertRaises(ConfigError):
                    installation._activate(self.ctx, state, 'updated')
                self.assertEqual(path.read_text(), content)
                self.assertFalse((self.root / 'state.json').exists())
                path.write_bytes(original)

    def test_malformed_settings_are_preserved_without_partial_setup(self):
        profile = self.home / '.claude'
        profile.mkdir()
        settings = profile / 'settings.json'
        settings.write_bytes(b'{broken json')
        before = self.paths()
        with self.assertRaisesRegex(ConfigError, 'Claude settings'):
            self.configure('--agent', 'claude')
        self.assertEqual(before, self.paths())
        self.assertEqual(settings.read_bytes(), b'{broken json')

    def test_discovery_does_not_write_or_create_profiles(self):
        before = self.paths()
        result = agent_setup.agents_command(['--status'])
        self.assertEqual({item['agent'] for item in result['agents']}, {'codex', 'claude'})
        self.assertEqual(before, self.paths())

    def test_claude_skill_adoption_preserves_manual_copy(self):
        skill = self.home / '.claude/skills/project-memory'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('manual copy')
        with self.assertRaisesRegex(ConfigError, '--replace-skill'):
            self.configure('--agent', 'claude')
        self.configure('--agent', 'claude', '--replace-skill')
        backup, = (self.home / '.claude/richi-skill-backups').glob('project-memory-*')
        self.assertEqual((backup / 'SKILL.md').read_text(), 'manual copy')
        self.assertTrue(skill.is_symlink())

    def test_claude_does_not_validate_codex_profile_or_budget(self):
        profile = self.home / '.claude'
        profile.mkdir()
        original = b'x' * 33000
        (profile / 'CLAUDE.md').write_bytes(original)
        target = self.home / 'codex-target'
        target.mkdir()
        (self.home / 'codex-link').symlink_to(target)
        with patch.dict(os.environ, CODEX_HOME=str(self.home / 'codex-link')):
            self.configure('--agent', 'claude')
        self.assertTrue((profile / 'CLAUDE.md').read_bytes().startswith(original))

    def test_duplicate_settings_keys_are_rejected_before_writing(self):
        profile = self.home / '.claude'
        profile.mkdir()
        settings = profile / 'settings.json'
        original = b'{"permissions":{"deny":["Bash(rm *)"]},"permissions":{}}'
        settings.write_bytes(original)
        before = self.paths()
        with self.assertRaisesRegex(ConfigError, 'duplicate'):
            self.configure('--agent', 'claude')
        self.assertEqual(before, self.paths())
        self.assertEqual(settings.read_bytes(), original)


class SyntheticEnvironmentTests(unittest.TestCase):
    def test_cli_environment_never_targets_the_callers_agent_profiles(self):
        # Shells started by Claude Code export CLAUDE_CONFIG_DIR.
        from cli_environment import cli_environment
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/real/claude", "CODEX_HOME": "/real/codex"}):
            environment = cli_environment("/synthetic")
        self.assertNotIn("CLAUDE_CONFIG_DIR", environment)
        self.assertNotIn("CODEX_HOME", environment)
        self.assertTrue(environment["HOME"].endswith("cli-home"))
