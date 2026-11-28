"""Tests for the map's Scenarios tab: HTTP API, auth boundaries, schema-3 fallback, and browser pure JS."""
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from richi import agents, map_access, memory, rules, scenario_runs, scenarios, serve


MAP = serve.BASE / "map.html"
MAP_SCENARIOS_JS = serve.BASE / "map_assets" / "scenarios.js"
BEGIN, END = "  // scenarios-pure: begin.", "  // scenarios-pure: end."


def clean_environment(test, home):
    patch = mock.patch.dict(os.environ, {"HOME": str(home)})
    patch.start()
    test.addCleanup(patch.stop)
    for key in [key for key in os.environ if key.startswith("RICHI_")
                or key in {"CODEX_THREAD_ID", "CLAUDECODE", "CURSOR_AGENT",
                           "ANTIGRAVITY_AGENT", "ANTIGRAVITY_CONVERSATION_ID",
                           "ANTIGRAVITY_APP_DATA_DIR",
                           "CLAUDE_CONFIG_DIR", "CODEX_HOME", "BROWSER", "XDG_CONFIG_HOME"}]:
        del os.environ[key]
    agents.forget_settings()
    test.addCleanup(agents.forget_settings)


def sample_definition(scenario_id="deploy-check", title="Deploy check"):
    return {
        "format": "richi-scenario",
        "format_version": 1,
        "id": scenario_id,
        "title": title,
        "description": "Synthetic pipeline for map UI tests",
        "tags": ["deploy", "qa"],
        "inputs": {
            "branch": {"type": "string", "title": "Branch", "required": True},
        },
        "steps": [
            {
                "id": "recall",
                "type": "memory.recall",
                "title": "Recall notes",
                "needs": [],
                "params": {"query": "{{inputs.branch}}", "limit": 5},
            },
            {
                "id": "approve",
                "type": "gate",
                "title": "Human confirmation",
                "needs": ["recall"],
                "params": {
                    "question": "Approve branch {{inputs.branch}}?",
                    "options": ["approve", "reject"],
                    "allow_comment": True,
                },
            },
        ],
        "layout": {
            "positions": {
                "recall": {"x": 36, "y": 36},
                "approve": {"x": 352, "y": 36},
            }
        },
    }


class MapScenariosAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-map-scenarios-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        clean_environment(self, self.root / "home")
        self.repo = self.root / "service"
        self.repo.mkdir()
        self.database = self.root / "store" / "memory.sqlite3"
        self.database.parent.mkdir()
        conn = memory.connect(self.database, create=True)
        try:
            memory.initialize(conn, self.database)
            memory.project_put(conn, {"id": "service", "name": "Service", "repo_path": str(self.repo)})
        finally:
            conn.close()
        self.server = serve.LoopbackHTTPServer(("127.0.0.1", 0), serve.handler_for(self.database))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.port = self.server.server_address[1]

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        hdrs = {"Host": f"127.0.0.1:{self.port}"}
        if headers:
            hdrs.update(headers)
        payload = body
        if isinstance(body, (dict, list)):
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        if method == "POST":
            hdrs.setdefault("Origin", f"http://127.0.0.1:{self.port}")
        try:
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            data = json.loads(raw.decode("utf-8")) if raw else None
            return resp.status, data
        finally:
            conn.close()

    def redeem_secret(self):
        ticket = map_access.issue_ticket(self.database)
        status, data = self.request("POST", "/api/session", {"ticket": ticket})
        self.assertEqual(status, 200, data)
        return data["secret"]

    def test_health_and_read_snapshot(self):
        status, health = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertIn("scenarios_editor", health["capabilities"])

        status, snap = self.request("GET", "/api/scenarios")
        self.assertEqual(status, 200, snap)
        self.assertEqual(snap["view"], "scenario-map")
        self.assertGreaterEqual(snap["schema_version"], 4)
        self.assertFalse(snap["editing"])
        self.assertEqual(snap["scenarios"], [])
        self.assertTrue(any(t["id"] == "review" and not t["shadowed"] for t in snap["builtin_templates"]))
        self.assertTrue(any(t["id"] == "deep-review" and not t["shadowed"] for t in snap["builtin_templates"]))
        self.assertEqual([p["id"] for p in snap["projects"]], ["service"])

        secret = self.redeem_secret()
        status, snap_auth = self.request("GET", "/api/scenarios", headers={map_access.HEADER: secret})
        self.assertEqual(status, 200)
        self.assertTrue(snap_auth["editing"])

        # Static map_assets routes serve cleanly with strict CSP and block unknown/traversal paths
        for asset, expected_ctype in serve.MAP_ASSETS.items():
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            try:
                conn.request("GET", f"/assets/{asset}", headers={"Host": f"127.0.0.1:{self.port}"})
                resp = conn.getresponse()
                body = resp.read()
                self.assertEqual(resp.status, 200, asset)
                self.assertEqual(resp.getheader("Content-Type"), expected_ctype)
                self.assertIn("script-src 'self';", resp.getheader("Content-Security-Policy") or "")
                self.assertGreater(len(body), 50)
            finally:
                conn.close()
        for bad_path in ("/assets/missing.js", "/assets/../serve.py", "/assets/map.css?v=1"):
            status, _ = self.request("GET", bad_path)
            self.assertEqual(status, 404, bad_path)

    def test_post_security_boundaries(self):
        change = {"action": "add", "scope": "global", "definition": sample_definition()}
        # Missing edit key -> 403 not_editing
        status, data = self.request("POST", "/api/scenarios", change)
        self.assertEqual(status, 403)
        self.assertEqual(data["code"], "not_editing")

        secret = self.redeem_secret()
        auth = {map_access.HEADER: secret}

        # Cross-origin rejected
        status, _ = self.request("POST", "/api/scenarios", change,
                                 headers={**auth, "Origin": "https://evil.example"})
        self.assertEqual(status, 403)

        # Cross-site Sec-Fetch-Site rejected
        status, _ = self.request("POST", "/api/scenarios", change,
                                 headers={**auth, "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

        # Wrong Content-Type rejected
        status, _ = self.request("POST", "/api/scenarios", b"{}",
                                 headers={**auth, "Content-Type": "text/plain"})
        self.assertEqual(status, 415)

        # Oversized body rejected (server replies 413 immediately on Content-Length without reading body)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.putrequest("POST", "/api/scenarios")
            c.putheader("Host", f"127.0.0.1:{self.port}")
            c.putheader("Origin", f"http://127.0.0.1:{self.port}")
            c.putheader("Content-Type", "application/json")
            c.putheader(map_access.HEADER, secret)
            c.putheader("Content-Length", str(serve.SCENARIOS_BODY_LIMIT + 10))
            c.endheaders()
            resp = c.getresponse()
            self.assertEqual(resp.status, 413)
        finally:
            c.close()

        # Excessively nested JSON rejected
        deep = "[" * (serve.SCENARIOS_BODY_DEPTH + 4) + "]" * (serve.SCENARIOS_BODY_DEPTH + 4)
        status, data = self.request("POST", "/api/scenarios", deep.encode("utf-8"),
                                    headers={**auth, "Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertEqual(data["code"], "invalid_request")

    def test_crud_cas_source_stripping_and_log(self):
        secret = self.redeem_secret()
        auth = {map_access.HEADER: secret}

        # Add global scenario; client-supplied source must be overwritten with "richi map"
        add_req = {
            "action": "add",
            "scope": "global",
            "source": "spoofed-source",
            "definition": sample_definition("deploy-check", "Deploy check"),
        }
        status, created = self.request("POST", "/api/scenarios", add_req, headers=auth)
        self.assertEqual(status, 200, created)
        scn = created["scenario"]
        self.assertEqual(scn["id"], "deploy-check")
        self.assertEqual(scn["version"], 1)
        self.assertEqual(scn["source"], serve.RULES_SOURCE)
        self.assertEqual(scn["changed_by"], serve.RULES_SOURCE)
        self.assertEqual(scn["definition"]["layout"]["positions"]["approve"], {"x": 352, "y": 36})

        # Add project-scoped scenario
        proj_req = {
            "action": "add",
            "scope": "project",
            "project": "service",
            "definition": sample_definition("svc-flow", "Service flow"),
        }
        status, created_proj = self.request("POST", "/api/scenarios", proj_req, headers=auth)
        self.assertEqual(status, 200, created_proj)
        self.assertEqual(created_proj["scenario"]["project_id"], "service")

        # Update with CAS
        updated_def = sample_definition("deploy-check", "Deploy check v2")
        upd_req = {
            "action": "update",
            "id": "deploy-check",
            "scope": "global",
            "expected_updated_at": scn["updated_at"],
            "definition": updated_def,
        }
        status, updated = self.request("POST", "/api/scenarios", upd_req, headers=auth)
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["scenario"]["version"], 2)

        # Stale CAS update -> 409 conflict
        status, conflict = self.request("POST", "/api/scenarios", upd_req, headers=auth)
        self.assertEqual(status, 409, conflict)
        self.assertEqual(conflict["code"], "conflict")

        # Snapshot reflects both scenarios and journal events
        status, snap = self.request("GET", "/api/scenarios", headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual({s["id"] for s in snap["scenarios"]}, {"deploy-check", "svc-flow"})
        actions = [ev["action"] for ev in snap["log"]]
        self.assertIn("scenario_created", actions)
        self.assertIn("scenario_updated", actions)

        # Remove with CAS
        rem_req = {
            "action": "remove",
            "id": "deploy-check",
            "scope": "global",
            "expected_updated_at": updated["scenario"]["updated_at"],
        }
        status, removed = self.request("POST", "/api/scenarios", rem_req, headers=auth)
        self.assertEqual(status, 200, removed)
        self.assertEqual(removed["status"], "removed")

    def test_runs_and_run_detail_endpoint(self):
        secret = self.redeem_secret()
        auth = {map_access.HEADER: secret}
        status, _ = self.request("POST", "/api/scenarios", {
            "action": "add", "scope": "global", "definition": sample_definition("gate-flow", "Gate Flow"),
        }, headers=auth)
        self.assertEqual(status, 200)

        with closing(memory.connect(self.database)) as conn, memory.transaction(conn, write=True):
            started = scenario_runs.start_run(
                conn, memory, self.database, "gate-flow",
                raw_inputs=["branch=main"],
            )
        run_id = started["run_id"]

        # Map snapshot lists the run with step_counts
        status, snap = self.request("GET", "/api/scenarios", headers=auth)
        self.assertEqual(status, 200)
        run_summary = next(r for r in snap["runs"] if r["id"] == run_id)
        self.assertEqual(run_summary["status"], "waiting")
        self.assertEqual(run_summary["step_counts"]["total"], 2)
        self.assertEqual(run_summary["step_counts"].get("done"), 1)
        self.assertEqual(run_summary["step_counts"].get("waiting_human"), 1)

        # Run detail includes scenario_title, rendered gate question/title, definition_steps, layout, and run log
        status, detail = self.request("GET", f"/api/scenario-run?id={run_id}")
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["run"]["scenario_title"], "Gate Flow")
        gate_step = next(s for s in detail["run"]["steps"] if s["id"] == "approve")
        self.assertEqual(gate_step["status"], "waiting_human")
        self.assertEqual(gate_step["title"], "Human confirmation")
        self.assertEqual(gate_step["gate"]["question"], "Approve branch main?")
        self.assertEqual(gate_step["gate"]["options"], ["approve", "reject"])
        self.assertTrue(gate_step["gate"]["allow_comment"])
        self.assertEqual([s["id"] for s in detail["run"]["definition_steps"]], ["recall", "approve"])
        self.assertEqual(detail["run"]["definition_steps"][1]["needs"], ["recall"])
        self.assertEqual(detail["run"]["layout"]["positions"]["approve"], {"x": 352, "y": 36})
        self.assertTrue(len(detail["log"]) >= 1)
        self.assertEqual(snap["runs_total"], 1)
        self.assertEqual(snap["runs_waiting"], 1)
        self.assertFalse(snap["runs_more"])
        self.assertEqual(len(snap["waiting_queue"]), 1)
        self.assertEqual(snap["waiting_queue"][0]["step_key"], "approve")
        self.assertEqual(snap["waiting_queue"][0]["question"], "Approve branch main?")
        self.assertEqual(snap["stats"]["total_runs"], 1)

        # Project-scoped run renders {{project.path}} and {{agent.id}} in /api/scenario-run gate prompt (F3)
        proj_def = {
            "format": "richi-scenario", "format_version": 1, "id": "proj-gate", "title": "Project Gate",
            "steps": [{
                "id": "ask", "type": "gate", "title": "Check {{run.id}}",
                "params": {"question": "Approve {{project.path}} by {{agent.id}}?", "options": ["yes", "no"]},
            }],
        }
        status, proj_res = self.request("POST", "/api/scenarios", {
            "action": "add", "scope": "project", "project": "service", "definition": proj_def,
        }, headers=auth)
        self.assertEqual(status, 200, proj_res)
        with closing(memory.connect(self.database)) as conn, memory.transaction(conn, write=True):
            p_run = scenario_runs.start_run(conn, memory, self.database, "proj-gate",
                                            project_id="service",
                                            author=rules.Author(by="codex:r2-test", agent="codex"))
        status, p_detail = self.request("GET", f"/api/scenario-run?id={p_run['run_id']}")
        self.assertEqual(status, 200, p_detail)
        p_gate = p_detail["run"]["steps"][0]["gate"]
        self.assertIn(str(self.repo), p_gate["question"])
        self.assertIn("codex", p_gate["question"])

        # Lone surrogate in JSON body returns 400 invalid_request rather than 500 (F6)
        raw_conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            raw_conn.request(
                "POST", "/api/scenarios",
                body=b'{"action":"add","scope":"global","definition":{"id":"bad-sur","title":"\\ud800","steps":[]}}',
                headers={
                    "Content-Type": "application/json",
                    "Origin": f"http://127.0.0.1:{self.port}",
                    map_access.HEADER: secret,
                },
            )
            raw_resp = raw_conn.getresponse()
            raw_data = json.loads(raw_resp.read().decode("utf-8"))
            self.assertEqual(raw_resp.status, 400, raw_data)
            self.assertEqual(raw_data.get("code"), "invalid_request")
        finally:
            raw_conn.close()

        # Large Cyrillic scenario (~130 KB UTF-8 across 10 steps, < 256 KB MAX_BYTES) must NOT inflate 3x to HTTP 413!
        ru_def = {
            "format": "richi-scenario",
            "format_version": 1,
            "id": "ru-large",
            "title": "Большой русский сценарий",
            "steps": [
                {
                    "id": f"s{i}",
                    "type": "instruction",
                    "title": f"Этап {i}",
                    "needs": [] if i == 1 else [f"s{i - 1}"],
                    "params": {"text": "Проверь архитектуру и тесты. " * 250},
                }
                for i in range(1, 11)
            ],
        }
        self.assertGreater(len(json.dumps(ru_def, ensure_ascii=False).encode("utf-8")), 120_000)
        status, ru_created = self.request("POST", "/api/scenarios", {
            "action": "add", "scope": "global", "definition": ru_def,
        }, headers=auth)
        self.assertEqual(status, 200, ru_created)

        # Error handling on /api/scenario-run
        status, missing = self.request("GET", "/api/scenario-run?id=run-does-not-exist")
        self.assertEqual(status, 404)
        self.assertEqual(missing["code"], "not_found")
        status, _ = self.request("GET", "/api/scenario-run")
        self.assertEqual(status, 400)
        status, _ = self.request("GET", "/api/scenario-run?id=abc&extra=1")
        self.assertEqual(status, 400)
        status, _ = self.request("GET", "/api/scenario-run?id=" + ("x" * 300))
        self.assertEqual(status, 414)

    def test_schema_v3_store_graceful_fallback(self):
        v3_db = self.root / "store" / "v3.sqlite3"
        conn = memory.connect(v3_db, create=True)
        try:
            memory.initialize(conn, v3_db)
            conn.execute("DROP TABLE scenario_events")
            conn.execute("DROP TABLE scenario_steps")
            conn.execute("DROP TABLE scenario_runs")
            conn.execute("PRAGMA user_version = 3")
            conn.commit()
        finally:
            conn.close()
        server = serve.LoopbackHTTPServer(("127.0.0.1", 0), serve.handler_for(v3_db))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=5)))
        port = server.server_address[1]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        try:
            c.request("GET", "/api/scenarios", headers={"Host": f"127.0.0.1:{port}"})
            resp = c.getresponse()
            snap = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 200)
            self.assertEqual(snap["schema_version"], 3)
            self.assertEqual(snap["runs"], [])
            self.assertEqual(snap["log_source"], "metadata")

            c.request("GET", "/api/scenario-run?id=run-any", headers={"Host": f"127.0.0.1:{port}"})
            resp = c.getresponse()
            err = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 409)
            self.assertEqual(err["code"], "schema_upgrade_required")
        finally:
            c.close()


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser-script tests")
class ScenariosPageLogicTests(unittest.TestCase):
    def evaluate(self, script):
        source = MAP_SCENARIOS_JS.read_text(encoding="utf-8")
        block = source[source.index(BEGIN):source.index(END)]
        prelude = "const assert = require('node:assert/strict');\n"
        result = subprocess.run([shutil.which("node"), "-e", prelude + block + "\n" + script],
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def test_dag_cycles_layout_edges_validation_and_commands(self):
        out = self.evaluate(r"""
const steps = [
  {id: 'a', type: 'memory.recall', title: 'A', needs: [], params: {query: 'q'}},
  {id: 'b', type: 'instruction', title: 'B', needs: ['a'], executor: {agent: 'codex'}, params: {text: 'do b'}},
  {id: 'c', type: 'skill', title: 'C', needs: ['a'], params: {skill: {default: 'review'}}},
  {id: 'd', type: 'gate', title: 'D', needs: ['b', 'c'], params: {question: 'ok?', options: ['ok', 'no']}},
  {id: 'chk', type: 'checklist', title: 'Check', needs: ['d'], params: {items: [{text: 'Verify tests', required: true}]}},
];
assert.deepEqual([...scenarioAncestors(steps, 'd')].sort(), ['a', 'b', 'c']);
assert.equal(scenarioWouldCycle(steps, 'a', 'a'), true);
assert.equal(scenarioWouldCycle(steps, 'd', 'a'), true);
assert.equal(scenarioWouldCycle(steps, 'd', 'b'), true);
assert.equal(scenarioWouldCycle(steps, 'b', 'c'), false);

const pos = scenarioAutoLayout(steps);
assert.ok(pos.a.x < pos.b.x);
assert.equal(pos.b.x, pos.c.x);
assert.ok(pos.b.y < pos.c.y);
assert.ok(pos.c.x < pos.d.x);

assert.ok(scenarioEdgePath(100, 50, 300, 50).startsWith('M100,50 C'));
assert.ok(scenarioEdgePath(300, 50, 100, 50).includes(' S'));

const draft = scenarioNormalizeDraft({
  id: 'my-flow',
  title: 'My Flow',
  tags: ['qa', 'qa', ''],
  inputs: {
    branch: {type: 'string', required: true},
    tok: {type: 'secret', default: 'leak'},
    env: {type: 'select', options: ['dev', 'dev', 'prod'], default: 'gone'},
  },
  steps: [
    ...steps,
    {id: 'e', type: 'condition', title: 'Cond', needs: ['chk', 'ghost'], params: {expr: "steps.d.answer == 'ok'"}, retry: {max: 2}, continue_on_failure: true},
  ],
  layout: {positions: {a: {x: 10.6, y: 20.4}, ghost: {x: 1, y: 1}}},
});
assert.equal(scenarioValidateDraft(draft), '');
assert.deepEqual(draft.tags, ['qa']);
assert.equal('default' in draft.inputs.tok, false);
assert.deepEqual(draft.inputs.env.options, ['dev', 'prod']);
assert.equal('default' in draft.inputs.env, false);
assert.deepEqual(draft.steps[1].executor, {agent: 'codex'});
assert.deepEqual(draft.steps[2].params.skill, {default: 'review'});
assert.deepEqual(draft.steps[4].params.items, [{text: 'Verify tests', required: true}]);
assert.deepEqual(draft.steps[5].needs, ['chk']);
assert.equal('retry' in draft.steps[5], false);
assert.equal('continue_on_failure' in draft.steps[5], false);
assert.deepEqual(draft.layout.positions, {a: {x: 11, y: 20}});

// Omitted needs defaults to sequential [previous]; blank/null/false numeric fields do not coerce to 0.
const shorthand = scenarioNormalizeDraft({
  id: 'seq', title: 'Seq',
  steps: [
    {id: 's1', type: 'memory.recall', title: 'One', params: {query: 'q', limit: '   '}},
    {id: 's2', type: 'command', title: 'Two', params: {command: 'echo hi', expect_exit: null, tail_lines: false}},
  ],
});
assert.deepEqual(shorthand.steps[0].needs, []);
assert.deepEqual(shorthand.steps[1].needs, ['s1']);
assert.equal('limit' in shorthand.steps[0].params, false);
assert.equal('expect_exit' in shorthand.steps[1].params, false);
assert.equal('tail_lines' in shorthand.steps[1].params, false);

// Validation catches bad id, cyclic graph, missing params
assert.notEqual(scenarioValidateDraft({...draft, id: '-bad'}), '');
const cyclic = scenarioNormalizeDraft({
  id: 'cyc', title: 'Cyc',
  steps: [
    {id: 'x', type: 'instruction', title: 'X', needs: ['y'], params: {text: 'x'}},
    {id: 'y', type: 'instruction', title: 'Y', needs: ['x'], params: {text: 'y'}},
  ],
});
assert.notEqual(scenarioValidateDraft(cyclic), '');

const applyReq = {action: 'add', scope: 'global', definition: draft};
const applyCmd = scenarioApplyCommand(applyReq, ['--workspace', 'my work']);
assert.ok(applyCmd.includes("'my work'"));
assert.ok(applyCmd.includes("<<'RICHI_SCENARIO'"));

const runCmdProj = scenarioRunCommand('my-flow', 'project', 'svc', {branch: "feat's $x"}, ['--workspace', 'w']);
const runCmdGlobal = scenarioRunCommand('my-flow', 'global', null, {branch: 'main'}, ['--workspace', 'w']);
assert.ok(!runCmdGlobal.includes('--global'));

const ansCmd = scenarioAnswerCommand('run-1', 'd', 'approve', "looks $good", ['--workspace', 'w']);
const ansCmdDash = scenarioAnswerCommand('run-1', 'd', '-reject', '', ['--workspace', 'w']);
assert.ok(!ansCmd.includes('--value'));

console.log(JSON.stringify({applyReq, applyCmd, runCmdProj, runCmdGlobal, ansCmd, ansCmdDash}));
""")
        # Verify the normalized draft produced by the browser JS is accepted by the real Python backend!
        with tempfile.TemporaryDirectory(prefix="richi-scn-js-") as tmp:
            db = Path(tmp) / "memory.sqlite3"
            conn = memory.connect(db, create=True)
            try:
                memory.initialize(conn, db)
                with memory.transaction(conn, write=True):
                    res = scenarios.change(conn, out["applyReq"], memory)
                self.assertEqual(res["status"], "created")
                self.assertEqual(res["scenario"]["id"], "my-flow")
            finally:
                conn.close()
        # Verify shell-quoted run & answer commands parse cleanly with the real CLI parser!
        parser = memory.parser()
        for cmd, expected in (
            (out["runCmdProj"], {"project": "svc", "input": ["branch=feat's $x"]}),
            (out["runCmdGlobal"], {"project": None, "input": ["branch=main"]}),
        ):
            words = shlex.split(cmd)
            self.assertEqual(words[0], "richi")
            parsed = parser.parse_args(words[1:])
            self.assertEqual(parsed.project, expected["project"])
            self.assertEqual(parsed.inputs, expected["input"])
        for cmd, exp_val, exp_comment in (
            (out["ansCmd"], "approve", "looks $good"),
            (out["ansCmdDash"], "-reject", None),
        ):
            words = shlex.split(cmd)
            self.assertEqual(words[0], "richi")
            parsed = parser.parse_args(words[1:])
            self.assertEqual(parsed.value, exp_val)
            self.assertEqual(parsed.comment, exp_comment)

    def test_token_parser_and_pipeline_linter(self):
        self.evaluate(r"""
const parsed = scenarioParseTokens('Check {{steps.s1.summary}} and {{inputs.branch | shell}}!');
assert.equal(parsed.length, 5);
assert.equal(parsed[1].root, 'steps');
assert.equal(parsed[1].target, 's1');
assert.equal(parsed[1].field, 'summary');
assert.equal(parsed[3].root, 'inputs');
assert.equal(parsed[3].target, 'branch');
assert.equal(parsed[3].filter, 'shell');

const cleanDef = {
  id: 'clean-pipe',
  title: 'Clean',
  inputs: {branch: {type: 'string', required: true}},
  steps: [
    {id: 's1', type: 'instruction', title: 'One', needs: [], params: {text: 'Review {{inputs.branch}}'}},
    {id: 's2', type: 'command', title: 'Two', needs: ['s1'], params: {command: 'git diff {{inputs.branch | shell}}'}},
    {id: 's3', type: 'skill', title: 'Three', needs: ['s2'], params: {skill: {default: 'known-skill'}, instruction: 'Use {{steps.s1.summary}}'}},
  ],
};
const okRes = scenarioLintDraft(cleanDef, ['known-skill']);
assert.equal(okRes.ok, true);
assert.equal(okRes.issues.length, 0);
assert.equal(okRes.byStep.s1.severity, 'ok');

const badDef = {
  id: 'bad-pipe',
  title: 'Bad',
  inputs: {tok: {type: 'secret'}},
  steps: [
    {id: 's1', type: 'instruction', title: 'One', needs: [], params: {text: 'Use {{steps.s2.summary}}'}},
    {id: 's2', type: 'command', title: 'Two', needs: [], params: {command: 'echo {{project.path}}'}},
    {id: 's3', type: 'gate', title: 'Three', needs: ['s1'], params: {question: 'Leak {{inputs.tok}}?'}},
    {id: 's4', type: 'skill', title: 'Four', needs: ['s3'], params: {skill: {default: 'missing-skill'}}},
  ],
};
const badRes = scenarioLintDraft(badDef, ['known-skill']);
assert.equal(badRes.ok, false);
assert.equal(badRes.byStep.s1.severity, 'err');
assert.equal(badRes.byStep.s2.severity, 'err');
assert.equal(badRes.byStep.s3.severity, 'err');
assert.equal(badRes.byStep.s4.severity, 'warn');

// Declarative incoming token toggle (prevents accidental duplicate spamming!)
const tok = '{{steps.self_review.summary}}';
assert.equal(scenarioHasToken('Compare {{ steps.self_review.summary }} now', tok), true);
assert.equal(scenarioHasToken('Compare nothing yet', tok), false);
const added = scenarioToggleToken('Review findings:', tok);
assert.equal(added.active, true);
assert.equal(added.value, 'Review findings: {{steps.self_review.summary}}');
const removed = scenarioToggleToken(added.value, tok);
assert.equal(removed.active, false);
assert.equal(removed.value, 'Review findings:');
const removedParen = scenarioToggleToken('Compare ({{steps.self_review.summary}}) and foo().', tok);
assert.equal(removedParen.active, false);
assert.equal(removedParen.value, 'Compare and foo().');

// Indexed inputs, multiline commands, numeric bounds, typo'd needs, and matrix gate refs
const idxDef = {
  id: 'idx-ok', title: 'Indexed',
  inputs: {angles: {type: 'multiselect', options: ['sec', 'perf']}},
  steps: [{id: 's1', type: 'instruction', title: 'One', needs: [], params: {text: 'Focus {{inputs.angles[0]}}'}}],
};
assert.equal(scenarioLintDraft(idxDef).ok, true);

const multiCmd = {
  id: 'multi-cmd', title: 'Multi',
  steps: [
    {id: 'c1', type: 'command', title: 'Cmd', needs: ['ghost_step'], params: {command: 'echo a\necho b', expect_exit: 300, tail_lines: 999}},
    {id: 'r1', type: 'memory.recall', title: 'Rec', needs: [], params: {query: 'q', limit: 99}},
  ],
};
const multiLint = scenarioLintDraft(multiCmd);
assert.equal(multiLint.ok, false);
assert.equal(multiLint.byStep.c1.severity, 'err');
assert.equal(multiLint.byStep.r1.severity, 'err');
assert.notEqual(scenarioValidateDraft(scenarioNormalizeDraft(multiCmd)), '');

// Draft baseline dirty comparison
assert.equal(scenarioDraftChanged({id: 'a', steps: []}, {id: 'a', steps: []}), false);
assert.equal(scenarioDraftChanged({id: 'a', steps: [{id: 'x'}]}, {id: 'a', steps: []}), true);
""")


class ScenariosHtmlSafetyTests(unittest.TestCase):
    def test_scenarios_markup_present_and_no_unsafe_html_sinks(self):
        source = MAP.read_text(encoding="utf-8")
        self.assertIn('id="scenarios-tab"', source)
        self.assertIn('id="scenarios-panel"', source)
        self.assertIn('id="scenarios-content"', source)
        for asset in serve.MAP_ASSETS:
            path = serve.BASE / "map_assets" / asset
            self.assertTrue(path.is_file(), f"Missing map asset {asset}")
            if asset.endswith(".js"):
                text = path.read_text(encoding="utf-8")
                for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
                    self.assertNotIn(sink, text, f"Unsafe DOM sink {sink} found in map_assets/{asset}")


if __name__ == "__main__":
    unittest.main()
