"""The MCP server speaks both protocol eras and runs tool calls as launcher commands.

The first class drives the protocol in process with a recorded launcher; the
second starts `richi mcp` against a synthetic home, as a client would.
"""
import io
import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from richi import __version__, mcp_server, mcp_tools, memory
from richi_launcher import maps
from richi_launcher.runtime import bootstrap_command

from cli_environment import cli_environment

CHAT = "claude:0af4cd88-675d-4258-a011-fdab17bf6333"
OTHER = "claude:7f3b9c1e-2222-4d5e-8a6b-000000000001"
META = {mcp_server.VERSION: "2026-07-28", mcp_server.CLIENT_CAPABILITIES: {},
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"}}
RECORD = {"id": "decision:atlas-pool-sizing", "kind": "decision", "knowledge_state": "confirmed",
          "title": "Atlas: размер пула по измеренной нагрузке",
          "summary": "The pool is sized from the measured peak of concurrent requests.\nNot from the core count.",
          "sources": [{"reference": "https://example.test/atlas/pull/42"}], "expected_updated_at": None}


def request(identity, method, params=None, meta=META):
    params = dict(params or {})
    if meta is not None:
        params["_meta"] = meta
    return json.dumps({"jsonrpc": "2.0", "id": identity, "method": method, "params": params})


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.launched = []
        self.reply = (0, '{"results":[]}\n', "")

        def launch(argv, stdin):
            self.launched.append((argv, stdin))
            return self.reply
        self.server = mcp_server.Server(memory.parser(), launch=launch)

    def ask(self, method, params=None, meta=META):
        reply = self.server.handle(request(7, method, params, meta))
        self.assertEqual((reply["jsonrpc"], reply["id"]), ("2.0", 7))
        return reply

    def test_discovery_states_versions_tools_and_the_isolation_rules(self):
        result = self.ask("server/discover")["result"]
        self.assertEqual(result["supportedVersions"], ["2026-07-28"])
        self.assertEqual(result["capabilities"], {"tools": {}})
        self.assertEqual(result["instructions"], mcp_tools.INSTRUCTIONS)
        self.assertEqual(result["_meta"], {mcp_server.SERVER_INFO: {"name": "richi", "version": __version__}})
        # The revision requires caching hints on discovery and on the tool list.
        self.assertEqual((result["resultType"], result["ttlMs"], result["cacheScope"]), ("complete", 0, "public"))

    def test_tool_list_is_the_profile_in_a_stable_order(self):
        listed = self.ask("tools/list")["result"]
        self.assertEqual(listed["tools"], mcp_tools.definitions(memory.parser(), "core"))
        self.assertEqual((listed["resultType"], listed["ttlMs"], listed["cacheScope"]), ("complete", 0, "public"))
        self.assertEqual(self.ask("tools/list")["result"], listed)
        every = mcp_server.Server(memory.parser(), "all").handle(request(1, "tools/list"))["result"]["tools"]
        self.assertEqual(every, mcp_tools.definitions(memory.parser(), "all"))
        self.assertEqual(self.launched, [])

    def test_modern_requests_are_judged_one_by_one(self):
        old = dict(META, **{mcp_server.VERSION: "2025-11-25"})
        refused = self.ask("tools/list", meta=old)["error"]
        self.assertEqual((refused["code"], refused["data"]), (-32022, {"supported": ["2026-07-28"], "requested": "2025-11-25"}))
        missing = self.ask("tools/list", meta={mcp_server.VERSION: "2026-07-28"})["error"]
        self.assertEqual(missing["code"], -32602)
        self.assertIn(mcp_server.CLIENT_CAPABILITIES, missing["message"])
        # A refused request leaves nothing behind: the next one is served.
        self.assertIn("tools", self.ask("tools/list")["result"])
        self.assertEqual(self.ask("resources/list")["error"]["code"], -32601)

    def test_requests_without_metadata_need_the_handshake_of_an_earlier_revision(self):
        refused = self.ask("tools/list", meta=None)["error"]
        self.assertEqual(refused["code"], -32602)
        for version in ("2026-07-28", "2025-11-25", "initialize"):
            self.assertIn(version, refused["message"])
        self.assertEqual(self.ask("ping", meta=None)["result"], {})
        started = self.ask("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}, meta=None)["result"]
        self.assertEqual(started, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                   "serverInfo": {"name": "richi", "version": __version__},
                                   "instructions": mcp_tools.INSTRUCTIONS})
        self.assertIsNone(self.server.handle('{"jsonrpc":"2.0","method":"notifications/initialized"}'))
        listed = self.ask("tools/list", meta=None)["result"]
        self.assertEqual(set(listed), {"tools"})
        called = self.ask("tools/call", {"name": "check", "arguments": {"chat": CHAT}}, meta=None)
        self.assertEqual(called["error"]["code"], -32602)  # check is not in the core profile
        called = self.ask("tools/call", {"name": "entry_get", "arguments": {"chat": CHAT, "id": "fact:a"}}, meta=None)
        self.assertEqual(set(called["result"]), {"content", "isError"})
        # The same connection still serves a self-contained request.
        self.assertEqual(self.ask("tools/list")["result"]["resultType"], "complete")

    def test_an_unknown_handshake_version_gets_the_newest_earlier_revision(self):
        for requested in ("2026-07-28", "1999-01-01", None):
            server = mcp_server.Server(memory.parser(), launch=None)
            result = server.handle(request(1, "initialize", {"protocolVersion": requested}, None))["result"]
            self.assertEqual(result["protocolVersion"], "2025-11-25")

    def test_a_call_is_one_launcher_command_selected_by_its_chat(self):
        self.reply = (0, '{"query":"pool","results":[{"id":"decision:a"}]}\n', "")
        result = self.ask("tools/call", {"name": "recall", "arguments": {
            "chat": CHAT, "project_path": "/work/atlas", "query": "pool", "limit": 3}})["result"]
        self.assertEqual(self.launched, [(["--chat=" + CHAT, "--compact-json", "--project-path=/work/atlas",
                                           "recall", "--limit=3", "--", "pool"], None)])
        self.assertEqual(result["content"], [{"type": "text", "text": '{"query":"pool","results":[{"id":"decision:a"}]}'}])
        self.assertIs(result["isError"], False)
        self.ask("tools/call", {"name": "entry_put", "arguments": {"chat": CHAT, "records": [RECORD]}})
        argv, stdin = self.launched[-1]
        self.assertEqual(argv, ["--chat=" + CHAT, "--compact-json", "entry", "put", "--json=-"])
        self.assertEqual(json.loads(stdin), [RECORD])
        self.ask("tools/call", {"name": mcp_tools.RUN_READ, "arguments": {"chat": OTHER, "command": "check"}})
        self.assertEqual(self.launched[-1], (["--chat=" + OTHER, "--compact-json", "check"], None))

    def test_a_failed_command_is_a_tool_error_that_keeps_its_next_step(self):
        refusal = {"error": "Choose a workspace for this chat before accessing knowledge", "code": "chat_workspace_required",
                   "workspaces": ["default"], "next_commands": ["richi chat bind --current", "richi chat bind NAME"]}
        self.reply = (1, "", "warning: unrelated line\n" + json.dumps(refusal) + "\n")
        result = self.ask("tools/call", {"name": "recall", "arguments": {"chat": OTHER, "query": "pool"}})["result"]
        self.assertIs(result["isError"], True)
        self.assertEqual(json.loads(result["content"][0]["text"]), refusal)
        self.reply = (1, "", "Traceback (most recent call last):\n  boom\n")
        failed = json.loads(self.ask("tools/call", {"name": "recall", "arguments": {"chat": CHAT, "query": "q"}})
                            ["result"]["content"][0]["text"])
        self.assertEqual(failed["code"], "command_failed")
        self.assertIn("boom", failed["error"])
        self.reply = (0, "x" * (mcp_server.OUTPUT_LIMIT + 1), "")
        large = self.ask("tools/call", {"name": "recall", "arguments": {"chat": CHAT, "query": "q"}})["result"]
        self.assertEqual((large["isError"], json.loads(large["content"][0]["text"])["code"]), (True, "output_too_large"))

    def test_calls_the_caller_can_correct_never_reach_the_launcher(self):
        for arguments, message in (({"query": "pool"}, "Missing arguments: chat"),
                                   ({"chat": CHAT, "query": "pool", "db": "/tmp/other"}, "Unknown arguments: db"),
                                   (["pool"], "arguments must be an object"), (None, "Missing arguments: chat, query")):
            with self.subTest(message=message):
                result = self.ask("tools/call", {"name": "recall", "arguments": arguments})["result"]
                self.assertEqual(json.loads(result["content"][0]["text"]), {"error": message, "code": "usage_error"})
                self.assertIs(result["isError"], True)
        for name in ("no_such_tool", "tools_get", None, 5):
            with self.subTest(name=name):
                refused = self.ask("tools/call", {"name": name, "arguments": {"chat": CHAT}})["error"]
                self.assertEqual(refused["code"], -32602)
        self.assertEqual(self.launched, [])

    def test_a_chat_identity_is_given_without_a_command(self):
        first = self.ask("tools/call", {"name": mcp_tools.OPEN})["result"]
        second = self.ask("tools/call", {"name": mcp_tools.OPEN, "arguments": {}})["result"]
        chats = [json.loads(result["content"][0]["text"])["chat"] for result in (first, second)]
        self.assertEqual((first["isError"], len(set(chats)), self.launched), (False, 2, []))
        self.ask("tools/call", {"name": mcp_tools.STATUS, "arguments": {"chat": chats[0]}})
        self.ask("tools/call", {"name": mcp_tools.BIND, "arguments": {"chat": chats[0], "workspace": "atlas"}})
        self.assertEqual(self.launched, [(["--chat=" + chats[0], "--compact-json", "chat", "current"], None),
                                         (["--chat=" + chats[0], "--compact-json", "chat", "bind", "--", "atlas"], None)])

    def test_the_clients_thread_in_the_request_metadata_becomes_the_chat(self):
        # As Codex calls: the handshake of an earlier revision, then its thread ID with every tool call.
        thread = "01a1ca8a-1201-78d1-b2cd-3a0ae11d5dcb"
        self.ask("initialize", {"protocolVersion": "2025-06-18", "capabilities": {"elicitation": {}},
                                "clientInfo": {"name": "codex-mcp-client", "version": "0.144.4"}}, meta=None)
        meta = {"progressToken": 2, "threadId": thread, "x-codex-turn-metadata": {"thread_id": thread}}
        opened = json.loads(self.ask("tools/call", {"name": mcp_tools.OPEN, "arguments": {}}, meta=meta)["result"]["content"][0]["text"])
        self.assertEqual((opened["chat"], opened["source"]), (thread, "client"))
        modern = json.loads(self.ask("tools/call", {"name": mcp_tools.OPEN}, meta=dict(META, threadId=thread))
                            ["result"]["content"][0]["text"])
        self.assertEqual(modern["chat"], thread)
        self.assertEqual(self.launched, [])

    def test_describe_answers_without_a_chat_or_a_command(self):
        result = self.ask("tools/call", {"name": mcp_tools.DESCRIBE, "arguments": {"command": "sources_impact"}})["result"]
        self.assertEqual(json.loads(result["content"][0]["text"]),
                         mcp_tools.describe(memory.parser(), {"command": "sources_impact"}))
        self.assertIs(result["isError"], False)
        bad = self.ask("tools/call", {"name": mcp_tools.DESCRIBE, "arguments": {"command": "no_such"}})["result"]
        self.assertIs(bad["isError"], True)
        self.assertEqual(self.launched, [])

    def test_a_guide_is_answered_as_plain_text_without_a_chat_or_a_command(self):
        for arguments in (None, {}, {"name": "ENTRY_WRITE.md"}):
            params = dict({"name": mcp_tools.GUIDE}, **({} if arguments is None else {"arguments": arguments}))
            result = self.ask("tools/call", params)["result"]
            # Not JSON in a string: the text is read as it was written.
            self.assertEqual((result["content"], result["isError"]),
                             ([{"type": "text", "text": mcp_tools.guide(arguments or {})}], False))
        bad = self.ask("tools/call", {"name": mcp_tools.GUIDE, "arguments": {"name": "NO_SUCH.md"}})["result"]
        self.assertEqual((bad["isError"], json.loads(bad["content"][0]["text"])["code"]), (True, "usage_error"))
        self.assertEqual(self.launched, [])

    def test_messages_that_need_no_reply_get_none_and_broken_ones_get_an_error(self):
        for line in ('{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":1}}',
                     '{"jsonrpc":"2.0","id":3,"result":{}}', '{"jsonrpc":"2.0","id":3,"error":{"code":1,"message":"x"}}'):
            self.assertIsNone(self.server.handle(line))
        for line, code, identity in (("{not json}", -32700, None), ('[{"jsonrpc":"2.0","id":1,"method":"ping"}]', -32600, None),
                                     ('{"id":4,"method":"ping"}', -32600, 4), ('{"jsonrpc":"2.0","id":null,"method":"ping"}', -32600, None),
                                     ('{"jsonrpc":"2.0","id":true,"method":"ping"}', -32600, None)):
            with self.subTest(line=line):
                reply = self.server.handle(line)
                self.assertEqual((reply["error"]["code"], reply["id"]), (code, identity))
        self.assertEqual(self.server.handle('{"jsonrpc":"2.0","id":8,"method":"tools/list","params":[1]}')["error"]["code"], -32602)
        self.assertEqual(self.server.handle('{"jsonrpc":"2.0","id":"a","method":"ping","params":null}'),
                         {"jsonrpc": "2.0", "id": "a", "result": {}})

    def test_a_failure_inside_a_call_is_reported_and_the_server_goes_on(self):
        def broken(argv, stdin):
            raise OSError("cannot start the command")
        server = mcp_server.Server(memory.parser(), launch=broken)
        reply = server.handle(request(2, "tools/call", {"name": "recall", "arguments": {"chat": CHAT, "query": "q"}}))
        self.assertEqual(reply["error"]["code"], -32603)
        self.assertIn("cannot start the command", reply["error"]["message"])
        self.assertIn("tools", server.handle(request(3, "tools/list"))["result"])

    def test_every_reply_is_one_line_of_utf8(self):
        self.reply = (0, json.dumps({"title": RECORD["title"], "summary": RECORD["summary"]}, ensure_ascii=False) + "\n", "")
        lines = [request(1, "server/discover"), "", "   ", '{"jsonrpc":"2.0","method":"notifications/initialized"}',
                 request(2, "tools/call", {"name": "entry_get", "arguments": {"chat": CHAT, "id": RECORD["id"]}}), "{broken"]
        wire = io.BytesIO()
        mcp_server.serve(self.server, io.BytesIO(("\n".join(lines) + "\n").encode("utf-8")), wire)
        replies = wire.getvalue().split(b"\n")
        self.assertEqual(replies.pop(), b"")
        self.assertEqual([json.loads(reply).get("id") for reply in replies], [1, 2, None])
        text = json.loads(replies[1])["result"]["content"][0]["text"]
        self.assertEqual(json.loads(text)["summary"], RECORD["summary"])
        self.assertIn("размер пула".encode("utf-8"), replies[1])

    def test_a_command_inherits_neither_the_protocol_stream_nor_the_servers_selection(self):
        pinned = {"RICHI_CONFIG": "/pinned/config.json", "RICHI_DATA_DIR": "/pinned", "RICHI_WORKSPACE": "default",
                  "RICHI_DB": "/pinned/memory.sqlite3", "RICHI_ACTIVE_CONFIG": "/pinned/config.json",
                  "RICHI_ACTIVE_WORKSPACE": "pinned", "RICHI_ACTIVE_RUNTIME": "{}", "RICHI_CHAT_ID": "claude:server-start",
                  "CODEX_THREAD_ID": "thread", "HOME": "/synthetic/home", "RICHI_INSTALLATION_GENERATION": "seed"}
        done = subprocess.CompletedProcess([], 0, '{"status":"ok"}\n', "")
        with mock.patch.dict(os.environ, pinned, clear=True), mock.patch.object(subprocess, "run", return_value=done) as run:
            self.assertEqual(mcp_server.launch(["--chat=" + CHAT, "--compact-json", "check"], None), (0, '{"status":"ok"}\n', ""))
            mcp_server.launch(["--chat=" + CHAT, "--compact-json", "entry", "put", "--json=-"], "[]")
        first, second = run.call_args_list
        self.assertEqual(first.args[0], bootstrap_command(["--chat=" + CHAT, "--compact-json", "check"]))
        self.assertEqual(first.kwargs["env"], {"HOME": "/synthetic/home", "RICHI_INSTALLATION_GENERATION": "seed"})
        self.assertIs(first.kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("input", first.kwargs)
        self.assertEqual((second.kwargs["input"], "stdin" in second.kwargs), ("[]", False))
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("richi", mcp_server.CALL_TIMEOUT)):
            status, out, err = mcp_server.launch(["--chat=" + CHAT, "check"], None)
        self.assertEqual((status, out, json.loads(err)["code"]), (1, "", "timeout"))


class Client:
    """A client's end of `richi mcp`: raw newline-delimited JSON-RPC."""

    def __init__(self, test, environment, arguments=(), command=None):
        self.test, self.identity = test, 0
        self.process = subprocess.Popen(command or bootstrap_command(["mcp", *arguments]), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
        test.addCleanup(self.close)

    def read(self):
        ready, _, _ = select.select([self.process.stdout], [], [], 60)
        self.test.assertTrue(ready, "the server did not reply")
        line = self.process.stdout.readline()
        self.test.assertTrue(line.endswith(b"\n"), self.process.stderr.read() if not line else line)
        return json.loads(line)

    def send(self, text):
        self.process.stdin.write(text.encode("utf-8") + b"\n")
        self.process.stdin.flush()

    def ask(self, method, params=None, meta=META):
        self.identity += 1
        self.send(request(self.identity, method, params, meta))
        reply = self.read()
        self.test.assertEqual(reply["id"], self.identity)
        return reply

    def call(self, name, arguments, meta=META):
        result = self.ask("tools/call", {"name": name, "arguments": arguments}, meta)["result"]
        return result["isError"], json.loads(result["content"][0]["text"])

    def close(self):
        """End the input, as a client does; returns the exit status and what was left on the streams."""
        if self.process.stdin.closed:
            return None
        self.process.stdin.close()
        status = self.process.wait(timeout=30)
        rest = self.process.stdout.read(), self.process.stderr.read()
        self.process.stdout.close()
        self.process.stderr.close()
        return (status,) + rest


class ServerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-mcp-server-")
        self.addCleanup(temporary.cleanup)
        self.environment = cli_environment(temporary.name)
        self.launcher(["init"])
        self.launcher(["--chat=" + CHAT, "chat", "bind", "--current"])

    def launcher(self, argv):
        result = subprocess.run(bootstrap_command(argv), capture_output=True, text=True, env=self.environment, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result.stdout

    def test_a_modern_client_discovers_writes_and_reads(self):
        client = Client(self, self.environment)
        discovered = client.ask("server/discover")["result"]
        self.assertEqual(discovered["supportedVersions"], ["2026-07-28"])
        names = [tool["name"] for tool in client.ask("tools/list")["result"]["tools"]]
        self.assertEqual(names[-3:], [mcp_tools.DESCRIBE, mcp_tools.RUN_READ, mcp_tools.RUN_WRITE])
        # The running server finds its guides, in a checkout and in an installed package alike.
        for arguments, heading in (({}, "# Project memory"), ({"name": "ENTRY_WRITE.md"}, "# ")):
            guide = client.ask("tools/call", {"name": mcp_tools.GUIDE, "arguments": arguments})["result"]
            self.assertIs(guide["isError"], False)
            self.assertTrue(guide["content"][0]["text"].startswith(mcp_tools.NOTE + heading), guide["content"][0]["text"][:800])
        self.assertEqual(client.call("entry_put", {"chat": CHAT, "records": [RECORD]}),
                         (False, {"results": [{"id": RECORD["id"], "status": "created"}]}))
        failed, stored = client.call("entry_get", {"chat": CHAT, "id": RECORD["id"]})
        self.assertEqual((failed, stored["title"], stored["summary"]), (False, RECORD["title"], RECORD["summary"]))
        failed, recalled = client.call("recall", {"chat": CHAT, "query": "pool sized from measured peak", "limit": 3})
        self.assertEqual((failed, [item["id"] for item in recalled["results"]]), (False, [RECORD["id"]]))
        self.assertEqual(client.call(mcp_tools.RUN_READ, {"chat": CHAT, "command": "check"})[1]["status"], "ok")
        failed, conflict = client.call("entry_put", {"chat": CHAT, "records": [dict(RECORD, title="Changed")]})
        self.assertTrue(failed)
        self.assertIn("expected_updated_at does not match", conflict["error"])
        failed, usage = client.call("recall", {"chat": CHAT, "query": "pool", "limit": 0})
        self.assertTrue(failed)
        self.assertIn("limit", usage["error"])
        # A broken line is answered and the server goes on; closing the input ends it cleanly.
        client.send("{broken")
        self.assertEqual(client.read()["error"]["code"], -32700)
        self.assertIn("tools", client.ask("tools/list")["result"])
        self.assertEqual(client.close(), (0, b"", b""))

    def test_a_client_of_an_earlier_revision_works_after_its_handshake(self):
        for version in ("2025-11-25", "2024-11-05"):
            with self.subTest(version=version):
                client = Client(self, self.environment)
                self.assertEqual(client.ask("tools/list", meta=None)["error"]["code"], -32602)
                started = client.ask("initialize", {"protocolVersion": version, "capabilities": {},
                                                    "clientInfo": {"name": "test", "version": "0"}}, meta=None)["result"]
                self.assertEqual((started["protocolVersion"], started["serverInfo"]["name"]), (version, "richi"))
                client.send('{"jsonrpc":"2.0","method":"notifications/initialized"}')
                self.assertEqual(client.ask("tools/list", meta=None)["result"]["tools"],
                                 mcp_tools.definitions(memory.parser(), "core"))
                self.assertEqual(client.call("entry_put", {"chat": CHAT, "records": [dict(RECORD, expected_updated_at=None,
                                                                                         id="decision:" + version)]}, meta=None)[0], False)
                self.assertEqual(client.call("entry_get", {"chat": CHAT, "id": "decision:" + version}, meta=None)[1]["title"],
                                 RECORD["title"])
                self.assertEqual(client.close(), (0, b"", b""))

    def test_one_server_keeps_two_chats_in_their_own_workspaces(self):
        self.launcher(["workspace", "create", "beta"])
        self.launcher(["-w", "beta", "init"])
        self.launcher(["--chat=" + OTHER, "chat", "bind", "beta"])
        # Start the server as a client configured with stray selections would.
        environment = dict(self.environment, RICHI_WORKSPACE="beta", RICHI_CHAT_ID=OTHER, CODEX_THREAD_ID="thread")
        client = Client(self, environment)
        self.assertEqual(client.call("entry_put", {"chat": CHAT, "records": [RECORD]})[0], False)
        self.assertEqual(client.call("entry_get", {"chat": CHAT, "id": RECORD["id"]})[1]["id"], RECORD["id"])
        failed, absent = client.call("entry_get", {"chat": OTHER, "id": RECORD["id"]})
        self.assertTrue(failed)
        self.assertEqual((set(absent) - {"code"}, "title" in absent), ({"error"}, False))
        self.assertEqual(client.call("recall", {"chat": OTHER, "query": "pool sized from measured peak"})[1]["results"], [])
        failed, unbound = client.call("recall", {"chat": "claude:never-bound", "query": "pool"})
        self.assertEqual((failed, unbound["code"]), (True, "chat_workspace_required"))
        self.assertNotIn("results", unbound)
        self.assertIn("beta", unbound["workspaces"])

    def test_a_client_without_a_shell_goes_from_no_identity_to_stored_knowledge(self):
        self.launcher(["workspace", "create", "gamma"])  # the human's step: workspaces are theirs to create
        client = Client(self, self.environment)
        failed, opened = client.call(mcp_tools.OPEN, {})
        chat = opened["chat"]
        self.assertEqual((failed, opened["source"]), (False, "new"))
        failed, status = client.call(mcp_tools.STATUS, {"chat": chat})
        self.assertEqual((failed, status["bound"], status["workspace"]), (False, False, None))
        self.assertEqual(status["workspaces"], ["default", "gamma"])
        failed, refused = client.call("recall", {"chat": chat, "query": "pool"})
        self.assertEqual((failed, refused["code"]), (True, "chat_workspace_required"))
        failed, bound = client.call(mcp_tools.BIND, {"chat": chat, "workspace": "gamma"})
        self.assertEqual((failed, bound["bound"], bound["workspace"]), (False, True, "gamma"))
        # Another choice needs the human's explicit replace.
        failed, rebind = client.call(mcp_tools.BIND, {"chat": chat, "current": True})
        self.assertEqual((failed, rebind["code"]), (True, "chat_rebind_required"))
        failed, missing = client.call("recall", {"chat": chat, "query": "pool"})
        self.assertTrue(failed)
        self.assertIn("init", missing["error"])
        self.assertFalse(client.call("init", {"chat": chat})[0])
        failed, project = client.call("project_check", {"chat": chat, "path": self.environment["HOME"]})
        self.assertEqual((failed, project["workspace"]), (False, "gamma"))
        self.assertEqual(client.call("entry_put", {"chat": chat, "records": [RECORD]})[1],
                         {"results": [{"id": RECORD["id"], "status": "created"}]})
        self.assertEqual(client.call("entry_get", {"chat": chat, "id": RECORD["id"]})[1]["title"], RECORD["title"])
        # The record lives in gamma only: the chat bound to default does not see it.
        self.assertTrue(client.call("entry_get", {"chat": CHAT, "id": RECORD["id"]})[0])
        self.assertEqual(client.call(mcp_tools.STATUS, {"chat": chat})[1]["workspace"], "gamma")

    def test_the_all_profile_lists_one_tool_per_command(self):
        client = Client(self, self.environment, ["--tools", "all"])
        names = [tool["name"] for tool in client.ask("tools/list")["result"]["tools"]]
        self.assertEqual(len(names), len(mcp_tools.definitions(memory.parser(), "all")))
        self.assertNotIn(mcp_tools.RUN_READ, names)
        self.assertEqual(client.call("check", {"chat": CHAT})[1]["status"], "ok")
        self.assertEqual(client.ask("tools/call", {"name": mcp_tools.RUN_READ, "arguments": {"chat": CHAT, "command": "check"}})
                         ["error"]["code"], -32602)

    def test_the_server_takes_no_project_path(self):
        result = subprocess.run(bootstrap_command(["--project-path", self.environment["HOME"], "mcp"]),
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, env=self.environment, timeout=60)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("belongs to a tool call", json.loads(result.stderr)["error"])


# A server that takes itself for a release of a managed installation: it sees a
# newer release once the named file exists, and that release's entry point
# starts the real server with the other tool profile, so the switch shows.
FOLLOWER = r"""
import os, sys
from types import SimpleNamespace
from richi import mcp_server
from richi_launcher import maps
from richi_launcher.runtime import bootstrap_command
os.environ[maps.ROOT_ENV], os.environ[maps.GENERATION_ENV] = "/synthetic/installation", "old-release"
mcp_server.IDLE_CHECK = 0.05
maps.running_current_generation = lambda: not os.path.exists(sys.argv[1])
maps.handoff_problem = lambda schema=None: None
maps.stable_command = lambda argv: (bootstrap_command(["mcp", "--tools", "all"]), True)
sys.exit(mcp_server.main(SimpleNamespace(tools="core")))
"""


class FollowingTests(unittest.TestCase):
    """After an update the server re-executes on the current release and keeps its client."""

    def test_input_is_handed_over_in_whole_lines_and_a_pause_never_holds_any(self):
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        pauses = []
        produced = mcp_server.lines(reader, lambda: pauses.append(None), interval=0.01)
        os.write(writer, b'{"a":1}\n{"b":')
        self.assertEqual(next(produced), b'{"a":1}')
        self.assertEqual(len(pauses), 1)  # before the first read, with nothing held
        os.write(writer, b'2}\n\n{"c":3}\n')
        self.assertEqual([next(produced) for _ in range(3)], [b'{"b":2}', b"", b'{"c":3}'])
        # Half a line was held meanwhile: the process could not have been replaced.
        self.assertEqual(len(pauses), 1)
        os.write(writer, b'{"unfinished":4}')
        os.close(writer)
        self.assertEqual(list(produced), [b'{"unfinished":4}'])
        self.assertEqual(len(pauses), 2)

    def followed(self, environment, *, current=False, problem=None, entry=(["/stable/python", "entry", "mcp"], True),
                 handshake=None, failing=False):
        server = mcp_server.Server(memory.parser(), launch=None, handshake=handshake)
        running = mock.Mock(side_effect=RuntimeError("unknown launcher")) if failing else mock.Mock(return_value=current)
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(maps, "running_current_generation", running), \
                mock.patch.object(maps, "handoff_problem", return_value=problem), \
                mock.patch.object(maps, "stable_command", return_value=entry) as stable, \
                mock.patch("richi_launcher.runtime.lease_fds", return_value=(9,)), \
                mock.patch.object(os, "set_inheritable") as inheritable, mock.patch.object(os, "execve") as execve:
            mcp_server.follow(server)
        return execve, stable, inheritable

    def test_the_server_stays_unless_a_complete_newer_release_has_an_entry_point(self):
        managed = {maps.ROOT_ENV: "/installation", maps.GENERATION_ENV: "old", "HOME": "/home/synthetic"}
        for label, environment, options in (
                ("not a managed installation", {"HOME": "/home/synthetic"}, {}),
                ("already the current release", managed, {"current": True}),
                ("the release is incomplete", managed, {"problem": "release 9.9.9 is incomplete"}),
                ("no entry point", managed, {"entry": (["python", "launcher"], False)}),
                ("a launcher it does not understand", managed, {"failing": True})):
            with self.subTest(label):
                execve, _stable, inheritable = self.followed(environment, **options)
                execve.assert_not_called()
                inheritable.assert_not_called()

    def test_the_server_re_executes_through_the_entry_point_with_a_clean_selection(self):
        environment = {maps.ROOT_ENV: "/installation", maps.GENERATION_ENV: "old", maps.LEASE_ENV: "9",
                       "HOME": "/home/synthetic", "PATH": "/usr/bin", "RICHI_CONFIG": "/pinned/config.json",
                       "RICHI_DATA_DIR": "/pinned", "RICHI_WORKSPACE": "default", "RICHI_ACTIVE_RUNTIME": "{}",
                       mcp_server.HANDSHAKE_ENV: "2024-11-05"}
        execve, stable, inheritable = self.followed(environment)
        stable.assert_called_once_with(["mcp", "--tools", "core"])
        # The entry point selects and leases the release; this one's lease ends with its code.
        inheritable.assert_called_once_with(9, False)
        execve.assert_called_once_with("/stable/python", ["/stable/python", "entry", "mcp"],
                                       {"HOME": "/home/synthetic", "PATH": "/usr/bin"})
        # A client of an earlier revision sends its handshake once: the new process is told the revision.
        execve, _stable, _inheritable = self.followed(environment, handshake="2025-06-18")
        self.assertEqual(execve.call_args.args[2], {"HOME": "/home/synthetic", "PATH": "/usr/bin",
                                                    mcp_server.HANDSHAKE_ENV: "2025-06-18"})
        self.assertEqual(mcp_server.Server(memory.parser(), launch=None, handshake="2025-06-18").handshake, "2025-06-18")
        self.assertIsNone(mcp_server.Server(memory.parser(), launch=None, handshake="1999-01-01").handshake)

    def test_a_client_keeps_its_connection_across_the_re_execution(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-mcp-follow-")
        self.addCleanup(temporary.cleanup)
        environment = cli_environment(temporary.name)
        for argv in (["init"], ["--chat=" + CHAT, "chat", "bind", "--current"]):
            subprocess.run(bootstrap_command(argv), check=True, capture_output=True, env=environment, timeout=60)
        changed = os.path.join(temporary.name, "installation-changed")
        client = Client(self, environment, command=[sys.executable, "-c", FOLLOWER, changed])
        client.ask("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}, meta=None)
        client.send('{"jsonrpc":"2.0","method":"notifications/initialized"}')
        core, every = (len(mcp_tools.definitions(memory.parser(), profile)) for profile in ("core", "all"))
        self.assertEqual(len(client.ask("tools/list", meta=None)["result"]["tools"]), core)
        open(changed, "w").close()
        # Every request is answered, by the old process or by the new one; none is lost in between.
        for _ in range(200):
            listed = len(client.ask("tools/list", meta=None)["result"]["tools"])
            if listed == every:
                break
            self.assertEqual(listed, core)
            time.sleep(0.05)
        self.assertEqual(listed, every)
        self.assertIsNone(client.process.poll())
        # The handshake was not repeated, and calls run as before.
        self.assertEqual(client.call(mcp_tools.STATUS, {"chat": CHAT}, meta=None), (False, mock.ANY))
        self.assertEqual(client.call("check", {"chat": CHAT}, meta=None)[1]["status"], "ok")
        self.assertEqual(client.close(), (0, b"", b""))


if __name__ == "__main__":
    unittest.main()
