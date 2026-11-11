#!/usr/bin/env python3
"""Count what the MCP tool contract adds to an agent's context, against the CLI path.

No store is opened, no model runs and no server starts: the script tokenizes
the texts each path puts in front of a model. It answers three questions.

1. What is in context before the first call: the managed instructions and the
   core skill today; the server instructions and tool definitions with MCP,
   loaded up front (all tools or the core profile) or deferred by the client.
2. What does learning one command's syntax cost: `capabilities describe` today,
   the command's tool definition with MCP.
3. What do the arguments of the same call cost in each form.

The skill and its guides also carry procedures that tool schemas do not
replace; their size is reported, not subtracted.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evaluate_agent_workflows import tokenize  # noqa: E402  the same tokenizer contract

from richi import capabilities, mcp_tools, memory, output  # noqa: E402
from richi_launcher import agent_setup  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CHAT = "claude:0af4cd88-675d-4258-a011-fdab17bf6333"
RECORD = {"id": "decision:atlas-pool-sizing", "kind": "decision", "title": "Atlas: size the pool from measured concurrency",
          "summary": "The pool is sized from the measured peak of concurrent requests, not from the host's core count. "
                     "The earlier fixed size of 20 exhausted connections under the nightly export.",
          "knowledge_state": "confirmed", "project_ids": ["atlas"], "verified_at": "2026-11-01",
          "sources": [{"reference": "https://example.test/atlas/pull/42", "type": "pull_request"}],
          "expected_updated_at": None}
CALLS = (
    ("recall", {"chat": CHAT, "project_path": "/work/atlas", "query": "connection pool sizing timeout", "project": "atlas"}),
    ("fetch", {"chat": CHAT, "project_path": "/work/atlas",
               "refs": ["entry:decision:atlas-pool-sizing", "entry:investigation:atlas-export-timeouts"]}),
    ("query_count", {"chat": CHAT, "project": "atlas", "where": "kind = 'decision' AND work_state = 'released'"}),
    ("graph_neighbors", {"chat": CHAT, "ref": "entry:decision:atlas-pool-sizing", "view": "compact", "depth": 2}),
    ("entry_put", {"chat": CHAT, "project_path": "/work/atlas", "records": [RECORD]}),
)


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def texts(skill):
    root = memory.parser()
    every, core = mcp_tools.definitions(root, "all"), mcp_tools.definitions(root, "core")
    result = {
        "mcp:instructions": mcp_tools.INSTRUCTIONS,
        "mcp:tools_all": compact({"tools": every}),
        "mcp:tools_core": compact({"tools": core}),
        # A client that defers definitions shows the model only names and instructions.
        "mcp:names_all": "\n".join(tool["name"] for tool in every),
        "cli:instructions": agent_setup._block("/usr/local/bin/richi", "claude").decode("utf-8"),
        "cli:skill": skill.read_text(encoding="utf-8"),
        "cli:capabilities_list": "".join(
            output.rendered(memory.run(root.parse_args(["capabilities", "list"] + extra)), compact=True)
            for extra in ([], ["--group", "all"])),
    }
    for row in capabilities.COMMANDS:
        name = mcp_tools.tool_name(row[0])
        result["mcp:tool:" + name] = compact(mcp_tools.definition(root, name))
        result["cli:describe:" + name] = output.rendered(
            memory.run(root.parse_args(["capabilities", "describe", row[0]])), compact=True)
    for name, arguments in CALLS:
        call = mcp_tools.invocation(root, name, arguments)
        result["mcp:call:" + name] = compact({"name": name, "arguments": arguments})
        # Today the agent types the command; records first go to a file it writes.
        argv = [part.replace("--json=-", "--json=/tmp/richi-records.json") for part in call.argv]
        result["cli:call:" + name] = compact({"cmd": shlex.join(["richi"] + argv)})
        if call.stdin is not None:
            result["cli:call:" + name] += compact({"file_path": "/tmp/richi-records.json", "content": call.stdin})
    return result, every, core


def report(skill, tokenizer):
    values, every, core = texts(skill)
    encoded = tokenize(tokenizer, values)
    encodings = {}
    for encoding, counts in encoded["counts"].items():
        names = [mcp_tools.tool_name(row[0]) for row in capabilities.COMMANDS]
        tool, describe = [counts["mcp:tool:" + name] for name in names], [counts["cli:describe:" + name] for name in names]
        encodings[encoding] = {
            "before_first_call": {
                "cli_instructions_and_skill": counts["cli:instructions"] + counts["cli:skill"],
                "mcp_all_tools_up_front": counts["mcp:instructions"] + counts["mcp:tools_all"],
                "mcp_core_profile_up_front": counts["mcp:instructions"] + counts["mcp:tools_core"],
                "mcp_deferred_names_only": counts["mcp:instructions"] + counts["mcp:names_all"],
                "parts": {key: counts[key] for key in ("cli:instructions", "cli:skill", "mcp:instructions", "mcp:tools_all",
                                                       "mcp:tools_core", "mcp:names_all", "cli:capabilities_list")},
            },
            "one_command_syntax": {
                "commands": len(names),
                "cli_describe": {"total": sum(describe), "median": statistics.median(describe), "max": max(describe)},
                "mcp_definition": {"total": sum(tool), "median": statistics.median(tool), "max": max(tool)},
                "per_command": {name: {"cli_describe": describe[index], "mcp_definition": tool[index]}
                                for index, name in enumerate(names)},
            },
            "call_arguments": {name: {"cli": counts["cli:call:" + name], "mcp": counts["mcp:call:" + name],
                                      "cli_tool_calls": 2 if name in ("entry_put",) else 1, "mcp_tool_calls": 1}
                               for name, _ in CALLS},
        }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "richi_version": __import__("richi").__version__, "python": sys.version.split()[0],
        "tokenizer": {"package": "tiktoken", "version": encoded["version"],
                      "note": "A common yardstick for both paths, not any client's own tokenizer."},
        "scope": ["Tokens of texts that enter context; no model, server or store is run.",
                  "Definitions are counted as compact JSON; a client may present them to its model in another form.",
                  "The skill and its guides also state procedures that schemas do not replace."],
        "tools": {"all": len(every), "core": len(core), "core_commands": list(mcp_tools.CORE),
                  "properties": sum(len(tool["inputSchema"]["properties"]) for tool in every),
                  "properties_without_description": sum(
                      "description" not in item for tool in every for item in tool["inputSchema"]["properties"].values())},
        "chars": {key: len(value) for key, value in values.items() if key.count(":") == 1},
        "sha256": {key: hashlib.sha256(value.encode("utf-8")).hexdigest() for key, value in values.items()
                   if key.count(":") == 1},
        "encodings": encodings,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tokenizer-python", required=True, type=Path,
                        help="Interpreter with tiktoken; TIKTOKEN_CACHE_DIR must name a populated cache")
    parser.add_argument("--skill", type=Path, default=ROOT / "skills/project-memory/SKILL.md",
                        help="Core skill file counted for the CLI path")
    parser.add_argument("--output", type=Path, help="New report file; an existing file is never overwritten")
    args = parser.parse_args(argv)
    rendered = json.dumps(report(args.skill, args.tokenizer_python), ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
