"""MCP tool definitions stay one contract with the parser and the catalog.

Definitions and command lines are checked without a store; the last class runs
tool calls through the launcher against a synthetic home.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock

from richi import capabilities, mcp_tools, memory
from richi_launcher.runtime import bootstrap_command

from cli_environment import cli_environment

CHAT = "claude:0af4cd88-675d-4258-a011-fdab17bf6333"
NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")


def sample(schema, key):
    kind = schema["type"]
    if kind == "boolean":
        return True
    if kind == "integer":
        return schema.get("default") or schema.get("minimum", 1)
    if kind == "array":
        return [sample(schema["items"], key), sample(schema["items"], key)]
    if kind == "object":
        return {"id": "fact:example", "kind": "fact"}
    return schema["enum"][0] if "enum" in schema else "value of " + key


ROWS = mcp_tools.SELECTION + tuple(capabilities.COMMANDS)
SESSION = [mcp_tools.OPEN, mcp_tools.STATUS, mcp_tools.BIND, mcp_tools.GUIDE, "init", "project_check"]


def full_call(root, tool):
    """Every argument the tool accepts, one member per exclusive group."""
    identity = next(row[0] for row in ROWS if mcp_tools.tool_name(row[0]) == tool["name"])
    excluded = {mcp_tools._key(identity, action) for group in mcp_tools._parser(root, identity)._mutually_exclusive_groups
                for action in group._group_actions[1:]}
    return identity, {key: sample(schema, key) for key, schema in tool["inputSchema"]["properties"].items()
                      if key not in excluded}


class DefinitionTests(unittest.TestCase):
    def setUp(self):
        self.root = memory.parser()

    def test_every_cataloged_command_is_one_tool_in_catalog_order(self):
        listed = mcp_tools.definitions(self.root, "all")
        self.assertEqual(len({tool["name"] for tool in listed}), len(listed))
        # The chat tools, the guide and the selection tools come first in every profile.
        self.assertEqual([tool["name"] for tool in listed[:len(SESSION)]], SESSION)
        self.assertEqual([tool["name"] for tool in mcp_tools.definitions(self.root, "core")[:len(SESSION)]], SESSION)
        tools = listed[len(SESSION):]
        self.assertEqual([tool["name"] for tool in tools],
                         [mcp_tools.tool_name(row[0]) for row in capabilities.COMMANDS])
        for tool, row in zip(tools, capabilities.COMMANDS):
            with self.subTest(tool=tool["name"]):
                self.assertRegex(tool["name"], NAME)
                # Texts name arguments as a tool call does, not as command-line options.
                self.assertEqual(re.split(r" (?:Give|Guide:) ", tool["description"])[0],
                                 re.sub(r"--([a-z-]+)", lambda m: m.group(1).replace("-", "_"), row[3]))
                for key, item in tool["inputSchema"]["properties"].items():
                    for option in re.findall(r"--[a-z][a-z-]*", item.get("description", "")):
                        self.assertNotIn(option[2:].replace("-", "_"), tool["inputSchema"]["properties"], (key, option))
                self.assertEqual(tool["annotations"], {"readOnlyHint": row[2] == "read", "openWorldHint": False})
                schema = tool["inputSchema"]
                self.assertEqual((schema["type"], schema["additionalProperties"]), ("object", False))
                self.assertEqual(schema["required"][0], "chat")
                self.assertLessEqual(set(schema["required"]), set(schema["properties"]))
                self.assertEqual(len(set(schema["required"])), len(schema["required"]))

    def test_schemas_use_only_forms_that_clients_accept(self):
        for tool in mcp_tools.definitions(self.root, "all") + mcp_tools.definitions(self.root, "core"):
            with self.subTest(tool=tool["name"]):
                schema = tool["inputSchema"]
                # Clients reject combinators at the top level of an input schema.
                self.assertFalse({"oneOf", "anyOf", "allOf", "not", "$ref"} & set(schema))
                for key, item in schema["properties"].items():
                    self.assertIn(item["type"], {"string", "integer", "boolean", "array", "object"}, key)
                    if item["type"] == "array":
                        self.assertIn(item["items"]["type"], {"string", "integer", "object"}, key)
                self.assertEqual(json.loads(json.dumps(tool)), tool)

    def test_definitions_are_stable_between_calls(self):
        self.assertEqual(mcp_tools.definitions(self.root, "all"), mcp_tools.definitions(memory.parser(), "all"))
        self.assertEqual(mcp_tools.definitions(self.root), mcp_tools.definitions(self.root, "core"))
        with self.assertRaises(ValueError):
            mcp_tools.definitions(self.root, "everything")

    def test_core_profile_reaches_every_command_exactly_once(self):
        catalog = [mcp_tools.tool_name(row[0]) for row in capabilities.COMMANDS]
        self.assertLessEqual(set(mcp_tools.CORE), {row[0] for row in capabilities.COMMANDS})
        tools = mcp_tools.definitions(self.root, "core")
        direct = [tool["name"] for tool in tools[len(SESSION):-3]]
        describe, reads, writes = tools[-3:]
        self.assertEqual([tool["name"] for tool in tools[-3:]], [mcp_tools.DESCRIBE, mcp_tools.RUN_READ, mcp_tools.RUN_WRITE])
        generic = describe["inputSchema"]["properties"]["command"]["enum"]
        self.assertCountEqual(direct + generic, catalog)
        self.assertEqual(direct, [mcp_tools.tool_name(row[0]) for row in capabilities.COMMANDS
                                  if row[0] in mcp_tools.CORE])
        # A client asks the human before a tool that is not read-only, so the
        # further commands are split by the effect the catalog states.
        effect = {mcp_tools.tool_name(row[0]): row[2] for row in capabilities.COMMANDS}
        reading, writing = (tool["inputSchema"]["properties"]["command"]["enum"] for tool in (reads, writes))
        self.assertEqual([name for name in generic if effect[name] == "read"], reading)
        self.assertEqual([name for name in generic if effect[name] != "read"], writing)
        self.assertTrue(reading and writing)
        self.assertEqual([tool["annotations"]["readOnlyHint"] for tool in (describe, reads, writes)], [True, True, False])
        for name in generic:
            self.assertIn(name + ":", describe["description"])

    def test_records_replace_the_json_file_of_put_commands(self):
        puts = [row[0] for row in capabilities.COMMANDS
                if any(action.dest == "json" for action in mcp_tools._arguments(mcp_tools._parser(self.root, row[0])))]
        self.assertCountEqual(puts, mcp_tools.RECORDS)
        for identity in mcp_tools.RECORDS:
            schema = mcp_tools.definition(self.root, mcp_tools.tool_name(identity))["inputSchema"]
            self.assertNotIn("json", schema["properties"])
            self.assertIn("records", schema["required"])
            self.assertEqual(schema["properties"]["records"]["maxItems"], 1000)

    def test_workspace_wide_commands_take_no_project_path(self):
        for row in ROWS:
            properties = mcp_tools.definition(self.root, mcp_tools.tool_name(row[0]))["inputSchema"]["properties"]
            self.assertEqual("project_path" not in properties, row[0] in mcp_tools.NO_PROJECT_PATH, row[0])
        self.assertLessEqual(set(mcp_tools.NO_PROJECT_PATH), {row[0] for row in ROWS})
        with self.assertRaisesRegex(mcp_tools.ToolError, "Unknown arguments: project_path"):
            mcp_tools.invocation(self.root, "backup", {"chat": CHAT, "project_path": "/work/example"})

    def test_exclusive_options_are_stated_in_words(self):
        show = mcp_tools.definition(self.root, "sources_show")
        self.assertIn("Give exactly one of: ref, json_file.", show["description"])
        self.assertNotIn("ref", show["inputSchema"]["required"])
        self.assertIn("Give at most one of: target, worktree.", mcp_tools.definition(self.root, "brief")["description"])
        # A required group of one option is simply a required argument.
        self.assertIn("ref", mcp_tools.definition(self.root, "sources_commit")["inputSchema"]["required"])

    def test_stated_ranges_are_the_ranges_the_argument_types_check(self):
        checked = 0
        for row in capabilities.COMMANDS:
            for action in mcp_tools._arguments(mcp_tools._parser(self.root, row[0])):
                schema = mcp_tools._property(row[0], action)
                if schema["type"] != "integer" or action.type is int:
                    continue
                with self.subTest(command=row[0], argument=action.dest):
                    self.assertIn("minimum", schema)
                    self.assertEqual(action.type(str(schema["minimum"])), schema["minimum"])
                    with self.assertRaises((argparse.ArgumentTypeError, ValueError)):
                        action.type(str(schema["minimum"] - 1))
                    if "maximum" in schema:
                        self.assertEqual(action.type(str(schema["maximum"])), schema["maximum"])
                        with self.assertRaises((argparse.ArgumentTypeError, ValueError)):
                            action.type(str(schema["maximum"] + 1))
                    else:
                        self.assertEqual(action.type(str(10 ** 9)), 10 ** 9)
                    if "default" in schema:
                        self.assertEqual(action.type(str(schema["default"])), schema["default"])
                    checked += 1
        self.assertGreater(checked, 30)

    def test_argument_types_describe_themselves_or_are_refused(self):
        def bounded(value):
            return int(value)
        bounded.bounds = (2, 9)

        def unknown(value):
            return value
        parser = argparse.ArgumentParser()
        self.assertEqual(mcp_tools._scalar(parser.add_argument("--a", type=bounded)),
                         {"type": "integer", "minimum": 2, "maximum": 9})
        # A range the type states itself wins over the listed one of its name.
        bounded.__name__ = "limit"
        self.assertEqual(mcp_tools._scalar(parser.add_argument("--f", type=bounded)),
                         {"type": "integer", "minimum": 2, "maximum": 9})
        self.assertEqual(mcp_tools._scalar(parser.add_argument("--b", type=memory.positive_int)),
                         {"type": "integer", "minimum": 1})
        self.assertEqual(mcp_tools._scalar(parser.add_argument("--c", choices=("x", "y"))),
                         {"type": "string", "enum": ["x", "y"]})
        with self.assertRaisesRegex(ValueError, "Unclassified argument type 'unknown'"):
            mcp_tools._scalar(parser.add_argument("--d", type=unknown))
        with self.assertRaisesRegex(ValueError, "Unsupported argument form"):
            mcp_tools._property("recall", parser.add_argument("--e", nargs="+"))

    def test_instructions_fit_client_limits_and_lead_with_the_isolation_rules(self):
        self.assertLessEqual(len(mcp_tools.INSTRUCTIONS), 4096)
        # A client may show only the beginning: both rules must survive the cut.
        lead = mcp_tools.INSTRUCTIONS[:512]
        self.assertIn("Pass `chat` on every call", lead)
        self.assertIn("chat_workspace_required", lead)
        self.assertIn("never choose one yourself or infer it from the working directory.", lead)


class InvocationTests(unittest.TestCase):
    def setUp(self):
        self.root = memory.parser()

    def test_every_tool_call_parses_to_the_values_given(self):
        for row in ROWS:
            tool = mcp_tools.definition(self.root, mcp_tools.tool_name(row[0]))
            identity, arguments = full_call(self.root, tool)
            with self.subTest(tool=tool["name"]):
                call = mcp_tools.invocation(self.root, tool["name"], arguments)
                parsed = self.root.parse_args(call.argv)
                self.assertEqual((parsed.chat, parsed.compact_json), (arguments["chat"], True))
                self.assertEqual([parsed.command] + ([parsed.action] if " " in identity else []), identity.split())
                for action in mcp_tools._arguments(mcp_tools._parser(self.root, identity)):
                    key = mcp_tools._key(identity, action)
                    if key == "records":
                        self.assertEqual(parsed.json, "-")
                        self.assertEqual(json.loads(call.stdin), arguments["records"])
                    elif key in arguments:
                        value = getattr(parsed, action.dest)
                        self.assertEqual(str(value) if isinstance(value, Path) else value, arguments[key], key)
                    else:
                        self.assertIn(getattr(parsed, action.dest), (action.default, None, False), key)
                if "project_path" in arguments:
                    self.assertEqual(str(parsed.project_path), arguments["project_path"])
                if "records" not in arguments:
                    self.assertIsNone(call.stdin)

    def test_only_required_arguments_leave_parser_defaults(self):
        call = mcp_tools.invocation(self.root, "recall", {"chat": CHAT, "query": "pool sizing"})
        self.assertEqual(call, mcp_tools.Invocation(["--chat=" + CHAT, "--compact-json", "recall", "--", "pool sizing"], None))
        parsed = self.root.parse_args(call.argv)
        self.assertEqual((parsed.limit, parsed.max_chars, parsed.include_hypotheses, parsed.project_path),
                         (8, 16000, False, None))
        self.assertEqual(mcp_tools.invocation(self.root, "recall", {"chat": CHAT, "query": "q", "explain": False}).argv,
                         ["--chat=" + CHAT, "--compact-json", "recall", "--", "q"])

    def test_values_that_look_like_options_stay_values(self):
        call = mcp_tools.invocation(self.root, "recall", {
            "chat": CHAT, "project_path": "--db=/tmp/other", "query": "--limit=1", "where": "-x", "project": "--explain"})
        parsed = self.root.parse_args(call.argv)
        self.assertEqual((parsed.query, parsed.where, parsed.project, parsed.limit, parsed.explain, parsed.db),
                         ("--limit=1", "-x", "--explain", 8, False, None))
        self.assertEqual(str(parsed.project_path), "--db=/tmp/other")
        get = self.root.parse_args(mcp_tools.invocation(self.root, "entry_get", {"chat": CHAT, "id": "--history"}).argv)
        self.assertEqual((get.id, get.history), ("--history", False))
        fetch = mcp_tools.invocation(self.root, "fetch", {"chat": CHAT, "refs": ["entry:a", "--offset=5"]})
        self.assertEqual(self.root.parse_args(fetch.argv).refs, ["entry:a", "--offset=5"])
        # A chat value is one argument whatever it contains; the launcher judges its form.
        spaced = mcp_tools.invocation(self.root, "check", {"chat": "x --db /tmp/other"})
        self.assertEqual(spaced.argv, ["--chat=x --db /tmp/other", "--compact-json", "check"])

    def test_calls_the_caller_can_correct_are_tool_errors(self):
        cases = (
            ("recall", [], "arguments must be an object"),
            ("recall", {"chat": CHAT}, "Missing arguments: query"),
            ("recall", {"query": "q"}, "Missing arguments: chat"),
            ("recall", {"chat": "  ", "query": "q"}, "chat must be a nonempty string"),
            ("recall", {"chat": CHAT, "query": "q", "db": "/tmp/other"}, "Unknown arguments: db"),
            ("recall", {"chat": CHAT, "query": "q", "limit": "8"}, "limit must be an integer"),
            ("recall", {"chat": CHAT, "query": "q", "limit": True}, "limit must be an integer"),
            ("recall", {"chat": CHAT, "query": "q", "explain": "yes"}, "explain must be a boolean"),
            ("recall", {"chat": CHAT, "query": 7}, "query must be a string"),
            ("fetch", {"chat": CHAT, "refs": "entry:a"}, "refs must be a nonempty array"),
            ("fetch", {"chat": CHAT, "refs": []}, "refs must be a nonempty array"),
            ("fetch", {"chat": CHAT, "refs": ["entry:a", 5]}, "refs must be a string"),
            ("entry_put", {"chat": CHAT, "records": {"id": "fact:a"}}, "records must be a nonempty array"),
            ("entry_put", {"chat": CHAT, "records": ["fact:a"]}, "records must be an object"),
            ("entry_put", {"chat": CHAT, "json": "/tmp/records.json"}, "Unknown arguments: json"),
            ("no_such_tool", {"chat": CHAT}, "Unknown command: no_such_tool"),
        )
        for name, arguments, message in cases:
            with self.subTest(name=name, message=message):
                with self.assertRaisesRegex(mcp_tools.ToolError, re.escape(message)):
                    mcp_tools.invocation(self.root, name, arguments)

    def test_ranges_and_choices_are_left_to_the_parser(self):
        call = mcp_tools.invocation(self.root, "recall", {"chat": CHAT, "query": "q", "limit": 0})
        with self.assertRaisesRegex(memory.MemoryError, "limit"):
            self.root.parse_args(call.argv)
        both = mcp_tools.invocation(self.root, "brief", {"chat": CHAT, "query": "q", "target": "HEAD", "worktree": True})
        with self.assertRaisesRegex(memory.MemoryError, "not allowed with"):
            self.root.parse_args(both.argv)

    def test_records_travel_on_standard_input_unchanged(self):
        records = [{"id": "fact:пример", "kind": "fact", "title": "Заголовок", "summary": "line one\nline two"}]
        call = mcp_tools.invocation(self.root, "entry_put", {"chat": CHAT, "project_path": "/work/example", "records": records})
        self.assertEqual(call.argv, ["--chat=" + CHAT, "--compact-json", "--project-path=/work/example",
                                     "entry", "put", "--json=-"])
        self.assertEqual(json.loads(call.stdin), records)
        self.assertIn("пример", call.stdin)

    def test_a_generic_run_is_the_named_command_with_the_same_selectors(self):
        direct = mcp_tools.invocation(self.root, "tools_get", {"chat": CHAT, "project_path": "/work/example", "id": "tool:git"})
        generic = mcp_tools.invocation(self.root, mcp_tools.RUN_READ, {
            "chat": CHAT, "project_path": "/work/example", "command": "tools_get", "arguments": {"id": "tool:git"}})
        self.assertEqual(generic, direct)
        self.assertEqual(mcp_tools.invocation(self.root, mcp_tools.RUN_READ, {"chat": CHAT, "command": "check"}).argv,
                         ["--chat=" + CHAT, "--compact-json", "check"])
        self.assertEqual(mcp_tools.invocation(self.root, mcp_tools.RUN_WRITE, {"chat": CHAT, "command": "search_index_build"}).argv,
                         ["--chat=" + CHAT, "--compact-json", "search-index", "build"])
        # Neither runner reaches the other's commands: the read-only mark stays true.
        for runner, command in ((mcp_tools.RUN_READ, "backup"), (mcp_tools.RUN_READ, "entity_put"),
                                (mcp_tools.RUN_READ, "storage_optimize"), (mcp_tools.RUN_WRITE, "check")):
            with self.subTest(runner=runner, command=command):
                with self.assertRaisesRegex(mcp_tools.ToolError, "command must be one of the commands " + runner):
                    mcp_tools.invocation(self.root, runner, {"chat": CHAT, "command": command})
        with self.assertRaisesRegex(mcp_tools.ToolError, "Unknown arguments: project_path"):
            mcp_tools.invocation(self.root, mcp_tools.RUN_WRITE, {"chat": CHAT, "command": "backup", "project_path": "/work/example"})
        cases = (
            ({"chat": CHAT, "command": "recall", "arguments": {"query": "q"}}, "command must be one of"),
            ({"chat": CHAT, "command": "no_such", "arguments": {}}, "command must be one of"),
            ({"chat": CHAT, "command": "tools_get", "arguments": {"id": "x", "chat": "other"}}, "without chat and project_path"),
            ({"chat": CHAT, "command": "tools_get", "arguments": ["x"]}, "arguments must be an object"),
            ({"chat": CHAT, "command": "tools_get", "id": "x"}, "Unknown arguments: id"),
            ({"command": "tools_get", "arguments": {"id": "x"}}, "Missing arguments: chat"),
        )
        for arguments, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(mcp_tools.ToolError, re.escape(message)):
                    mcp_tools.invocation(self.root, mcp_tools.RUN_READ, arguments)

    def test_chat_tools_are_the_launchers_chat_commands(self):
        status = mcp_tools.call(self.root, mcp_tools.STATUS, {"chat": CHAT})
        self.assertEqual(status, mcp_tools.Invocation(["--chat=" + CHAT, "--compact-json", "chat", "current"], None))

        def bind(**arguments):
            return mcp_tools.call(self.root, mcp_tools.BIND, dict(arguments, chat=CHAT)).argv[2:]
        self.assertEqual(bind(workspace="atlas"), ["chat", "bind", "--", "atlas"])
        self.assertEqual(bind(current=True), ["chat", "bind", "--current"])
        self.assertEqual(bind(workspace="atlas", replace=True, current=False), ["chat", "bind", "--replace", "--", "atlas"])
        # A name that looks like an option stays a name; the launcher judges it.
        self.assertEqual(bind(workspace="--current"), ["chat", "bind", "--", "--current"])
        for arguments, message in (({"chat": CHAT}, "Give exactly one of: workspace, current"),
                                   ({"chat": CHAT, "current": False}, "Give exactly one of: workspace, current"),
                                   ({"chat": CHAT, "workspace": "atlas", "current": True}, "Give exactly one of"),
                                   ({"chat": CHAT, "workspace": 5}, "workspace must be a string"),
                                   ({"chat": CHAT, "workspace": "atlas", "db": "/tmp/other"}, "Unknown arguments: db"),
                                   ({"workspace": "atlas"}, "Missing arguments: chat")):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(mcp_tools.ToolError, re.escape(message)):
                    mcp_tools.call(self.root, mcp_tools.BIND, arguments)
        with self.assertRaisesRegex(mcp_tools.ToolError, "Unknown arguments: project_path"):
            mcp_tools.call(self.root, mcp_tools.STATUS, {"chat": CHAT, "project_path": "/work/atlas"})

    def test_a_new_chat_identity_is_unique_and_valid(self):
        first, second = mcp_tools.call(self.root, mcp_tools.OPEN, {}), mcp_tools.call(self.root, mcp_tools.OPEN, None)
        self.assertNotEqual(first["chat"], second["chat"])
        # The form the launcher accepts as a chat ID.
        self.assertRegex(first["chat"], r"\Amcp:[0-9a-f]{32}\Z")
        self.assertEqual((first["source"], set(first)), ("new", {"chat", "source", "next"}))
        with self.assertRaisesRegex(mcp_tools.ToolError, "takes no arguments"):
            mcp_tools.call(self.root, mcp_tools.OPEN, {"chat": CHAT})

    def test_the_clients_own_conversation_identity_is_preferred(self):
        thread = "01a1ca8a-1201-78d1-b2cd-3a0ae11d5dcb"
        # Codex states its thread with every tool call; its terminal gives the command line the same ID.
        meta = {"progressToken": 3, "threadId": thread, "x-codex-turn-metadata": {"thread_id": thread}}
        for _ in range(2):
            self.assertEqual(mcp_tools.call(self.root, mcp_tools.OPEN, {}, meta)["chat"], thread)
        self.assertEqual(mcp_tools.call(self.root, mcp_tools.OPEN, {}, meta)["source"], "client")
        # What the launcher would not accept as a chat ID is not used.
        for value in ("", "with space", "../escape", "a" * 201, 7, None, ["x"]):
            with self.subTest(value=value):
                answer = mcp_tools.call(self.root, mcp_tools.OPEN, {}, {"threadId": value})
                self.assertEqual((answer["source"], answer["chat"][:4]), ("new", "mcp:"))
        self.assertEqual(mcp_tools.call(self.root, mcp_tools.OPEN, {}, {"progressToken": 1})["source"], "new")
        # Only the open tool reads it: another call still needs its chat argument.
        with self.assertRaisesRegex(mcp_tools.ToolError, "Missing arguments: chat"):
            mcp_tools.call(self.root, "recall", {"query": "pool"}, meta)

    def test_an_entry_is_described_by_the_fields_and_choices_the_store_checks(self):
        entry = mcp_tools.definition(self.root, "entry_put")["inputSchema"]["properties"]["records"]["items"]
        self.assertEqual(set(entry["properties"]), set(memory.ENTRY_FIELDS) - {"created_at", "updated_at"}
                         | {"project_ids", "event_id", "expected_updated_at"})
        self.assertEqual((entry["required"], entry["additionalProperties"]), (["id", "kind", "title", "summary"], False))
        self.assertEqual(entry["properties"]["kind"]["enum"], sorted(memory.KINDS))
        self.assertEqual(entry["properties"]["knowledge_state"]["enum"], sorted(memory.KNOWLEDGE))
        self.assertEqual(entry["properties"]["work_state"]["enum"], sorted(memory.STATES) + [None])
        source = entry["properties"]["sources"]["items"]
        # The fields memory.sources accepts; an unknown one is refused there.
        every = {"reference": "https://example.test/a", "label": "x", "type": "page", "observed_at": "2026-11-01",
                 "revision": "abc", "locator": "p. 1", "sha256": "0" * 64}
        self.assertEqual(set(source["properties"]) - {"git"}, set(every))
        self.assertEqual(memory.sources([every]), [every])
        with self.assertRaises(memory.MemoryError):
            memory.sources([dict(every, extra="x")])
        # The other record kinds stay open objects until their fields are described too.
        self.assertEqual(mcp_tools.definition(self.root, "entity_put")["inputSchema"]["properties"]["records"]["items"],
                         {"type": "object"})

    def test_selection_commands_are_tools_of_their_own(self):
        self.assertEqual(mcp_tools.call(self.root, "init", {"chat": CHAT}).argv, ["--chat=" + CHAT, "--compact-json", "init"])
        check = mcp_tools.call(self.root, "project_check", {"chat": CHAT, "path": "/work/atlas", "project_id": "atlas"})
        self.assertEqual(check.argv, ["--chat=" + CHAT, "--compact-json", "project", "check", "--id=atlas", "--", "/work/atlas"])
        # They are listed directly, so the generic tool does not reach them.
        with self.assertRaisesRegex(mcp_tools.ToolError, "command must be one of"):
            mcp_tools.call(self.root, mcp_tools.RUN_WRITE, {"chat": CHAT, "command": "init"})

    def test_describe_returns_the_definition_of_a_further_command(self):
        answer = mcp_tools.describe(self.root, {"command": "sources_impact"})
        tool = mcp_tools.definition(self.root, "sources_impact")
        # In the runner's terms: the command's own arguments, and which tool runs it.
        self.assertEqual((answer["command"], answer["description"], answer["run_with"], answer["takes_project_path"]),
                         ("sources_impact", tool["description"], mcp_tools.RUN_READ, True))
        self.assertEqual(answer["arguments"]["required"], ["project", "base"])
        self.assertEqual(set(answer["arguments"]["properties"]), set(tool["inputSchema"]["properties"]) - {"chat", "project_path"})
        arguments = {"project": "atlas", "base": "main"}
        self.assertEqual(mcp_tools.invocation(self.root, answer["run_with"], {"chat": CHAT, "command": "sources_impact",
                                                                             "arguments": arguments}),
                         mcp_tools.invocation(self.root, "sources_impact", dict(arguments, chat=CHAT)))
        self.assertEqual([mcp_tools.describe(self.root, {"command": name})["run_with"]
                          for name in ("backup", "entity_put", "recall", "init")],
                         [mcp_tools.RUN_WRITE, mcp_tools.RUN_WRITE, "recall", "init"])
        self.assertIs(mcp_tools.describe(self.root, {"command": "backup"})["takes_project_path"], False)
        for arguments in ({}, {"command": "no_such"}, {"command": "check", "chat": CHAT}, "check"):
            with self.assertRaises(mcp_tools.ToolError):
                mcp_tools.describe(self.root, arguments)


class GuideTests(unittest.TestCase):
    """The agent skill is the one manual: a client without it reads the same texts."""

    def setUp(self):
        self.root = memory.parser()
        self.skill = mcp_tools._skill()

    def test_the_guide_tool_lists_the_references_of_the_skill(self):
        for profile in ("core", "all"):
            tool = mcp_tools.definitions(self.root, profile)[SESSION.index(mcp_tools.GUIDE)]
            self.assertEqual((tool["name"], tool["annotations"]["readOnlyHint"], tool["inputSchema"]["required"]),
                             (mcp_tools.GUIDE, True, []))
            # No store is read, so no chat is asked for.
            self.assertEqual(set(tool["inputSchema"]["properties"]), {"name"})
        names = tool["inputSchema"]["properties"]["name"]["enum"]
        self.assertEqual(names, sorted(path.name for path in (self.skill / "references").iterdir()
                                       if not path.name.endswith(".ru.md")))
        # Every procedure the working rules link is asked for by the name it is linked with.
        linked = set(re.findall(r"\(references/([A-Z_]+\.md)\)", (self.skill / "SKILL.md").read_text(encoding="utf-8")))
        self.assertGreater(len(linked), 10)
        self.assertLessEqual(linked, set(names))
        self.assertIn(mcp_tools.GUIDE, mcp_tools.INSTRUCTIONS)

    def test_every_cataloged_command_names_a_guide_that_can_be_read(self):
        names = mcp_tools.guides()
        for row in capabilities.COMMANDS:
            with self.subTest(command=row[0]):
                self.assertIn(row[4], names)
                tool = mcp_tools.definition(self.root, mcp_tools.tool_name(row[0]))
                self.assertTrue(tool["description"].endswith(" Guide: %s." % row[4]), tool["description"])
                self.assertEqual(mcp_tools.describe(self.root, {"command": tool["name"]})["guide"], row[4])
        # The selection commands have no procedure of their own: the working rules cover them.
        for row in mcp_tools.SELECTION:
            name = mcp_tools.tool_name(row[0])
            self.assertNotIn("Guide:", mcp_tools.definition(self.root, name)["description"])
            self.assertNotIn("guide", mcp_tools.describe(self.root, {"command": name}))

    def test_a_guide_is_the_text_of_the_skill_after_the_reading_note(self):
        manual = mcp_tools.call(self.root, mcp_tools.GUIDE, {})
        self.assertTrue(manual.startswith(mcp_tools.NOTE + "# Project memory\n"), manual[:700])
        # The header that addresses an agent's skill loader is left out; the rest is whole.
        self.assertTrue((self.skill / "SKILL.md").read_text(encoding="utf-8").endswith(manual[len(mcp_tools.NOTE):]))
        self.assertNotIn("name: project-memory", manual)
        self.assertIn("## Save meaningful outcomes within authorization", manual)
        for name in ("ENTRY_WRITE.md", "RECALL.md"):
            self.assertEqual(mcp_tools.call(self.root, mcp_tools.GUIDE, {"name": name}),
                             mcp_tools.NOTE + (self.skill / "references" / name).read_text(encoding="utf-8"))
        # The note explains the texts with tools and arguments that exist.
        tools = {tool["name"]: tool["inputSchema"]["properties"] for tool in mcp_tools.definitions(self.root, "core")}
        for name in ("entry_put", mcp_tools.DESCRIBE, mcp_tools.STATUS, mcp_tools.BIND, mcp_tools.GUIDE):
            self.assertIn(name, tools)
            self.assertIn(name, mcp_tools.NOTE)
        self.assertIn("max_chars", tools["recall"])
        self.assertIn("records", tools["entry_put"])
        self.assertIn("RECALL.md", mcp_tools.guides())

    def test_help_pages_and_the_guide_tool_read_the_same_files(self):
        # Two places know where a release keeps the skill: the launcher's help
        # pages name the local copy of a guide, and this tool reads it.
        try:
            from richi_launcher.help_format import local_guide
        except ImportError:
            self.skipTest("this launcher has no help pages that name local guide copies")
        with mock.patch.dict(os.environ):
            os.environ.pop("RICHI_ACTIVE_RUNTIME", None)  # as the launcher's own pages see a checkout or a release
            for name in mcp_tools.guides():
                self.assertEqual(local_guide(name), self.skill / "references" / name, name)

    def test_only_the_listed_guides_are_read(self):
        for arguments in ({"name": "SKILL.md"}, {"name": "../SKILL.md"}, {"name": "ENTRY_WRITE.ru.md"},
                          {"name": "entry_write.md"}, {"name": 3}, {"chat": CHAT}, "RECALL.md"):
            with self.subTest(arguments=arguments):
                with self.assertRaises(mcp_tools.ToolError):
                    mcp_tools.call(self.root, mcp_tools.GUIDE, arguments)

    def test_guides_come_from_the_checkout_or_from_the_copy_a_release_carries(self):
        with tempfile.TemporaryDirectory(prefix="richi-mcp-guides-") as temporary:
            base = Path(temporary).resolve()
            for layout, module, skill in (
                    ("checkout", "checkout/src/richi/mcp_tools.py", "checkout/skills/project-memory"),
                    ("release", "site/richi/mcp_tools.py", "site/richi_launcher/agent_skill")):
                with self.subTest(layout):
                    (base / skill / "references").mkdir(parents=True)
                    (base / skill / "SKILL.md").write_text("---\nname: project-memory\n---\n\n# Rules of the %s\n" % layout,
                                                           encoding="utf-8")
                    (base / skill / "references" / "ONE.md").write_text("One.\n", encoding="utf-8")
                    (base / skill / "references" / "ONE.ru.md").write_text("Один.\n", encoding="utf-8")
                    with mock.patch.object(mcp_tools, "__file__", str(base / module)):
                        self.assertEqual(mcp_tools._skill(), base / skill)
                        self.assertEqual(mcp_tools.guides(), ["ONE.md"])
                        self.assertEqual(mcp_tools.guide({}), mcp_tools.NOTE + "# Rules of the %s\n" % layout)
                        self.assertEqual(mcp_tools.guide({"name": "ONE.md"}), mcp_tools.NOTE + "One.\n")
            # A broken installation still lists its tools and says what is missing.
            with mock.patch.object(mcp_tools, "__file__", str(base / "other/site/richi/mcp_tools.py")):
                self.assertIsNone(mcp_tools._skill())
                tool = mcp_tools.definitions(self.root)[SESSION.index(mcp_tools.GUIDE)]
                self.assertNotIn("enum", tool["inputSchema"]["properties"]["name"])
                with self.assertRaisesRegex(mcp_tools.ToolError, "carries no guides"):
                    mcp_tools.guide({})


class LauncherTests(unittest.TestCase):
    """A tool call is an ordinary launcher command: its chat binding, its results, its errors."""
    RECORD = {"id": "decision:atlas-pool-sizing", "kind": "decision", "knowledge_state": "confirmed",
              "title": "Atlas: размер пула по измеренной нагрузке",
              "summary": "The pool is sized from the measured peak of concurrent requests.\nNot from the core count.",
              "sources": [{"reference": "https://example.test/atlas/pull/42"}], "expected_updated_at": None}

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-mcp-tools-")
        self.addCleanup(temporary.cleanup)
        self.environment = cli_environment(temporary.name)
        self.root = memory.parser()
        self.launch(["init"])
        self.launch(["--chat=" + CHAT, "chat", "bind", "--current"])

    def launch(self, argv, stdin=None, expected=0):
        result = subprocess.run(bootstrap_command(argv), input=stdin, capture_output=True, text=True,
                                env=self.environment, timeout=60)
        self.assertEqual(result.returncode, expected, result.stderr or result.stdout)
        return json.loads(result.stdout if expected == 0 else result.stderr)

    def call(self, name, arguments, expected=0):
        invocation = mcp_tools.invocation(self.root, name, arguments)
        return self.launch(invocation.argv, invocation.stdin, expected)

    def test_records_written_by_a_call_are_read_by_the_next_calls(self):
        identity = self.RECORD["id"]
        self.assertEqual(self.call("entry_put", {"chat": CHAT, "records": [self.RECORD]}),
                         {"results": [{"id": identity, "status": "created"}]})
        stored = self.call("entry_get", {"chat": CHAT, "id": identity})
        self.assertEqual((stored["title"], stored["summary"]), (self.RECORD["title"], self.RECORD["summary"]))
        recalled = self.call("recall", {"chat": CHAT, "query": "pool sized from measured peak", "limit": 3})
        self.assertEqual([item["id"] for item in recalled["results"]], [identity])
        fetched = self.call("fetch", {"chat": CHAT, "refs": ["entry:" + identity, "entry:decision:absent"]})
        self.assertEqual([item["status"] for item in fetched["results"]], ["found", "missing"])
        # The generic tool runs a further command with the same binding.
        self.assertEqual(self.call(mcp_tools.RUN_READ, {"chat": CHAT, "command": "check"})["status"], "ok")
        # A stale version is refused by the store, as for any other caller.
        conflict = self.call("entry_put", {"chat": CHAT, "records": [dict(self.RECORD, title="Changed")]}, expected=1)
        self.assertIn("expected_updated_at does not match", conflict["error"])

    def test_a_chat_without_a_workspace_reads_nothing(self):
        self.call("entry_put", {"chat": CHAT, "records": [self.RECORD]})
        for name, arguments in (("recall", {"query": "pool"}), ("entry_get", {"id": self.RECORD["id"]}),
                                (mcp_tools.RUN_READ, {"command": "check"})):
            with self.subTest(name=name):
                refused = self.call(name, dict(arguments, chat="claude:another-session"), expected=1)
                self.assertEqual(refused["code"], "chat_workspace_required")
                self.assertNotIn("results", refused)

    def test_values_cannot_select_other_storage(self):
        elsewhere = self.environment["HOME"] + "/elsewhere.sqlite3"
        refused = self.call("recall", {"chat": CHAT, "query": "pool", "project_path": "--db=" + elsewhere}, expected=1)
        self.assertNotIn("results", refused)
        answer = self.call("recall", {"chat": CHAT, "query": "--db=" + elsewhere})
        self.assertEqual(answer["query"], "--db=" + elsewhere)
        with self.assertRaises(OSError):
            open(elsewhere)


if __name__ == "__main__":
    unittest.main()
