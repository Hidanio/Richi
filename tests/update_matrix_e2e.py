"""Release gate: every published release updates to this candidate in place.

Sources are the stable vX.Y.Z git tags from v1.0.1 below the candidate's version
(v1.0.0 failed validation and was never published). Each is built from its tag
with that tag's own release scripts and installed into a synthetic home with its
own installer. Then the user's real commands run: the source's own update check,
apply and automatic run go through the offline runner from test_update_e2e (only
GitHub transport and scheduler registration are replaced); rollback goes through
the original environment's `richi` console.

Every source gets two homes with realistic state: workspaces with projects,
entries and graph records, a bound chat, Codex and, where the source supports it,
Claude Code. One updates manually, rolls back, goes forward again and covers the
rollback edges; the other updates with `update auto enable --install` and
`update auto run`. Without --from, one more home updates the oldest source
through every intermediate release to the candidate. After each step the
version, every connected agent's `setup --status`, the chat binding and the store
schema are checked, and recall/search/read/get results must equal the source's
first answers apart from generation timestamps.

Usage: python tests/update_matrix_e2e.py DIST [--from vX.Y.Z]... [--list]
DIST holds the candidate's wheel and its richi-release.json; install that wheel in
the running Python first. Needs the git tags (a full clone) and the `build`
package; building a tag fetches its build backend.
"""
import argparse
import io
import json
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from cli_environment import cli_environment  # noqa: E402
import test_update_e2e  # noqa: E402

# v1.0.0 was tagged from code reporting 0.1.0; its validation failed, nothing was published.
FIRST_PUBLISHED = (1, 0, 1)
# From 1.1.0 the original console refuses a rollback to a release lacking a database
# schema the current one supports; 1.0.1 rolls back without that check.
ROLLBACK_SCHEMA_CHECK = (1, 1, 0)
BASE = "https://github.com/Hidanio/Richi/releases/"
API = "https://api.github.com/repos/Hidanio/Richi/releases/"
CHAT = "matrix-chat"
FOREIGN_HOOK = {"hooks": [{"type": "command", "command": "/usr/bin/true"}], "matcher": ""}
SCHEMAS = ("import json, sys; sys.path.insert(0, sys.argv[1]); from richi import memory; "
           "print(json.dumps(sorted(getattr(memory, 'SUPPORTED_VERSIONS', {1, memory.VERSION}))))")


def run(command, env=None, ok=True, stdin=None, cwd=None):
    result = subprocess.run([str(item) for item in command], env=env, input=stdin, cwd=cwd,
                            capture_output=True, text=True, timeout=600)
    if ok is not None and (result.returncode == 0) != ok:
        raise AssertionError("{} -> {}\n{}\n{}".format(command, result.returncode, result.stdout, result.stderr))
    return result


def version_key(version):
    match = re.fullmatch(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version)
    return tuple(int(part) for part in match.groups()) if match else None


class Release:
    def __init__(self, dist, work, tag=None):
        self.manifest = dist / "richi-release.json"
        data = json.loads(self.manifest.read_text())
        self.version, self.wheel = data["version"], dist / data["wheel"]["filename"]
        self.key, self.tag = version_key(self.version), tag or "v" + data["version"]
        self.installer = dist / "richi-installer.pyz"
        # What the release's own code reports, read without installing it.
        unpacked = work / ("unpacked-" + self.version)
        with zipfile.ZipFile(self.wheel) as archive:
            archive.extractall(unpacked)
        self.schemas = json.loads(run([sys.executable, "-I", "-B", "-c", SCHEMAS, unpacked]).stdout)
        self.feed = self._feed(work)

    def _feed(self, work):
        """Offline GitHub responses that make this release the latest."""
        manifest_url = BASE + "download/v" + self.version + "/richi-release.json"
        wheel_url = BASE + "download/v" + self.version + "/" + self.wheel.name
        release = {"draft": False, "prerelease": False, "tag_name": "v" + self.version,
                   "html_url": BASE + "tag/v" + self.version,
                   "assets": [{"name": "richi-release.json", "state": "uploaded",
                               "size": self.manifest.stat().st_size, "browser_download_url": manifest_url},
                              {"name": self.wheel.name, "state": "uploaded", "size": self.wheel.stat().st_size,
                               "browser_download_url": wheel_url}]}
        release_path = work / (self.version + "-release.json")
        release_path.write_text(json.dumps(release))
        responses = {API + "tags/v" + self.version: str(release_path), API + "latest": str(release_path),
                     manifest_url: str(self.manifest), wheel_url: str(self.wheel)}
        path = work / (self.version + "-responses.json")
        path.write_text(json.dumps(responses))
        return path


