"""Integration tests use isolated databases; never touch the user's knowledge store."""
import concurrent.futures
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cli_environment import cli_environment



class MemoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="project-memory-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "knowledge with spaces.sqlite3"
        self.counter = 0
        self.run_cli("init")
        self.put("project", [
            {"id": "alpha", "name": "Alpha", "source": "test fixture"},
            {"id": "beta", "name": "Beta", "source": "test fixture"},
        ], command="upsert")

    def run_cli(self, *args, ok=True, db=None):
        result = subprocess.run(
            [sys.executable, "-m", "richi", "--db", str(db or self.db), *args],
            capture_output=True, text=True, timeout=20, env=cli_environment(self.db.parent)
        )
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        if result.stdout.strip():
            return json.loads(result.stdout)
        return {}

    def payload_file(self, payload):
        self.counter += 1
        path = self.root / ("payload %d.json" % self.counter)
        path.write_text(json.dumps(payload, ensure_ascii=False))
        return str(path)

    def put(self, entity, payload, command="put", ok=True):
        return self.run_cli(entity, command, "--json", self.payload_file(payload), ok=ok)

    def entry(self, ident="experiment:pooling", **overrides):
        value = {
            "id": ident, "kind": "experiment", "title": "Проверка пула соединений",
            "summary": "Connection pooling: p99 вырос, изменение отклонено.",
            "project_ids": ["alpha"], "work_state": "done", "knowledge_state": "confirmed",
            "sources": [{"reference": "test://measurement/123", "type": "measurement"}],
            "tags": ["redis", "latency"], "aliases": ["пул соединений", "задержки"],
            "verified_at": "2026-09-29T10:00:00Z",
        }
        value.update(overrides)
        return value

    def get(self, ident):
        value = self.run_cli("entry", "get", ident, "--history")
        return value.get("entry", value)

    def result_ids(self, response):
        values = response.get("entries", response.get("results", []))
        return [item["id"] for item in values]

    def test_roundtrip_unicode_and_failed_experiment_without_jira(self):
        payload = self.entry()
        self.put("entry", payload)
        value = self.get(payload["id"])
        for key in ("id", "kind", "title", "summary", "sources"):
            self.assertEqual(value[key], payload[key])
        for key in ("tags", "aliases", "project_ids"):
            self.assertEqual(sorted(value[key]), sorted(payload[key]))
        self.assertEqual(value["work_state"], "done")
        self.assertIsNone(value.get("jira_key"))

    def test_retry_does_not_duplicate_history_and_update_preserves_previous_value(self):
        payload = self.entry()
        self.put("entry", payload)
        first = self.get(payload["id"])
        self.put("entry", payload)
        retry = self.get(payload["id"])
        self.assertEqual(first["created_at"], retry["created_at"])
        self.assertEqual(first["history"], retry["history"])
        changed = dict(payload, summary="Новый замер показал регрессию только под нагрузкой.")
        self.put("entry", changed)
        updated = self.get(payload["id"])
        self.assertEqual(updated["created_at"], first["created_at"])
        self.assertEqual(updated["summary"], changed["summary"])
        self.assertGreater(len(updated["history"]), len(first["history"]))
        self.assertIn(payload["summary"], json.dumps(updated["history"], ensure_ascii=False))

    def test_search_alias_project_filter_and_fts_update(self):
        payload = self.entry()
        self.put("entry", payload)
        self.assertIn(payload["id"], self.result_ids(self.run_cli("search", "задержки")))
        self.assertEqual([], self.result_ids(self.run_cli("search", "задержки", "--project", "beta")))
        self.put("entry", dict(payload, aliases=["exclusivealias"], tags=[], title="Experiment", summary="Updated"))
        self.assertEqual([], self.result_ids(self.run_cli("search", "задержки")))
        self.assertIn(payload["id"], self.result_ids(self.run_cli("search", "exclusivealias")))

    def test_superseded_record_excluded_from_search_and_context_but_retrievable(self):
        payload = self.entry(knowledge_state="superseded")
        self.put("entry", payload)
        self.assertEqual([], self.result_ids(self.run_cli("search", "pooling")))
        self.assertEqual([], self.result_ids(self.run_cli("context", "--project", "alpha")))
        self.assertEqual(self.get(payload["id"])["knowledge_state"], "superseded")

    def test_bad_batch_rolls_back_and_unknown_project_rejected(self):
        self.put("entry", [self.entry(), self.entry("bad", project_ids=["missing"])], ok=False)
        self.run_cli("entry", "get", "experiment:pooling", ok=False)

    def test_bad_fields_and_unsupported_states_are_rejected(self):
        for payload in (
            self.entry(work_state="deployed-maybe"),
            self.entry(kind="fact", sources=[]),
            self.entry(kind="decision", verified_at="yesterday"),
            self.entry(tags="latency"),
            self.entry(project_ids="alpha"),
            self.entry(knowledge_state="guessed"),
        ):
            with self.subTest(payload=payload):
                self.put("entry", payload, ok=False)

    def test_search_query_is_literal_and_does_not_execute_fts_or_sql_syntax(self):
        self.put("entry", self.entry())
        for query in ('" OR *', "'); DROP TABLE entries; --", "redis NEAR(foo", "pooling:xxx"):
            with self.subTest(query=query):
                self.run_cli("search", query)
        self.assertEqual(self.get("experiment:pooling")["id"], "experiment:pooling")

    def test_relations_are_visible_in_both_directions(self):
        self.put("relation", {
            "id": "alpha-uses-beta", "from_project": "alpha", "to_project": "beta",
            "kind": "uses_library", "description": "Alpha imports Beta",
            "source": "test://alpha/manifest", "knowledge_state": "confirmed",
            "verified_at": "2026-09-29T10:00:00Z",
        })
        for project in ("alpha", "beta"):
            value = self.run_cli("context", "--project", project)
            self.assertEqual(len(value["relations"]), 1)
            self.assertEqual(value["relations"][0]["from_project"], "alpha")
            self.assertEqual(value["relations"][0]["to_project"], "beta")

    def test_sqlite_backup_preserves_content_and_original_remains_usable(self):
        self.put("entry", self.entry())
        backup = self.root / "backup.sqlite3"
        self.run_cli("backup", "--output", str(backup))
        self.run_cli("check", db=backup)
        restored = self.run_cli("entry", "get", "experiment:pooling", db=backup)
        self.assertIn("pooling", json.dumps(restored))
        self.put("entry", self.entry("experiment:second"))
        self.run_cli("entry", "get", "experiment:second", db=backup, ok=False)

    def test_concurrent_writers_do_not_lose_distinct_entries(self):
        files = [self.payload_file(self.entry("experiment:%d" % i)) for i in range(8)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda path: self.run_cli("entry", "put", "--json", path), files))
        self.assertEqual(len(results), 8)
        for i in range(8):
            self.assertEqual(self.get("experiment:%d" % i)["id"], "experiment:%d" % i)
        self.run_cli("check")

    def test_future_database_schema_is_not_silently_downgraded(self):
        with sqlite3.connect(str(self.db)) as connection:
            connection.execute("PRAGMA user_version = 999;")
        self.run_cli("init", ok=False)
        with sqlite3.connect(str(self.db)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version;").fetchone()[0], 999)

    def test_stale_update_cannot_overwrite_concurrent_findings(self):
        original = self.entry()
        self.put("entry", original)
        read = self.get(original["id"])
        first_writer = dict(original, summary="Новый результат первого автора", expected_updated_at=read["updated_at"])
        self.put("entry", first_writer)
        stale_writer = dict(original, summary="Устаревшая версия второго автора", expected_updated_at=read["updated_at"])
        self.put("entry", stale_writer, ok=False)
        self.assertEqual(self.get(original["id"])["summary"], first_writer["summary"])

    def test_idempotency_key_replay_and_conflicting_reuse(self):
        payload = self.entry(event_id="write-once")
        self.put("entry", payload)
        before = self.get(payload["id"])
        self.put("entry", payload)
        self.assertEqual(before["history"], self.get(payload["id"])["history"])
        self.put("entry", dict(payload, summary="Different event payload"), ok=False)
        self.assertEqual(self.get(payload["id"])["summary"], payload["summary"])

    def test_backup_refuses_to_overwrite_an_existing_file(self):
        target = self.root / "keep-me.sqlite3"
        target.write_bytes(b"existing valuable data")
        self.run_cli("backup", "--output", str(target), ok=False)
        self.assertEqual(target.read_bytes(), b"existing valuable data")


if __name__ == "__main__":
    unittest.main()
