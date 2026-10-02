"""Public workspace selection against disposable installed packages and stores."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ("import sys; sys.path.insert(0, sys.argv[1]); "
             "from richi_launcher.cli import main; sys.exit(main(sys.argv[2:]))")


class WorkspaceCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-workspace-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.installed = self.root / "fixed package"
        ignored = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(ROOT / "launcher" / "richi_launcher",
                        self.installed / "richi_launcher", ignore=ignored)
        shutil.copytree(ROOT / "src" / "richi", self.installed / "richi", ignore=ignored)
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("RICHI_")}
        self.environment.update(HOME=str(self.root / "home"),
                                XDG_CONFIG_HOME=str(self.root / "config"),
                                XDG_DATA_HOME=str(self.root / "data"))

    def command(self, *args, ok=True, input_json=None, environment=None, runner=BOOTSTRAP):
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", runner, str(self.installed), *map(str, args)],
            cwd=self.root, env=environment or self.environment,
            input=json.dumps(input_json) if input_json is not None else None,
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result

    def json_command(self, *args, **kwargs):
        return json.loads(self.command(*args, **kwargs).stdout)

    def create(self, name):
        result = self.json_command("workspace", "create", name)
        self.assertFalse(result["initialized"])
        self.assertFalse(Path(result["database"]).exists())
        self.assertEqual(result["next_command"], "richi --workspace " + name + " init")
        return result

    def test_create_use_and_current_do_not_open_database_or_change_implicit_default(self):
        original = self.json_command("config", "show")
        self.assertEqual(original["workspace"], "default")
        self.assertFalse(Path(original["config_file"]).exists())
        self.assertFalse(Path(original["database"]).exists())
        listing = self.json_command("workspace", "list")
        self.assertEqual(listing["current"], "default")
        alpha = self.create("alpha")
        self.assertEqual(self.json_command("workspace", "current")["workspace"], "default")
        self.assertEqual(self.json_command("use", "alpha")["current"], "alpha")
        current = self.json_command("workspace", "current")
        self.assertEqual(current["workspace"], "alpha")
        self.assertEqual(current["database"], alpha["database"])
        self.assertFalse(current["initialized"])
        self.assertEqual(self.json_command("workspace", "use", "default")["current"], "default")
        self.assertEqual(self.json_command("config", "show")["database"], original["database"])
        self.assertFalse(Path(original["database"]).exists())

    def test_same_repository_and_record_ids_stay_in_separate_workspaces(self):
        stores = {name: self.create(name) for name in ("alpha", "beta")}
        repository = self.root / "shared repository"
        repository.mkdir()
        for name in stores:
            self.json_command("-w=" + name, "init")
            self.json_command("--workspace=" + name, "project", "upsert", "--json", "-",
                              input_json={"id": "shared", "name": "Shared repository",
                                          "repo_path": str(repository)})
            self.json_command("-w", name, "entry", "put", "--json", "-", input_json={
                "id": "note:shared", "kind": "note", "title": "Shared ID",
                "summary": "Knowledge owned by " + name, "project_ids": ["shared"],
                "work_state": "done", "knowledge_state": "confirmed",
                "sources": [{"reference": "test://workspace/" + name}],
            })
        self.json_command("use", "beta")
        beta = self.json_command("entry", "get", "note:shared")
        alpha = self.json_command("--workspace", "alpha", "entry", "get", "note:shared")
        self.assertEqual(alpha["summary"], "Knowledge owned by alpha")
        self.assertEqual(beta["summary"], "Knowledge owned by beta")
        self.assertNotEqual(stores["alpha"]["database"], stores["beta"]["database"])
        self.assertNotEqual(stores["alpha"]["data_dir"], stores["beta"]["data_dir"])
        self.assertEqual(len(list(self.root.rglob("*.sqlite3"))), 2)
        # A subprocess pinned explicitly to alpha cannot be redirected by either
        # the new registry default or a conflicting parent environment selector.
        environment = dict(self.environment, RICHI_WORKSPACE="beta")
        chosen = self.json_command("-w", "alpha", "config", "show", environment=environment)
        self.assertEqual(chosen["workspace"], "alpha")
        self.assertEqual(chosen["database"], stores["alpha"]["database"])

    def test_concatenated_short_selector_pins_requested_workspace_for_cli_and_map(self):
        alpha = self.create("alpha")
        self.create("beta")
        self.json_command("-walpha", "init")
        self.json_command("use", "beta")
        self.assertEqual(self.json_command("-walpha", "workspace", "current")["workspace"], "alpha")
        self.assertEqual(self.json_command("-walpha", "config", "show")["workspace"], "alpha")
        self.assertEqual(self.json_command("-walpha", "project", "list")["projects"], [])
        runner = ("import sys,json,os; sys.path.insert(0,sys.argv[1]); "
                  "from richi_launcher.cli import main; "
                  "os.execve=lambda executable,argv,env:print(json.dumps({'argv':argv,'env':{k:v for k,v in env.items() if k.startswith('RICHI_')}})); "
                  "sys.exit(main(sys.argv[2:]))")
        result = self.json_command("-walpha", "map", "--no-open", runner=runner)
        arguments = result["argv"][result["argv"].index("cli") + 1:]
        self.assertEqual(arguments, ["--db", alpha["database"], "--config", alpha["config_file"],
                                     "map", "--no-open"])
        self.assertEqual(result["env"]["RICHI_ACTIVE_WORKSPACE"], "alpha")
        self.assertEqual(result["env"]["RICHI_DATA_DIR"], alpha["data_dir"])

    def test_workspace_operations_and_config_recovery_survive_broken_development(self):
        alpha = self.create("alpha")
        self.json_command("-w", "alpha", "config", "set", "development.source", self.root / "missing source")
        shown = self.json_command("-w", "alpha", "config", "set", "dev", "true")
        self.assertEqual(shown["workspace"], "alpha")
        self.assertFalse(shown["runtime"]["available"])
        self.json_command("use", "alpha")
        self.assertEqual(self.json_command("workspace", "current")["workspace"], "alpha")
        self.assertIn("alpha", [record["name"] for record in self.json_command("workspace", "list")["workspaces"]])
        self.assertFalse(self.json_command("config", "show")["runtime"]["available"])
        failed = self.command("--version", ok=False)
        self.assertIn("development.source", json.loads(failed.stderr)["error"])
        recovered = self.json_command("config", "set", "dev", "false")
        self.assertEqual(recovered["workspace"], "alpha")
        self.assertTrue(recovered["runtime"]["available"])
        self.command("--version")
        self.assertFalse(Path(alpha["database"]).exists())

    def test_named_storage_overrides_are_rejected_before_runtime_or_writes(self):
        alpha = self.create("alpha")
        other = self.root / "other.sqlite3"
        for args in (("-w", "alpha", "--db", other, "init"),
                     ("-w", "alpha", "--config", alpha["config_file"], "init"),
                     ("-w", "alpha", "map", "--db", other, "--no-open"),
                     ("-w", "alpha", "map", "--config", alpha["config_file"], "--no-open")):
            with self.subTest(args=args):
                failed = self.command(*args, ok=False)
                self.assertTrue(json.loads(failed.stderr)["error"])
        for key in ("RICHI_DB", "RICHI_DATA_DIR"):
            with self.subTest(environment=key):
                failed = self.command("-w", "alpha", "init", ok=False,
                                      environment=dict(self.environment, **{key: str(other)}))
                self.assertIn(key, json.loads(failed.stderr)["error"])
        self.assertFalse(other.exists())
        self.assertFalse(Path(alpha["database"]).exists())

    def test_noninteractive_use_never_reads_stdin_or_changes_selection(self):
        self.create("alpha")
        for args in (("use",), ("workspace", "use")):
            failed = self.command(*args, ok=False, input_json=1)
            self.assertIn("interactive terminal", json.loads(failed.stderr)["error"])
            self.assertEqual(failed.stdout, "")
        self.assertEqual(self.json_command("workspace", "current")["workspace"], "default")

    def test_interactive_menu_prompts_on_stderr_and_returns_json(self):
        self.create("alpha")
        runner = ("import sys,io; sys.path.insert(0,sys.argv[1]); "
                  "from richi_launcher.cli import main; "
                  "sys.stdin=io.StringIO('2\\n'); sys.stdin.isatty=lambda:True; "
                  "sys.exit(main(sys.argv[2:]))")
        result = self.command("use", runner=runner)
        self.assertIn("Select workspace", result.stderr)
        self.assertEqual(json.loads(result.stdout)["current"], "alpha")
        self.assertEqual(self.json_command("workspace", "current")["workspace"], "alpha")

    def test_config_set_creates_custom_config_but_does_not_recreate_missing_named_config(self):
        explicit = self.root / "new explicit config.json"
        shown = self.json_command("--config", explicit, "config", "set", "dev", "false")
        self.assertEqual(shown["config_file"], str(explicit))
        self.assertFalse(shown["dev"])
        self.assertEqual(json.loads(explicit.read_text()), {"dev": False})
        environment_config = self.root / "new environment config.json"
        self.json_command("config", "set", "dev", "false",
                          environment=dict(self.environment, RICHI_CONFIG=str(environment_config)))
        self.assertEqual(json.loads(environment_config.read_text()), {"dev": False})
        alpha = self.create("alpha")
        Path(alpha["config_file"]).unlink()
        failed = self.command("-w", "alpha", "config", "set", "dev", "false", ok=False)
        self.assertIn("does not exist", json.loads(failed.stderr)["error"])
        self.assertFalse(Path(alpha["config_file"]).exists())
        self.assertEqual(list(self.root.rglob("*.sqlite3")), [])

    def test_map_storage_options_are_normalized_before_selected_runtime(self):
        # Replace execve, then inspect exactly what the real bootstrap would pass
        # to mutable code. This does not launch a server or initialize a store.
        runner = ("import sys,json,os; sys.path.insert(0,sys.argv[1]); "
                  "from richi_launcher.cli import main; "
                  "os.execve=lambda executable,argv,env:print(json.dumps({'argv':argv,'env':{k:v for k,v in env.items() if k.startswith('RICHI_')}})); "
                  "sys.exit(main(sys.argv[2:]))")
        first, second = self.root / "first.json", self.root / "second.json"
        first.write_text(json.dumps({"database": "first.sqlite3"}), encoding="utf-8")
        second.write_text(json.dumps({"database": "second.sqlite3"}), encoding="utf-8")
        result = self.json_command("--config", first, "--db", self.root / "ignored.sqlite3",
                                   "map", "--config=" + str(second), "--db", self.root / "chosen.sqlite3",
                                   "--port", "8973", "--no-open", runner=runner)
        args = result["argv"]
        offset = args.index("cli") + 1
        passed = args[offset:]
        self.assertEqual(passed.count("--config"), 1)
        self.assertEqual(passed.count("--db"), 1)
        self.assertEqual(passed[:5], ["--db", str(self.root / "chosen.sqlite3"), "--config", str(second), "map"])
        self.assertEqual(result["env"]["RICHI_ACTIVE_CONFIG"], str(second))
        self.assertEqual(result["env"]["RICHI_DATA_DIR"], self.json_command("--config", second, "config", "show")["data_dir"])
        self.assertNotIn("RICHI_ACTIVE_WORKSPACE", result["env"])
        self.assertFalse((self.root / "chosen.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
