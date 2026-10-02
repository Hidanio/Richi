"""Existing callers retain their database, and bridge installation is reversible."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / 'scripts' / 'install_legacy_bridge.py'


class LegacyBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='richi-bridge-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.legacy = self.root / 'old installation'
        self.legacy.mkdir()
        self.db = self.legacy / 'memory.sqlite3'
        config = self.root / 'config.json'
        config.write_text('{}', encoding='utf-8')
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('RICHI_')}
        self.env.update(RICHI_CONFIG=str(config), RICHI_DATA_DIR=str(self.root / 'unused data'))
        self.run_cli(sys.executable, '-m', 'richi', '--db', str(self.db), 'init')
        self.originals = {}
        for name in ('memory.py', 'serve.py', 'launch_map.py'):
            content = ('# original ' + name + '\n').encode()
            (self.legacy / name).write_bytes(content)
            self.originals[name] = content

    def run_cli(self, *args, ok=True):
        result = subprocess.run(list(args), cwd=self.root, env=self.env,
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return json.loads(result.stdout if ok else result.stderr)

    def install(self):
        return self.run_cli(sys.executable, str(BRIDGE), '--legacy-dir', str(self.legacy),
                            '--python', sys.executable)

    def test_legacy_database_is_preserved_from_unrelated_directory_and_restore(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        record = self.install()
        result = self.run_cli(sys.executable, str(self.legacy / 'memory.py'), 'check')
        self.assertEqual(result['status'], 'ok')
        self.assertFalse((self.root / 'unused data').exists())
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        result = self.run_cli(sys.executable, str(BRIDGE), '--restore', record['manifest'])
        self.assertEqual(result['status'], 'restored')
        for name, content in self.originals.items():
            self.assertEqual((self.legacy / name).read_bytes(), content)

    def test_legacy_imports_and_already_running_map_worker_remain_usable(self):
        self.install()
        env = dict(self.env, PYTHONPATH=str(self.legacy))
        check = subprocess.run(
            [sys.executable, "-c", "import memory; print(memory.__name__)"],
            cwd=self.root, env=env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual(check.stdout.strip(), "richi.memory")
        worker = subprocess.run(
            [sys.executable, "-c", "from serve import _git_worker; _git_worker()"],
            input=json.dumps({"database": str(self.db), "options": {"ref": "entry:missing"}}),
            cwd=self.legacy, env=env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(worker.returncode, 0, worker.stderr)
        result = json.loads(worker.stdout)
        self.assertEqual(result["status"], 404)
        self.assertEqual(result["payload"]["code"], "record_unavailable")

    def test_restore_rejects_later_edits_before_touching_other_files(self):
        record = self.install()
        first = (self.legacy / 'memory.py').read_bytes()
        changed = self.legacy / 'serve.py'
        changed.write_text('# subsequent user edit\n', encoding='utf-8')
        result = self.run_cli(sys.executable, str(BRIDGE), '--restore', record['manifest'], ok=False)
        self.assertIn('changed', result['error'])
        self.assertEqual((self.legacy / 'memory.py').read_bytes(), first)
        self.assertEqual(changed.read_text(), '# subsequent user edit\n')

    def test_reinstall_does_not_replace_original_backup_with_wrapper(self):
        self.install()
        result = self.run_cli(sys.executable, str(BRIDGE), '--legacy-dir', str(self.legacy),
                              '--python', sys.executable, ok=False)
        self.assertIn('already installed', result['error'])
        self.assertEqual(len(list(self.legacy.glob('richi-bridge-backup-*'))), 1)

    def test_explicit_database_still_overrides_legacy_default(self):
        self.install()
        alternate = self.root / 'alternate.sqlite3'
        self.run_cli(sys.executable, str(self.legacy / 'memory.py'), '--db', str(alternate), 'init')
        self.assertTrue(alternate.is_file())
        self.assertEqual(self.run_cli(sys.executable, str(self.legacy / 'memory.py'), 'check')['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