def discover(candidate):
    """Every published stable tag below the candidate, oldest first."""
    tags = run(["git", "-C", ROOT, "tag", "--list", "v*"]).stdout.split()
    keyed = sorted((version_key(tag), tag) for tag in tags if version_key(tag) is not None)
    return [tag for key, tag in keyed if FIRST_PUBLISHED <= key < candidate.key]


def build(tag, work):
    """Build a published release's artifacts with that tag's own scripts."""
    source, dist = work / ("source-" + tag), work / ("dist-" + tag)
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", tag],
                             capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(source, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    run([sys.executable, "-m", "build", "--wheel", "--outdir", dist, source])
    (wheel,) = dist.glob("richi-" + tag[1:] + "-py3-none-any.whl")
    # Explicit outputs and cwd: the scripts' defaults are relative to the caller.
    run([sys.executable, source / "scripts/release_manifest.py", wheel, "--tag", tag,
         "--output", dist / "richi-release.json"], cwd=source)
    run([sys.executable, source / "scripts/build_installer.py", wheel,
         "--output", dist / "richi-installer.pyz"], cwd=source)
    return Release(dist, work, tag)


def store_schema(store):
    with sqlite3.connect("file:" + str(store) + "?mode=ro", uri=True) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def store_files(store):
    """The updater never opens a store: every SQLite file keeps its bytes."""
    return {path.name: path.read_bytes() for path in
            (store, store.with_name(store.name + "-wal"), store.with_name(store.name + "-shm")) if path.exists()}


SOURCES = [{"reference": "https://example.invalid/relay/pull/7", "type": "pull_request", "observed_at": "2026-09-14"}]
PROJECTS = [{"id": "relay", "name": "Relay", "description": "Synthetic receipt delivery service"},
            {"id": "ledger", "name": "Ledger", "description": "Synthetic booking ledger"}]
RELATION = {"id": "relation:relay-ledger", "from_project": "relay", "to_project": "ledger", "kind": "calls_api",
            "description": "Relay posts receipts to the Ledger API.", "source": SOURCES[0]["reference"],
            "knowledge_state": "confirmed", "verified_at": "2026-09-14"}
ENTRIES = [
    {"id": "task:DEMO-7", "kind": "task", "jira_key": "DEMO-7", "title": "DEMO-7: retry receipts idempotently",
     "summary": "Relay retries receipt delivery with an idempotency key, so Ledger books each receipt once.",
     "project_ids": ["relay", "ledger"], "knowledge_state": "confirmed", "work_state": "implemented",
     "verified_at": "2026-09-14", "sources": SOURCES, "tags": ["receipts"], "aliases": ["receipt retry"]},
    {"id": "decision:receipt-key", "kind": "decision", "jira_key": "DEMO-7",
     "title": "Receipt idempotency key is the delivery ID",
     "summary": "The delivery ID is stable across retries; payload hashes changed with formatting.",
     "project_ids": ["relay"], "knowledge_state": "confirmed", "verified_at": "2026-09-12",
     "sources": [{"reference": "https://example.invalid/relay/spec/receipts", "type": "specification"}],
     "tags": ["receipts"]},
    {"id": "investigation:double-booking", "kind": "investigation", "jira_key": "DEMO-7",
     "title": "Double booking after receipt timeouts",
     "summary": "Hypothesis: a timeout retry raced the first delivery; the idempotency key prevents it.",
     "project_ids": ["relay", "ledger"], "knowledge_state": "hypothesis",
     "sources": [{"reference": "https://example.invalid/relay/traces/race", "type": "investigation"}]},
]
ENTITIES = [
    {"id": "pr:relay-7", "kind": "pull_request", "title": "PR 7: receipt idempotency key",
     "summary": "Adds the delivery-ID idempotency key; merged, not yet released.", "sources": SOURCES,
     "knowledge_state": "confirmed", "verified_at": "2026-09-14", "tags": ["receipts"], "aliases": ["receipt key"]},
    {"id": "concept:idempotency-key", "kind": "concept", "title": "Idempotency key",
     "summary": "A client-chosen identifier that lets Ledger ignore a repeated receipt.",
     "sources": [{"reference": "https://example.invalid/relay/spec/receipts"}],
     "knowledge_state": "confirmed", "verified_at": "2026-09-12", "aliases": ["dedup key"]},
]
EDGES = [
    {"id": "pr7-implements-demo7", "from_ref": "entity:pr:relay-7", "to_ref": "entry:task:DEMO-7",
     "kind": "implements", "description": "The PR implements the task; release state is checked separately.",
     "sources": SOURCES, "knowledge_state": "confirmed", "verified_at": "2026-09-14"},
    {"id": "idempotency-key-used-in-relay", "from_ref": "entity:concept:idempotency-key",
     "to_ref": "project:relay", "kind": "used_in", "description": "Relay sends the key with every receipt.",
     "sources": SOURCES, "knowledge_state": "confirmed", "verified_at": "2026-09-14"},
    {"id": "decision-part-of-demo7", "from_ref": "entry:decision:receipt-key", "to_ref": "entry:task:DEMO-7",
     "kind": "part_of", "description": "The decision selected the key used by the task.",
     "sources": SOURCES, "knowledge_state": "confirmed", "verified_at": "2026-09-14"},
]
# The default workspace holds a same-ID record; reads through the chat must not see it.
DECOY = {"id": "task:DEMO-7", "kind": "task", "title": "DEMO-7 in another workspace",
         "summary": "Receipt idempotency notes owned by the default workspace.", "project_ids": ["relay"],
         "knowledge_state": "confirmed", "sources": [{"reference": "https://example.invalid/default"}]}
QUERIES = {
    "recall": ["recall", "receipt idempotency key"],
    "recall_hypotheses": ["recall", "double booking timeout", "--include-hypotheses"],
    "search": ["search", "receipts"],
    "read": ["entry", "read", "task:DEMO-7"],
    "context": ["context", "--project", "relay"],
    "neighbors": ["graph", "neighbors", "entry:task:DEMO-7"],
    "graph": ["graph", "export"],
    "projects": ["project", "list"],
    **{"entry " + item["id"]: ["entry", "get", item["id"], "--history"] for item in ENTRIES},
    **{"entity " + item["id"]: ["entity", "get", item["id"], "--history"] for item in ENTITIES},
    **{"edge " + item["id"]: ["edge", "get", item["id"], "--history"] for item in EDGES},
}


class Home:
    """One synthetic user with a release installed by its own installer."""

    def __init__(self, work, name, release):
        self.name, self.root = name, work / name
        self.root.mkdir()
        self.env = cli_environment(self.root, SHELL="/bin/zsh")
        for key in [key for key in self.env if key.startswith("CLAUDE") or key in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}]:
            self.env.pop(key)
        self.profile = Path(self.env["HOME"]) / ".claude"
        self.profile.mkdir(parents=True)
        (self.profile / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [FOREIGN_HOOK]}}))
        installed = json.loads(run([sys.executable, "-I", release.installer, "--yes", "--no-path",
                                    "--wheel", release.wheel, "--manifest", release.manifest], self.env).stdout)
        seed = Path(installed["prefix"])
        self.python, self.console = seed / "bin/python", seed / "bin/richi"
        self.runner = work / "offline.py"
        self.agents = ["codex"]  # The installer connects Codex.
        self.store = self.schema = self.baseline = None

    def cli(self, *args, ok=True, stdin=None):
        return run([self.console, *args], self.env, ok=ok, stdin=stdin)

    def json(self, *args, **kwargs):
        return json.loads(self.cli(*args, **kwargs).stdout)

    def update(self, release, *args, ok=True):
        """The current release's own updater, offline."""
        return run([self.python, "-I", "-B", self.runner, release.feed, "update", *args], self.env, ok=ok)

    def put(self, workspace, command, payload):
        return self.json("--workspace", workspace, *command, "--json", "-", stdin=json.dumps(payload))

    def version(self):
        return self.cli("--version").stdout.strip()

    def offered_agents(self):
        """Agents the active release can connect; releases before 1.1.0 support Codex only."""
        match = re.search(r"--agent \{([a-z,]+)\}", self.cli("setup", "--help").stdout)
        return match.group(1).split(",") if match else ["codex"]

    def connect_agents(self):
        """Connect Claude Code once the active release supports it; report whether it did."""
        if "claude" in self.agents or "claude" not in self.offered_agents():
            return False
        self.cli("setup", "--agent", "claude", "--yes", "--no-path")
        self.agents.append("claude")
        return True

    def setup_status(self, agent):
        data = self.json("setup", *(["--agent", agent] if agent != "codex" else []), "--status")
        return data["status"], data.get("changes", [])

    def create_state(self):
        self.cli("init")
        self.store = Path(self.json("workspace", "create", "alpha")["database"])
        self.cli("--workspace", "alpha", "init")
        self.put("alpha", ("project", "upsert"), PROJECTS)
        self.put("alpha", ("relation", "put"), RELATION)
        self.put("alpha", ("entry", "put"), ENTRIES)
        self.put("alpha", ("entity", "put"), ENTITIES)
        self.put("alpha", ("edge", "put"), EDGES)
        self.put("default", ("project", "upsert"), PROJECTS[:1])
        self.put("default", ("entry", "put"), DECOY)
        self.cli("--chat", CHAT, "chat", "bind", "alpha")
        self.schema = store_schema(self.store)

    def observe(self):
        return knowledge({name: json.loads(self.cli("--chat", CHAT, *query).stdout) for name, query in QUERIES.items()})

    def verify(self, release):
        """The active release, its agents, the chat binding and unchanged knowledge."""
        assert self.version() == "Richi " + release.version, (self.name, self.version(), release.version)
        for agent in self.agents:
            assert self.setup_status(agent) == ("configured", []), (self.name, agent, self.setup_status(agent))
        if "claude" in self.agents:
            self.verify_claude_hook()
        current = self.json("--chat", CHAT, "chat", "current")
        assert current["workspace"] == "alpha", (self.name, current)
        assert store_schema(self.store) == self.schema, (self.name, store_schema(self.store), self.schema)
        observed = self.observe()
        if self.baseline is None:
            self.baseline = observed
        elif observed != self.baseline:
            changed = sorted(name for name in QUERIES if observed[name] != self.baseline[name])
            raise AssertionError("{} on {}: results changed for {}\nbefore: {}\nafter:  {}".format(
                self.name, release.version, ", ".join(changed),
                json.dumps({name: self.baseline[name] for name in changed}, sort_keys=True)[:6000],
                json.dumps({name: observed[name] for name in changed}, sort_keys=True)[:6000]))

    def verify_claude_hook(self):
        starts = json.loads((self.profile / "settings.json").read_text())["hooks"]["SessionStart"]
        assert FOREIGN_HOOK in starts and len(starts) == 2, starts
        command = [item for item in starts if item != FOREIGN_HOOK][0]["hooks"][0]["command"]
        env_file = self.root / "claude-env"
        if env_file.exists():
            env_file.unlink()
        run(shlex.split(command), dict(self.env, CLAUDE_ENV_FILE=str(env_file)),
            stdin=json.dumps({"hook_event_name": "SessionStart", "session_id": "matrix-session"}))
        assert env_file.read_text() == "export RICHI_CHAT_ID=claude:matrix-session\n", env_file.read_text()

    def apply(self, release):
        """The active release's updater installs `release` and leaves the store untouched."""
        before = store_files(self.store)
        checked = json.loads(self.update(release, "check").stdout)
        assert checked["status"] == "update_available" and checked["release"]["version"] == release.version, checked
        applied = json.loads(self.update(release, "apply").stdout)
        assert applied["status"] == "updated", applied
        assert store_files(self.store) == before, self.name + ": the update changed the store"
        self.verify(release)


