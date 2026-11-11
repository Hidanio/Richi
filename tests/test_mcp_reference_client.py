"""The official MCP SDK's client, an independent implementation, against `richi mcp`.

The server is written against the specification by hand, so its own tests
cannot show what it misread. This module runs only where the SDK is installed
(Python 3.10+); Richi itself never depends on it.
"""
import asyncio
import json
import subprocess
import tempfile
import unittest

from richi import __version__, mcp_tools, memory
from richi_launcher.runtime import bootstrap_command

from cli_environment import cli_environment

try:
    from mcp import Client, StdioServerParameters
except ImportError:
    Client = None

CHAT = "claude:0af4cd88-675d-4258-a011-fdab17bf6333"
RECORD = {"id": "decision:atlas-pool-sizing", "kind": "decision", "knowledge_state": "confirmed",
          "title": "Atlas: размер пула по измеренной нагрузке",
          "summary": "The pool is sized from the measured peak of concurrent requests.",
          "sources": [{"reference": "https://example.test/atlas/pull/42"}], "expected_updated_at": None}


@unittest.skipIf(Client is None, "the MCP SDK is not installed")
class ReferenceClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-mcp-reference-")
        self.addCleanup(temporary.cleanup)
        self.environment = cli_environment(temporary.name)
        for argv in (["init"], ["--chat=" + CHAT, "chat", "bind", "--current"]):
            subprocess.run(bootstrap_command(argv), check=True, capture_output=True, env=self.environment, timeout=60)

    def converse(self, mode, profile="core"):
        command = bootstrap_command(["mcp", "--tools", profile])
        server = StdioServerParameters(command=command[0], args=command[1:], env=self.environment)

        async def conversation():
            async with Client(server, mode=mode, read_timeout_seconds=60) as client:
                tools = (await client.list_tools()).tools
                opened = await client.call_tool(mcp_tools.OPEN, {})
                self.assertRegex(json.loads(opened.content[0].text)["chat"], r"\Amcp:[0-9a-f]{32}\Z")
                written = await client.call_tool("entry_put", {"chat": CHAT, "records": [RECORD]})
                read = await client.call_tool("entry_get", {"chat": CHAT, "id": RECORD["id"]})
                refused = await client.call_tool("recall", {"chat": "claude:never-bound", "query": "pool"})
                return (client.protocol_version, client.instructions, client.server_info, tools,
                        [(result.is_error, json.loads(result.content[0].text)) for result in (written, read, refused)])
        return asyncio.run(asyncio.wait_for(conversation(), 120))

    def check(self, mode, version):
        negotiated, instructions, server, tools, (written, read, refused) = self.converse(mode)
        self.assertEqual(negotiated, version)
        self.assertEqual(instructions, mcp_tools.INSTRUCTIONS)
        self.assertEqual((server.name, server.version), ("richi", __version__))
        self.assertEqual([tool.name for tool in tools][-3:], [mcp_tools.DESCRIBE, mcp_tools.RUN_READ, mcp_tools.RUN_WRITE])
        recall = next(tool for tool in tools if tool.name == "recall")
        self.assertEqual(recall.input_schema["required"], ["chat", "query"])
        self.assertIs(recall.annotations.read_only_hint, True)
        self.assertEqual(written, (False, {"results": [{"id": RECORD["id"], "status": "created"}]}))
        self.assertEqual((read[0], read[1]["title"]), (False, RECORD["title"]))
        self.assertEqual((refused[0], refused[1]["code"]), (True, "chat_workspace_required"))

    def test_the_client_probes_and_stays_on_the_stateless_revision(self):
        self.check("auto", "2026-07-28")

    def test_the_client_can_use_the_handshake_of_the_earlier_revisions(self):
        self.check("legacy", "2025-11-25")

    def test_every_definition_of_the_full_profile_is_accepted(self):
        tools = self.converse("auto", "all")[3]
        self.assertEqual([tool.name for tool in tools],
                         [tool["name"] for tool in mcp_tools.definitions(memory.parser(), "all")])


if __name__ == "__main__":
    unittest.main()
