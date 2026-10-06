"""Chat binding isolation and transactions against synthetic registries/stores."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from richi_launcher import chats, config, workspaces


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ("import sys; sys.path.insert(0,sys.argv[1]); "
             "from richi_launcher.cli import main; sys.exit(main(sys.argv[2:]))")
CAPTURE = ("import sys,json,os; sys.path.insert(0,sys.argv[1]); "
           "from richi_launcher.cli import main; "
           "os.execve=lambda executable,argv,env:print(json.dumps({'argv':argv,'env':{k:v for k,v in env.items() if k.startswith('RICHI_')}})); "
           "sys.exit(main(sys.argv[2:]))")


class ChatBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-chat-binding-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        for patch in (mock.patch.dict(os.environ, {}, clear=True),
                      mock.patch.object(Path, "home", return_value=self.home),
                      mock.patch.object(config.sys, "platform", "linux")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_absent_chat_and_binding_only_read_metadata(self):
        self.assertIsNone(chats.detect_chat())
        self.assertFalse(chats.current_chat(None)["detected"])
        state = chats.current_chat("new-chat")
        self.assertFalse(state["bound"])
        self.assertEqual(state["cli_current"], "default")
        self.assertEqual(state["cli_default"], "default")
        self.assertFalse(self.home.exists())
        with self.assertRaises(chats.ChatError) as raised:
            chats.pin_chat("new-chat", {"workspace": "default"})
        self.assertEqual(raised.exception.details["code"], "chat_workspace_required")
        self.assertFalse(self.home.exists())

    def test_chat_id_precedence_validation_and_no_session_fallback(self):
        os.environ["CODEX_SESSION_ID"] = "generic-session"
        self.assertIsNone(chats.detect_chat())
        os.environ["CODEX_THREAD_ID"] = "codex-thread"
        self.assertEqual(chats.detect_chat(), "codex-thread")
        os.environ["RICHI_CHAT_ID"] = "client-thread"
        self.assertEqual(chats.detect_chat(), "client-thread")
        self.assertEqual(chats.detect_chat("explicit"), "explicit")
        for value in ("", "../escape", "with space", "a\n", "☃", "a" * 201):
            with self.subTest(value=value), self.assertRaises(chats.ChatError):
                chats.detect_chat(value)
        os.environ["RICHI_CHAT_ID"] = ""
        with self.assertRaises(chats.ChatError):
            chats.detect_chat()

    def test_current_choice_is_saved_once_and_global_selection_cannot_redirect(self):
        workspaces.create_workspace("alpha")
        workspaces.create_workspace("beta")
        workspaces.use_workspace("alpha")
        bound = chats.bind_chat("first", current=True)
        self.assertEqual(bound["workspace"], "alpha")
        workspaces.use_workspace("beta")
        self.assertEqual(chats.pin_chat("first", {})["workspace"], "alpha")
        self.assertEqual(chats.current_chat("first")["cli_current"], "beta")
        self.assertEqual(chats.bind_chat("second", current=True)["workspace"], "beta")
        with mock.patch.dict(os.environ, {"RICHI_WORKSPACE": "alpha"}):
            self.assertEqual(chats.bind_chat("third", current=True)["workspace"], "alpha")
        self.assertEqual(workspaces.list_workspaces()["current"], "beta")
        self.assertEqual(list(self.root.rglob("*.sqlite3")), [])

    def test_idempotent_bind_requires_deliberate_replacement_and_private_modes(self):
        workspaces.create_workspace("alpha")
        first = chats.bind_chat("chat", "default")
        self.assertTrue(first["changed"])
        registry = chats._registry_path()
        before = registry.stat().st_mtime_ns
        self.assertFalse(chats.bind_chat("chat", "default")["changed"])
        self.assertEqual(registry.stat().st_mtime_ns, before)
        with self.assertRaises(chats.ChatError) as raised:
            chats.bind_chat("chat", "alpha")
        self.assertEqual(raised.exception.details["code"], "chat_rebind_required")
        self.assertTrue(chats.bind_chat("chat", "alpha", replace=True)["replaced"])
        self.assertEqual(chats.current_chat("chat")["workspace"], "alpha")
        for path, mode in ((registry, 0o600), (registry.parent, 0o700),
                           (registry.with_name(registry.name + ".lock"), 0o600)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)

    def test_concurrent_bindings_preserve_all_records_and_conflicting_choice_has_one_winner(self):
        workspaces.create_workspace("alpha")
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda number: chats.bind_chat("chat-%d" % number, "default"), range(24)))
        self.assertEqual(len(chats._read_registry()["chats"]), 24)
        def attempt(name):
            try:
                chats.bind_chat("conflict", name)
                return True
            except chats.ChatError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(attempt, ["default", "alpha"])), 1)
        self.assertEqual(len(chats._read_registry()["chats"]), 25)

    def test_failed_atomic_update_preserves_registry(self):
        chats.bind_chat("first", "default")
        before = chats._registry_path().read_bytes()
        with mock.patch.object(workspaces.os, "replace", side_effect=OSError("full disk")):
            with self.assertRaisesRegex(chats.ChatError, "full disk"):
                chats.bind_chat("second", "default")
        self.assertEqual(chats._registry_path().read_bytes(), before)
        self.assertEqual(list(chats._registry_path().parent.glob(".chat-workspaces.json.*")), [])

    def test_corrupt_bounded_registry_unknown_workspace_and_missing_config_never_fall_back(self):
        alpha = workspaces.create_workspace("alpha")
        chats.bind_chat("chat", "alpha")
        registry = chats._registry_path()
        good = registry.read_bytes()
        for raw in (b"{}", b"[]", b'{"version":1,"version":1,"chats":{}}',
                    b'{"version":true,"chats":{}}',
                    b'{"version":1,"chats":{"bad/id":{"workspace":"default"}}}',
                    b'{"version":1,"chats":{"chat":{"workspace":"default","extra":1}}}',
                    b" " * (chats._MAX_BYTES + 1)):
            registry.write_bytes(raw)
            with self.subTest(raw=raw[:80]), self.assertRaises(chats.ChatError):
                chats.pin_chat("chat", {})
        registry.write_bytes(good)
        with self.assertRaisesRegex(config.ConfigError, "Unknown workspace"):
            chats.bind_chat("unknown", "missing")
        self.assertNotIn("unknown", chats._read_registry()["chats"])
        Path(alpha["config_file"]).unlink()
        with self.assertRaisesRegex(config.ConfigError, "does not exist"):
            config.resolve_settings(**chats.pin_chat("chat", {}))
        workspace_registry = workspaces._read_registry()
        del workspace_registry["workspaces"]["alpha"]
        workspaces._registry_path().write_text(json.dumps(workspace_registry))
        with self.assertRaises(chats.ChatError) as raised:
            chats.current_chat("chat")
        self.assertEqual(raised.exception.details["code"], "chat_workspace_missing")

    def test_chat_storage_overrides_conflicts_and_invalid_bind_do_not_create_database(self):
        chats.bind_chat("chat", "default")
        for selectors in ({"config_file": "/tmp/other.json"}, {"db": "/tmp/other.sqlite3"},
                          {"workspace": "other"}):
            with self.subTest(selectors=selectors), self.assertRaises(chats.ChatError):
                chats.pin_chat("chat", selectors)
        for key in ("RICHI_CONFIG", "RICHI_DB", "RICHI_DATA_DIR", "RICHI_WORKSPACE"):
            with mock.patch.dict(os.environ, {key: "other"}):
                with self.subTest(key=key), self.assertRaises(chats.ChatError):
                    chats.pin_chat("chat", {})
                if key != "RICHI_WORKSPACE":
                    with self.assertRaises(chats.ChatError):
                        chats.bind_chat("new", current=True)
        self.assertEqual(list(self.root.rglob("*.sqlite3")), [])

    def test_pin_survives_rebinding_later_in_same_invocation(self):
        workspaces.create_workspace("alpha")
        chats.bind_chat("chat", "default")
        selectors = chats.pin_chat("chat", {})
        chats.bind_chat("chat", "alpha", replace=True)
        self.assertEqual(config.resolve_settings(**selectors).workspace, "default")
        self.assertEqual(chats.pin_chat("chat", {})["workspace"], "alpha")


class ChatCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-chat-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.installed = self.root / "fixed-package"
        ignored = shutil.ignore_patterns("__pycache__", "*.pyc")
        for source, target in ((ROOT / "launcher/richi_launcher", "richi_launcher"), (ROOT / "src/richi", "richi")):
            shutil.copytree(source, self.installed / target, ignore=ignored)
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("RICHI_") and key not in {"CODEX_THREAD_ID", "CODEX_SESSION_ID"}}
        self.environment.update(HOME=str(self.root / "home"), XDG_CONFIG_HOME=str(self.root / "config"),
                                XDG_DATA_HOME=str(self.root / "data"))

    def command(self, *args, chat=None, env=None, ok=True, payload=None, runner=BOOTSTRAP):
        environment = dict(self.environment, **(env or {}))
        if chat is not None:
            environment["CODEX_THREAD_ID"] = chat
        result = subprocess.run([sys.executable, "-I", "-B", "-c", runner, str(self.installed), *map(str, args)],
                                cwd=self.root, env=environment, capture_output=True, text=True, timeout=20,
                                input=json.dumps(payload) if payload is not None else None)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return json.loads(result.stdout if ok else result.stderr)

    def test_unbound_cannot_read_write_or_select_around_choice_but_metadata_and_help_work(self):
        self.command("workspace", "create", "alpha", chat="new")
        for args in (("init",), ("-w", "alpha", "init"), ("project", "list"),
                     ("recall", "--", "--help"), ("config", "set", "dev", "false")):
            with self.subTest(args=args):
                failure = self.command(*args, chat="new", ok=False)
                self.assertEqual(failure["code"], "chat_workspace_required")
        for args in (("use", "alpha"), ("workspace", "use", "alpha")):
            failure = self.command(*args, chat="new", ok=False)
            self.assertEqual(failure["code"], "chat_global_use_forbidden")
        self.assertEqual(self.command("chat", "current", chat="new")["cli_default"], "default")
        self.assertEqual(self.command("config", "show", chat="new")["workspace"], "default")
        self.assertEqual(self.command("workspace", "current", chat="new")["workspace"], "default")
        state = self.command("chat", "current", chat="new")
        self.assertFalse(state["bound"])
        for args in (("--help",), ("recall", "--help"), ("map", "--help")):
            captured = self.command(*args, chat="new", runner=CAPTURE)
            self.assertIn("--help", captured["argv"])
        self.assertEqual(list(self.root.rglob("*.sqlite3")), [])

    def test_two_chats_in_same_directory_remain_isolated_after_cli_default_changes(self):
        for name in ("alpha", "beta"):
            self.command("workspace", "create", name)
            self.command("chat", "bind", name, chat=name)
            self.command("init", chat=name)
            self.command("entry", "put", "--json", "-", chat=name, payload={
                "id": "note:shared", "kind": "note", "title": "Same record ID", "summary": name,
                "knowledge_state": "confirmed", "work_state": "done",
                "sources": [{"reference": "test://" + name}]})
        self.command("use", "beta")
        for name in ("alpha", "beta"):
            self.assertEqual(self.command("entry", "get", "note:shared", chat=name)["summary"], name)
            self.assertEqual(self.command("config", "show", chat=name)["workspace"], name)
        denied = self.command("-w", "beta", "entry", "get", "note:shared", chat="alpha", ok=False)
        self.assertEqual(denied["code"], "chat_workspace_conflict")
        self.assertEqual(self.command("use", "beta", chat="alpha", ok=False)["code"], "chat_global_use_forbidden")
        self.assertEqual(self.command("workspace", "use", "beta", chat="alpha", ok=False)["code"], "chat_global_use_forbidden")

    def test_map_global_local_and_environment_overrides_cannot_escape_default_chat(self):
        shown = self.command("chat", "bind", "default", chat="chat")
        for args in (("--db", self.root / "other.db", "init"),
                     ("--config", shown["config_file"], "init"),
                     ("map", "--db", self.root / "other.db"),
                     ("map", "--config=" + shown["config_file"]),
                     ("--db", self.root / "other.db", "map", "--db=" + shown["database"])):
            with self.subTest(args=args):
                self.assertEqual(self.command(*args, chat="chat", ok=False)["code"], "chat_storage_override")
        for key in ("RICHI_DB", "RICHI_DATA_DIR", "RICHI_CONFIG"):
            with self.subTest(key=key):
                self.assertEqual(self.command("map", chat="chat", env={key: "other"}, ok=False)["code"], "chat_storage_override")
        captured = self.command("map", "--port", "8768", "--no-open", chat="chat", runner=CAPTURE)
        args = captured["argv"]
        self.assertEqual(args[args.index("--db") + 1], shown["database"])
        self.assertEqual(captured["env"]["RICHI_CHAT_ID"], "chat")
        self.assertEqual(captured["env"]["RICHI_ACTIVE_WORKSPACE"], "default")
        self.assertEqual(list(self.root.rglob("*.sqlite3")), [])

    def test_broken_development_still_allows_bind_current_inspect_and_recover(self):
        self.command("workspace", "create", "alpha")
        self.command("-w", "alpha", "config", "set", "development.source", self.root / "missing")
        self.command("-w", "alpha", "config", "set", "dev", "true")
        self.command("use", "alpha")
        self.assertEqual(self.command("chat", "bind", "--current", chat="chat")["workspace"], "alpha")
        self.command("use", "default")
        self.assertEqual(self.command("chat", "current", chat="chat")["workspace"], "alpha")
        self.assertFalse(self.command("config", "show", chat="chat")["runtime"]["available"])
        self.assertTrue(self.command("config", "set", "dev", "false", chat="chat")["runtime"]["available"])
        self.assertEqual(list(self.root.rglob("*.sqlite3")), [])

    def test_explicit_chat_and_project_path_are_forwarded_without_becoming_storage_selectors(self):
        self.command("workspace", "create", "alpha")
        self.command("--chat", "generic", "chat", "bind", "alpha", chat="automatic")
        self.assertFalse(self.command("chat", "current", chat="automatic")["bound"])
        path = self.root / "shared repo"
        captured = self.command("--project-path=" + str(path), "--chat=generic", "-walpha", "project", "list",
                                chat="automatic", runner=CAPTURE)
        args = captured["argv"]
        self.assertEqual(args[args.index("--project-path") + 1], str(path))
        self.assertEqual(captured["env"]["RICHI_CHAT_ID"], "generic")
        self.assertNotIn("--chat", args)
        self.assertIn("--project-path applies", self.command("--chat=generic", "--project-path", path,
                                                             "map", ok=False)["error"])


class ChatMapTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "Map launch targets macOS and Linux")
    def test_public_map_workers_and_dev_reload_keep_original_chat_storage(self):
        # Reuse the existing real-map fixture without importing its TestCase into
        # module globals (which would make unittest discover its tests twice).
        from test_workspace_map import WorkspaceMapTests
        from urllib.parse import urlsplit
        fixture = WorkspaceMapTests("test_maps_workers_and_dev_reload_remain_in_original_workspace")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        alpha = fixture.create_workspace("alpha")
        fixture.create_workspace("beta")
        repository = fixture.root / "shared-repository"
        repository.mkdir()
        fixture.git(repository, "init", "-q")
        fixture.git(repository, "config", "user.name", "Fixture")
        fixture.git(repository, "config", "user.email", "fixture@example.invalid")
        (repository / "code.txt").write_text("Baseline\n")
        fixture.git(repository, "add", "code.txt")
        fixture.git(repository, "commit", "-qm", "Baseline")
        record = fixture.seed(alpha, "Alpha", repository)
        fixture.environment["CODEX_THREAD_ID"] = "map-chat"
        fixture.cli("chat", "bind", "alpha")
        port = fixture.free_port()
        started = fixture.cli("map", "--no-open", "--port", str(port))
        port = urlsplit(started["url"]).port
        fixture.detached.append((started["pid"], port))
        initial = fixture.health(port)
        self.assertEqual(initial["workspace"], "alpha")
        self.assertEqual(initial["database"], alpha["database"])
        fixture.assert_knowledge(port, "Alpha", record)
        fixture.cli("config", "set", "development.source", str(fixture.checkout))
        fixture.cli("config", "set", "dev", "true")
        development = fixture.health(port, "dev")
        self.assertEqual(development["workspace"], "alpha")
        self.assertEqual(development["database"], alpha["database"])
        fixture.assert_knowledge(port, "Alpha", record)
        # Rebinding changes future chat commands. Both changing the terminal's
        # default and editing dev source must leave the existing map pinned.
        fixture.cli("chat", "bind", "beta", "--replace")
        fixture.environment.pop("CODEX_THREAD_ID")
        fixture.cli("use", "beta")
        fixture.environment["CODEX_THREAD_ID"] = "map-chat"
        source = fixture.package / "serve.py"
        source.write_text(source.read_text().replace('server_version = "ProjectMemoryMap/4"',
                                                     'server_version = "ProjectMemoryMap/5"'))
        self.assertEqual(fixture.health(port, "dev", "ProjectMemoryMap/5"), development)
        fixture.assert_knowledge(port, "Alpha", record)
        # A separate terminal can also restore alpha's installed runtime; the
        # map reload still follows alpha even though the chat now uses beta.
        fixture.environment.pop("CODEX_THREAD_ID")
        fixture.cli("-w", "alpha", "config", "set", "dev", "false")
        fixture.environment["CODEX_THREAD_ID"] = "map-chat"
        self.assertEqual(fixture.health(port), initial)
        fixture.assert_knowledge(port, "Alpha", record)


if __name__ == "__main__":
    unittest.main()