def knowledge(value):
    """Results without the time each answer was generated."""
    if isinstance(value, dict):
        return {key: knowledge(item) for key, item in value.items() if key != "generated_at"}
    if isinstance(value, list):
        return [knowledge(item) for item in value]
    return value


def rollback_allowed(source, candidate):
    return not set(candidate.schemas) - set(source.schemas) or source.key < ROLLBACK_SCHEMA_CHECK


def prepare(work, name, source):
    home = Home(work, name, source)
    home.connect_agents()
    home.create_state()
    home.verify(source)
    return home


def direct(work, source, candidate):
    home = prepare(work, "direct-" + source.tag, source)
    home.apply(candidate)
    if not rollback_allowed(source, candidate):
        refused = home.cli("update", "rollback", ok=False)
        assert "drops database schema support" in refused.stdout + refused.stderr, refused.stderr
        home.verify(candidate)
        return home
    # The user's rollback: the original console selects the previous release's manager.
    home.cli("update", "rollback")
    home.verify(source)
    home.apply(candidate)

    # A store migrated by the candidate, then a rollback by a source without the check.
    newest = max(candidate.schemas)
    if home.schema < newest:
        home.cli("--workspace", "alpha", "backup")
        assert home.json("--workspace", "alpha", "migrate")["schema_version"] == newest
        home.cli("update", "rollback")
        assert home.version() == "Richi " + source.version
        if newest in source.schemas:
            assert home.json("--workspace", "alpha", "check")["status"] == "ok"
        else:
            refused = home.cli("--workspace", "alpha", "check", ok=False)
            assert "Unsupported schema version %d" % newest in refused.stdout + refused.stderr, refused.stderr
            assert store_schema(home.store) == newest
        assert json.loads(home.update(candidate, "apply").stdout)["status"] == "updated"
        assert home.version() == "Richi " + candidate.version
        assert home.json("--workspace", "alpha", "check")["status"] == "ok"

    # A source that cannot maintain Claude Code refuses the rollback once the candidate connected it.
    if "claude" not in home.agents and "claude" in home.offered_agents():
        home.cli("setup", "--agent", "claude", "--yes", "--no-path")
        home.agents.append("claude")
        settings = (home.profile / "settings.json").read_bytes()
        home.cli("update", "rollback", ok=False)
        assert home.version() == "Richi " + candidate.version
        assert (home.profile / "settings.json").read_bytes() == settings
        for agent in home.agents:
            assert home.setup_status(agent) == ("configured", []), agent
        home.verify_claude_hook()
    return home


