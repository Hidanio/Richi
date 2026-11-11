"""MCP server on standard input and output.

It speaks both protocol eras: per-request metadata (2026-07-28) and the
initialize handshake of the earlier revisions. It keeps no conversation state
and selects no workspace: every tool call names its chat and runs as an
ordinary launcher command, so the chat binding, the runtime selection and the
errors are the command line's own. Tool definitions come from mcp_tools.

A client keeps the server running for hours, so after an update the server
re-executes on the installation's current release, on the same streams.
"""
import json
import os
import select
import subprocess
import sys

from . import __version__, mcp_tools

MODERN = ("2026-07-28",)
HANDSHAKE = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
VERSION = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO = "io.modelcontextprotocol/serverInfo"
SERVER = {"name": "richi", "version": __version__}
CAPABILITIES = {"tools": {}}
# The revision requires caching hints on these results. The tool list is the
# same for every caller, and a restarted server may list other tools.
CACHED = {"server/discover": {"ttlMs": 0, "cacheScope": "public"}, "tools/list": {"ttlMs": 0, "cacheScope": "public"}}
CALL_TIMEOUT = 300
OUTPUT_LIMIT = 16 * 1024 * 1024
# How often an idle server looks whether its installation has changed.
IDLE_CHECK = 2.0
# The handshake of an earlier revision, carried over a re-execution: its client
# sends initialize only once.
HANDSHAKE_ENV = "RICHI_MCP_HANDSHAKE"
# What selected the server's own process. A tool call is selected by its chat
# alone, so none of this may reach the command that runs it.
SELECTORS = ("RICHI_CONFIG", "RICHI_DB", "RICHI_DATA_DIR", "RICHI_WORKSPACE", "RICHI_ACTIVE_CONFIG",
             "RICHI_ACTIVE_WORKSPACE", "RICHI_ACTIVE_RUNTIME", "RICHI_CHAT_ID", "CODEX_THREAD_ID")