def automatic(work, source, candidate):
    home = prepare(work, "auto-" + source.tag, source)
    before = store_files(home.store)
    enabled = json.loads(home.update(candidate, "auto", "enable", "--install").stdout)
    assert enabled["status"] == "enabled" and enabled["auto_install"] is True, enabled
    ran = json.loads(home.update(candidate, "auto", "run").stdout)
    assert ran["status"] == "updated" and ran["version"] == candidate.version, ran
    assert store_files(home.store) == before, home.name + ": the update changed the store"
    home.verify(candidate)
    # The candidate keeps the policy and schedule the source recorded.
    assert home.json("update", "auto", "run")["status"] == "not_due"
    status = json.loads(home.update(candidate, "auto", "status").stdout)
    assert status["enabled"] and status["auto_install"] and status["status"] == "enabled", status
    return home


def chain(work, sources, candidate):
    home = prepare(work, "chain", sources[0])
    for release in sources[1:] + [candidate]:
        home.apply(release)
        if home.connect_agents():
            home.verify(release)
    return home


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dist", type=Path, help="Directory with the candidate's wheel and richi-release.json")
    parser.add_argument("--from", dest="only", action="append", metavar="TAG",
                        help="Run only this source release (repeatable); skips the chained path")
    parser.add_argument("--list", action="store_true", help="Print the source releases and exit")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="richi-update-matrix-") as directory:
        work = Path(directory).resolve()
        candidate = Release(args.dist.resolve(), work)
        tags = discover(candidate)
        if args.list:
            print("Candidate Richi {}; sources: {}".format(candidate.version, " ".join(tags) or "none"))
            return 0
        if not tags:
            parser.error("No published release tag below {}; fetch tags (fetch-depth: 0)".format(candidate.version))
        unknown = sorted(set(args.only or []) - set(tags))
        if unknown:
            parser.error("Not a source for {}: {} (sources: {})".format(candidate.version, " ".join(unknown), " ".join(tags)))
        (work / "offline.py").write_text(test_update_e2e._OFFLINE_COMMAND)
        selected = [tag for tag in tags if not args.only or tag in args.only]
        sources = {}
        for tag in selected:
            started = time.monotonic()
            sources[tag] = build(tag, work)
            print("Built {} in {:.0f}s".format(tag, time.monotonic() - started), flush=True)
        scenarios = [("direct " + tag, direct, (sources[tag], candidate)) for tag in selected]
        scenarios += [("automatic " + tag, automatic, (sources[tag], candidate)) for tag in selected]
        if not args.only and len(tags) > 1:
            scenarios.append(("chain " + " -> ".join(tags + ["v" + candidate.version]), chain,
                              ([sources[tag] for tag in tags], candidate)))
        for label, scenario, arguments in scenarios:
            started = time.monotonic()
            home = scenario(work, *arguments)
            print("Passed {} in {:.0f}s (agents: {}; store schema {})".format(
                label, time.monotonic() - started, ", ".join(home.agents), home.schema), flush=True)
    print("Update matrix passed for Richi {} from {}".format(candidate.version, ", ".join(selected)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