class Refusal(Exception):
    """A request the protocol rejects; reported as a JSON-RPC error."""

    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.body = dict({"code": code, "message": message}, **({"data": data} if data is not None else {}))


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def launch(argv, stdin):
    """Run one command through the installed launcher, as a terminal would.
    Returns its exit status, standard output and standard error."""
    from richi_launcher.runtime import bootstrap_command, lease_fds
    environment = {key: value for key, value in os.environ.items() if key not in SELECTORS}
    # Without records the command gets no input: the server's own standard
    # input is the protocol stream and must never be inherited.
    source = {"stdin": subprocess.DEVNULL} if stdin is None else {"input": stdin}
    try:
        done = subprocess.run(bootstrap_command(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              encoding="utf-8", errors="replace", env=environment, timeout=CALL_TIMEOUT,
                              pass_fds=lease_fds(), **source)
    except subprocess.TimeoutExpired:
        return 1, "", compact({"error": "The command did not finish in %d seconds" % CALL_TIMEOUT, "code": "timeout"})
    return done.returncode, done.stdout, done.stderr


def _text(value, failed=False):
    return {"content": [{"type": "text", "text": value if isinstance(value, str) else compact(value)}], "isError": failed}


def outcome(status, out, err):
    """A tool result from a finished command: its JSON output, or the JSON
    error it printed, which names what the caller can do next."""
    if status == 0 and len(out) <= OUTPUT_LIMIT:
        return _text(out.strip())
    if status == 0:
        return _text({"error": "The result exceeds the size limit; narrow the request", "code": "output_too_large"}, True)
    for line in reversed(err.strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return _text(value, True)
    return _text({"error": (err or out).strip()[-2000:] or "The command failed", "code": "command_failed"}, True)


class Server:
    """One connection. Only the handshake of an earlier revision is remembered,
    as those revisions require; nothing about chats or calls is."""

    def __init__(self, root, profile="core", launch=launch, handshake=None):
        self.root, self.launch, self.profile = root, launch, profile
        self.tools = mcp_tools.definitions(root, profile)
        self.names = {tool["name"] for tool in self.tools}
        self.handshake = handshake if handshake in HANDSHAKE else None

    def call(self, params):
        name, arguments = params.get("name"), params.get("arguments")
        if not isinstance(name, str) or name not in self.names:
            raise Refusal(-32602, "Unknown tool: " + str(name))
        try:
            answer = mcp_tools.call(self.root, name, {} if arguments is None else arguments, params.get("_meta"))
        except mcp_tools.ToolError as exc:
            return _text({"error": str(exc), "code": "usage_error"}, True)
        if not isinstance(answer, mcp_tools.Invocation):
            return _text(answer)  # answered without a command
        return outcome(*self.launch(answer.argv, answer.stdin))

    def operation(self, method, params):
        if method == "tools/list":
            return {"tools": self.tools}
        if method == "tools/call":
            return self.call(params)
        if method == "ping":
            return {}
        raise Refusal(-32601, "Method not found: " + method)

    def answer(self, method, params):
        meta = params.get("_meta")
        if isinstance(meta, dict) and VERSION in meta:
            # A self-contained request: nothing before it matters.
            if meta[VERSION] not in MODERN:
                raise Refusal(-32022, "Unsupported protocol version",
                              {"supported": list(MODERN), "requested": meta[VERSION]})
            if CLIENT_CAPABILITIES not in meta:
                raise Refusal(-32602, "Missing required _meta field: " + CLIENT_CAPABILITIES)
            if method == "server/discover":
                result = {"supportedVersions": list(MODERN), "capabilities": CAPABILITIES,
                          "instructions": mcp_tools.INSTRUCTIONS}
            else:
                result = self.operation(method, params)
            return dict(result, resultType="complete", _meta={SERVER_INFO: SERVER}, **CACHED.get(method, {}))
        if method == "initialize":
            requested = params.get("protocolVersion")
            self.handshake = requested if requested in HANDSHAKE else HANDSHAKE[-1]
            return {"protocolVersion": self.handshake, "capabilities": CAPABILITIES, "serverInfo": SERVER,
                    "instructions": mcp_tools.INSTRUCTIONS}
        if method == "ping":
            return {}
        if self.handshake is None:
            # A client of an earlier revision can only show this message.
            raise Refusal(-32602, "Missing required _meta field: %s. Supported protocol versions: %s; "
                          "earlier clients send initialize first (%s)" % (VERSION, ", ".join(MODERN), ", ".join(HANDSHAKE)))
        return self.operation(method, params)

    def handle(self, line):
        """The reply to one line of input, or None when it needs none."""
        try:
            message = json.loads(line)
        except ValueError:
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            identity = message.get("id") if isinstance(message, dict) else None
            return {"jsonrpc": "2.0", "id": identity, "error": {"code": -32600, "message": "Invalid request"}}
        method, identity = message.get("method"), message.get("id")
        if not isinstance(method, str) or "id" not in message:
            return None  # a notification, or a client's reply to a request this server never sends
        if isinstance(identity, bool) or not isinstance(identity, (str, int)):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
        params = {} if message.get("params") is None else message["params"]
        try:
            if not isinstance(params, dict):
                raise Refusal(-32602, "params must be an object")
            return {"jsonrpc": "2.0", "id": identity, "result": self.answer(method, params)}
        except Refusal as exc:
            return {"jsonrpc": "2.0", "id": identity, "error": exc.body}
        except Exception as exc:  # the server stays up; the client sees the failure
            return {"jsonrpc": "2.0", "id": identity,
                    "error": {"code": -32603, "message": "Internal error: " + str(exc)[:500]}}


def serve(server, reader, writer):
    """Answer newline-delimited messages until the input ends."""
    for line in reader:
        if not line.strip():
            continue
        reply = server.handle(line)
        if reply is not None:
            # json.dumps escapes line breaks inside strings: one reply is one line.
            writer.write(compact(reply).encode("utf-8") + b"\n")
            writer.flush()


def lines(descriptor, idle, interval=None):
    """Complete lines of the input. idle() runs whenever no input is held
    here, before waiting and every interval while waiting: whatever the client
    has sent by then is still in the pipe, so the process may be replaced."""
    held = b""
    while True:
        while b"\n" in held:
            line, held = held.split(b"\n", 1)
            yield line
        if not held:
            idle()
        if not select.select([descriptor], [], [], IDLE_CHECK if interval is None else interval)[0]:
            continue
        chunk = os.read(descriptor, 65536)
        if not chunk:
            if held.strip():
                yield held
            return
        held += chunk


def follow(server):
    """Re-execute on the installation's current release when it has changed.

    The streams, and with them the client's connection, stay as they are. The
    installation's entry point selects and leases the release; this process
    keeps serving when there is none, or when the release is incomplete.
    """
    from richi_launcher import maps
    from richi_launcher.runtime import lease_fds
    if not (os.environ.get(maps.ROOT_ENV) and os.environ.get(maps.GENERATION_ENV)):
        return  # a source checkout or an ordinary installation has no releases to follow
    try:
        if maps.running_current_generation() or maps.handoff_problem() is not None:
            return
        command, entry_point = maps.stable_command(["mcp", "--tools", server.profile])
    except Exception:  # an installed launcher this server does not understand
        return
    if not entry_point:
        return
    dropped = SELECTORS + (maps.LEASE_ENV, maps.ROOT_ENV, maps.GENERATION_ENV)
    environment = {key: value for key, value in os.environ.items() if key not in dropped}
    if server.handshake is not None:
        environment[HANDSHAKE_ENV] = server.handshake
    else:
        environment.pop(HANDSHAKE_ENV, None)
    # This release's lease ends when its code stops running.
    for descriptor in lease_fds():
        os.set_inheritable(descriptor, False)
    os.execve(command[0], command, environment)


def main(args):
    from . import memory
    wire, source = sys.stdout.buffer, sys.stdin.fileno()
    # Nothing but protocol messages may reach the client.
    sys.stdout = sys.stderr
    server = Server(memory.parser(), args.tools, handshake=os.environ.pop(HANDSHAKE_ENV, None))
    serve(server, lines(source, lambda: follow(server)), wire)
    return 0
