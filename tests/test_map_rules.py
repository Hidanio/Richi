"""The map changes standing rules only from a page opened by richi map; synthetic stores and homes only."""
import html
import http.client
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock

from richi import agents, map_access, memory, rules, serve, skill_catalog


def write_skill(directory, header):
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(header, encoding="utf-8")


def clean_environment(test, home):
    patch = mock.patch.dict(os.environ, {"HOME": str(home)})
    patch.start()
    test.addCleanup(patch.stop)
    # Agent settings are the user's: a test must read them only from its disposable home.
    for key in [key for key in os.environ if key.startswith("RICHI_")
                or key in {"CODEX_THREAD_ID", "CLAUDECODE", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "BROWSER",
                           "XDG_CONFIG_HOME"}]:
        del os.environ[key]
    agents._cache.clear()
    test.addCleanup(agents._cache.clear)


class SkillCatalogTests(unittest.TestCase):
    def test_front_matter_reads_a_safe_subset(self):
        folded = "---\nname: pr-description\ndescription: >\n  Writes the title\n  and the body.\nmetadata:\n  author: x\n---\nBody\n"
        self.assertEqual(skill_catalog.front_matter(folded),
                         {"name": "pr-description", "description": "Writes the title and the body."})
        quoted = "---\nname: 'it''s'\ndescription: \"Says \\\"hi\\\"\"\n---\n"
        self.assertEqual(skill_catalog.front_matter(quoted), {"name": "it's", "description": 'Says "hi"'})
        continued = "---\nname: a\ndescription: first line\n  continues here\n---\n"
        self.assertEqual(skill_catalog.front_matter(continued)["description"], "first line continues here")
        literal = "---\ndescription: |\n  one\n  two\n---\n"
        self.assertEqual(skill_catalog.front_matter(literal)["description"], "one\ntwo")
        for text in ("No header", "---\nname: unterminated\n", ""):
            with self.subTest(text=text):
                self.assertEqual(skill_catalog.front_matter(text), {})

    def test_catalog_merges_agents_and_offers_only_storable_names(self):
        with tempfile.TemporaryDirectory(prefix="richi-skills-") as temporary:
            root = Path(temporary)
            home, profile = root / "home", root / "claude-profile"
            codex = home / ".agents" / "skills"
            write_skill(profile / "skills" / "pr-description", "---\nname: pr-description\ndescription: Claude copy\n---\n")
            write_skill(codex / "pr-description", "---\nname: pr-description\ndescription: Codex copy\n---\n")
            write_skill(codex / "no-header", "Just instructions\n")
            write_skill(codex / "spaced", "---\nname: two words\n---\n")
            write_skill(codex / "long", "---\nname: long\ndescription: " + "x" * 2000 + "\n---\n")
            (codex / ".hidden").mkdir()
            (codex / "without-skill-file").mkdir()
            result = skill_catalog.catalog({"CLAUDE_CONFIG_DIR": str(profile)}, home)
        self.assertEqual([skill["name"] for skill in result["skills"]], ["long", "no-header", "pr-description"])
        skills = {skill["name"]: skill for skill in result["skills"]}
        self.assertEqual(skills["pr-description"], {"name": "pr-description", "description": "Claude copy",
                                                    "kind": "skills", "agents": ["claude", "codex"]})
        self.assertEqual(skills["no-header"], {"name": "no-header", "description": "", "kind": "skills",
                                               "agents": ["codex"]})
        self.assertEqual(len(skills["long"]["description"]), skill_catalog.MAX_DESCRIPTION)
        self.assertEqual([(entry["agent"], entry["kind"], entry["exists"]) for entry in result["roots"]],
                         [("claude", "skills", True), ("claude", "plugins", False), ("claude", "plugins", False),
                          ("codex", "skills", True), ("codex", "skills", False), ("codex", "system", False),
                          ("codex", "plugins", False)])
        self.assertFalse(result["truncated"])

    def test_catalog_includes_codex_system_skills_and_installed_plugin_skills(self):
        with tempfile.TemporaryDirectory(prefix="richi-skills-") as temporary:
            home = Path(temporary)
            codex, claude = home / ".codex", home / ".claude"
            write_skill(codex / "skills" / ".system" / "skill-installer",
                        "---\nname: skill-installer\ndescription: Installs skills\n---\n")
            (codex / "skills" / ".system" / ".codex-system-skills.marker").write_text("", encoding="utf-8")
            cache = codex / "plugins" / "cache"
            write_skill(cache / "bundled" / "sheets" / "1.0.0" / "skills" / "sheets",
                        "---\nname: Sheets\ndescription: Old version\n---\n")
            write_skill(cache / "bundled" / "sheets" / "1.1.0" / "skills" / "sheets",
                        "---\nname: Sheets\ndescription: Edits spreadsheets\n---\n")
            write_skill(cache / "bundled" / "sheets" / "1.1.0" / "skills" / "live-control", "No header\n")
            write_skill(cache / "remote" / "sheets" / "2.0.0" / "skills" / "sheets",
                        "---\nname: Sheets\ndescription: Remote copy\n---\n")
            write_skill(claude / "plugins" / "cache" / "market" / "engineering" / "abc123" / "skills" / "code-review",
                        "---\nname: code-review\ndescription: Reviews code\n---\n")
            (cache / ".staging" / "hidden" / "1" / "skills" / "secret").mkdir(parents=True)
            # Skills that only the user may invoke cannot follow a rule.
            write_skill(codex / "skills" / ".system" / "review-agent", "---\nname: review-agent\n---\n")
            (codex / "skills" / ".system" / "review-agent" / "agents").mkdir()
            (codex / "skills" / ".system" / "review-agent" / "agents" / "openai.yaml").write_text(
                "interface:\n  display_name: \"Review\"\npolicy:\n  allow_implicit_invocation: false\n", encoding="utf-8")
            write_skill(cache / "bundled" / "sheets" / "1.1.0" / "skills" / "allowed", "---\nname: allowed\n---\n")
            (cache / "bundled" / "sheets" / "1.1.0" / "skills" / "allowed" / "agents").mkdir()
            (cache / "bundled" / "sheets" / "1.1.0" / "skills" / "allowed" / "agents" / "openai.yaml").write_text(
                "policy:\n  allow_implicit_invocation: true\n", encoding="utf-8")
            write_skill(claude / "skills" / "manual-only", "---\nname: manual-only\ndisable-model-invocation: true\n---\n")
            # The higher version is the one installed, whichever folder was touched last.
            old, new = cache / "bundled" / "sheets" / "1.0.0", cache / "bundled" / "sheets" / "1.1.0"
            os.utime(old, (2000, 2000))
            os.utime(new, (1000, 1000))
            # A cache keyed by commit has no order: there the folder written last is used.
            commits = claude / "plugins" / "cache" / "market" / "notes"
            write_skill(commits / "0a1b2c" / "skills" / "old-note", "---\nname: old-note\n---\n")
            write_skill(commits / "9f8e7d" / "skills" / "note", "---\nname: note\n---\n")
            os.utime(commits / "0a1b2c", (1000, 1000))
            os.utime(commits / "9f8e7d", (2000, 2000))
            result = skill_catalog.catalog({}, home)
        skills = {skill["name"]: skill for skill in result["skills"]}
        self.assertEqual([skill["name"] for skill in result["skills"]],
                         ["skill-installer", "engineering:code-review", "notes:note", "sheets:allowed",
                          "sheets:live-control", "sheets:Sheets"])
        self.assertEqual(skills["skill-installer"],
                         {"name": "skill-installer", "description": "Installs skills", "kind": "system",
                          "agents": ["codex"]})
        self.assertEqual(skills["sheets:Sheets"]["description"], "Edits spreadsheets")
        self.assertEqual(skills["sheets:Sheets"]["kind"], "plugins")
        self.assertEqual(skills["engineering:code-review"]["agents"], ["claude"])
        self.assertTrue(all(rules.valid_skill(name) for name in skills))

    def test_catalog_reads_the_claude_apps_plugins_and_built_in_skills(self):
        def plugin(directory, name):
            (directory / ".claude-plugin").mkdir(parents=True)
            (directory / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")

        with tempfile.TemporaryDirectory(prefix="richi-skills-") as temporary:
            home = Path(temporary)
            app = home / agents.CLAUDE_APP
            rpm = app / "account-1" / "organization-1" / "rpm"
            plugin(rpm / "plugin_A", "engineering")
            write_skill(rpm / "plugin_A" / "skills" / "code-review", "---\nname: code-review\ndescription: Reviews\n---\n")
            (rpm / "plugin_A" / "commands").mkdir()
            (rpm / "plugin_A" / "commands" / "standup.md").write_text("---\ndescription: Writes a standup\n---\n",
                                                                      encoding="utf-8")
            (rpm / "plugin_A" / "commands" / "notes.txt").write_text("not a command", encoding="utf-8")
            # A plugin folder the manifest no longer lists is not installed.
            plugin(rpm / "plugin_B", "removed")
            write_skill(rpm / "plugin_B" / "skills" / "old", "---\nname: old\n---\n")
            (rpm / "manifest.json").write_text(json.dumps({"plugins": [{"id": "plugin_A", "name": "uuid"}]}),
                                               encoding="utf-8")
            built_in = app / "skills-plugin" / "account-1" / "organization-1"
            plugin(built_in, "anthropic-skills")
            write_skill(built_in / "skills" / "docx", "---\nname: docx\ndescription: Word files\n---\n")
            write_skill(built_in / "skills" / "switched-off", "---\nname: switched-off\n---\n")
            write_skill(built_in / "skills" / "setup", "---\nname: setup\ndisable-model-invocation: true\n---\n")
            (built_in / "manifest.json").write_text(json.dumps({"skills": [
                {"name": "docx", "enabled": True}, {"name": "switched-off", "enabled": False},
                {"name": "setup", "enabled": True}]}), encoding="utf-8")
            result = skill_catalog.catalog({}, home)
        skills = {skill["name"]: skill for skill in result["skills"]}
        self.assertEqual(sorted(skills), ["anthropic-skills:docx", "engineering:code-review", "engineering:standup"])
        self.assertEqual(skills["engineering:standup"],
                         {"name": "engineering:standup", "description": "Writes a standup", "kind": "plugins",
                          "agents": ["claude"]})
        self.assertIn(("claude", "plugins", True),
                      [(entry["agent"], entry["kind"], entry["exists"]) for entry in result["roots"]])


class MapAccessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-map-access-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.database = self.root / "memory.sqlite3"
        self.runtime = self.root / "runtime"

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_a_ticket_is_redeemed_once_for_a_private_rotating_secret(self):
        ticket = map_access.issue_ticket(self.database)
        self.assertEqual((self.mode(self.runtime), self.mode(self.runtime / map_access.TICKET_FILE)), (0o700, 0o600))
        self.assertIsNone(map_access.redeem(self.database, "0" * 64))
        self.assertIsNone(map_access.redeem(self.database, "not a ticket"))
        first = map_access.redeem(self.database, ticket)
        self.assertRegex(first, r"\A[0-9a-f]{64}\Z")
        self.assertEqual(self.mode(self.runtime / map_access.SECRET_FILE), 0o600)
        self.assertIsNone(map_access.redeem(self.database, ticket))
        self.assertTrue(map_access.authorized(self.database, first))
        for value in (None, "", "f" * 64, first.upper(), first + "0"):
            with self.subTest(value=value):
                self.assertFalse(map_access.authorized(self.database, value))
        # Opening the map again replaces the secret: an earlier page can no longer write.
        second = map_access.redeem(self.database, map_access.issue_ticket(self.database))
        self.assertNotEqual(second, first)
        self.assertFalse(map_access.authorized(self.database, first))
        self.assertTrue(map_access.authorized(self.database, second))

    def test_expired_or_exposed_files_grant_nothing(self):
        expired = map_access.issue_ticket(self.database, now=time.time() - map_access.TICKET_SECONDS - 1)
        self.assertIsNone(map_access.redeem(self.database, expired))
        self.assertFalse((self.runtime / map_access.TICKET_FILE).exists())
        exposed = map_access.issue_ticket(self.database)
        os.chmod(str(self.runtime / map_access.TICKET_FILE), 0o644)
        self.assertIsNone(map_access.redeem(self.database, exposed))
        value = map_access.redeem(self.database, map_access.issue_ticket(self.database))
        os.chmod(str(self.runtime / map_access.SECRET_FILE), 0o644)
        self.assertFalse(map_access.authorized(self.database, value))
        self.assertIsNone(map_access.secret(self.database))

    def test_the_browser_gets_the_ticket_outside_its_arguments(self):
        opened = []
        environment = {key: value for key, value in os.environ.items() if key != "BROWSER"}
        with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(map_access.sys, "platform", "darwin"):
            map_access.open_browser("http://127.0.0.1:1/", self.database, opened.append)
        self.assertRegex(opened[0], r"\Ahttp://127\.0\.0\.1:1/#ticket=[0-9a-f]{64}\Z")
        self.assertIsNotNone(map_access.redeem(self.database, opened[0].rsplit("=", 1)[1]))
        with mock.patch.object(map_access.sys, "platform", "linux"):
            map_access.open_browser("http://127.0.0.1:1/", self.database, opened.append, wait=0)
        redirect = self.runtime / map_access.REDIRECT_FILE
        self.assertEqual(opened[1], redirect.as_uri())
        self.assertEqual(self.mode(redirect), 0o600)
        link = re.search(r'href="([^"]+)"', redirect.read_text(encoding="utf-8")).group(1)
        self.assertTrue(html.unescape(link).startswith("http://127.0.0.1:1/#ticket="))
        self.assertIsNotNone(map_access.redeem(self.database, html.unescape(link).rsplit("=", 1)[1]))
        with mock.patch.object(map_access, "issue_ticket", side_effect=PermissionError("read-only")):
            map_access.open_browser("http://127.0.0.1:1/", self.database, opened.append)
        self.assertEqual(opened[2], "http://127.0.0.1:1/")

    def test_a_browser_that_cannot_read_the_redirect_still_gets_the_map(self):
        address, redirect = "http://127.0.0.1:1/", (self.runtime / map_access.REDIRECT_FILE).as_uri()
        with mock.patch.object(map_access.sys, "platform", "linux"):
            # A sandboxed browser shows an error for the private file and never redeems the ticket.
            opened, naps = [], []
            self.assertTrue(map_access.open_browser(address, self.database, lambda url: opened.append(url) or True,
                                                    wait=0.05, sleep=naps.append))
            self.assertEqual(opened, [redirect, address])
            # A browser that loads the redirect redeems the ticket: nothing else is opened.
            opened = []

            def redeeming(_seconds):
                ticket = json.loads((self.runtime / map_access.TICKET_FILE).read_text())["ticket"]
                self.assertIsNotNone(map_access.redeem(self.database, ticket))
            map_access.open_browser(address, self.database, lambda url: opened.append(url) or True, wait=60,
                                    sleep=redeeming)
            self.assertEqual(opened, [redirect])
            # No browser at all: there is nobody to wait for.
            opened = []
            self.assertFalse(map_access.open_browser(address, self.database, lambda url: opened.append(url) and False,
                                                     wait=60, sleep=self.fail))
            self.assertEqual(opened, [redirect])


class MapRulesAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-map-rules-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        clean_environment(self, self.root / "home")
        write_skill(self.root / "home" / ".agents" / "skills" / "pr-description",
                    "---\nname: pr-description\ndescription: Writes pull request descriptions.\n---\n")
        repository = self.root / "service"
        repository.mkdir()
        self.database = self.root / "store" / "memory.sqlite3"
        self.database.parent.mkdir()
        conn = memory.connect(self.database, create=True)
        try:
            memory.initialize(conn, self.database)
            memory.project_put(conn, {"id": "service", "name": "Service", "repo_path": str(repository)})
        finally:
            conn.close()
        self.server = serve.LoopbackHTTPServer(("127.0.0.1", 0), serve.handler_for(self.database))
        self.server.daemon_threads = True
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.origin = "http://127.0.0.1:%d" % self.port

    def request(self, method, path, body=None, headers=None, origin=True, raw=None):
        sent = {"Origin": self.origin} if origin else {}
        if body is not None or raw is not None:
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        data = raw if raw is not None else json.dumps(body).encode("utf-8") if body is not None else None
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            conn.request(method, path, body=data, headers=sent)
            response = conn.getresponse()
            payload = response.read()
            return response.status, json.loads(payload) if payload else None
        finally:
            conn.close()

    def secret(self):
        status, payload = self.request("POST", "/api/session", {"ticket": map_access.issue_ticket(self.database)})
        self.assertEqual(status, 200, payload)
        return payload["secret"]

    def change(self, secret, expected=200, **body):
        status, payload = self.request("POST", "/api/rules", body, {map_access.HEADER: secret} if secret else {})
        self.assertEqual(status, expected, payload)
        return payload

    def listed(self, secret=None):
        status, payload = self.request("GET", "/api/rules", headers={map_access.HEADER: secret} if secret else {})
        self.assertEqual(status, 200, payload)
        return payload

    def test_rules_change_only_with_the_edit_secret(self):
        empty = self.listed()
        self.assertEqual((empty["rules"], empty["editing"]), ([], False))
        self.assertEqual((empty["limit_characters"], empty["limit_text"]), (rules.MAX_TOTAL, rules.MAX_TEXT))
        rule = {"action": "add", "id": "pr-skill", "scope": "global", "text": "Use it.", "skill": "pr-description"}
        self.assertEqual(self.change(None, 403, **rule)["code"], "not_editing")
        self.assertEqual(self.change("f" * 64, 403, **rule)["code"], "not_editing")
        self.assertEqual(self.listed()["rules"], [])
        secret = self.secret()
        self.assertTrue(self.listed(secret)["editing"])
        self.assertFalse(self.listed()["editing"])
        added = self.change(secret, **rule)["rule"]
        self.assertEqual((added["skill"], added["source"], added["scope"]), ("pr-description", "richi map", "global"))
        # Text is passed on stdin and other values attached to their options: none is read as an option.
        tricky = "--global\n'single' \"double\" $HOME `date` -"
        project = self.change(secret, action="add", id="naming", scope="project", project="service", text=tricky)["rule"]
        self.assertEqual((project["text"], project["project_id"], project["skill"]), (tricky, "service", None))
        self.assertEqual(self.change(secret, 409, **rule)["code"], "exists")
        self.assertEqual(self.change(secret, 404, action="add", id="other", scope="project", project="missing",
                                     text="x")["code"], "not_found")
        stale = self.change(secret, 409, action="update", id="pr-skill", expected_updated_at="2000-01-01T00:00:00Z",
                            text="Lost.")
        self.assertEqual(stale["code"], "conflict")
        updated = self.change(secret, action="update", id="pr-skill", expected_updated_at=added["updated_at"],
                              text="Use it for pull requests.", skill="")["rule"]
        self.assertEqual((updated["text"], updated["skill"], updated["source"]),
                         ("Use it for pull requests.", None, "richi map"))
        self.change(secret, 400, action="update", id="pr-skill", expected_updated_at=updated["updated_at"])
        # Agents may each get their own skill; one left empty uses none.
        own = self.change(secret, action="update", id="pr-skill", expected_updated_at=updated["updated_at"],
                          skill=None, skills={"claude": "engineering:code-review", "codex": ""})["rule"]
        self.assertEqual((own["skill"], own["skills"]), (None, {"claude": "engineering:code-review"}))
        shared = self.change(secret, action="update", id="pr-skill", expected_updated_at=own["updated_at"],
                             skills={"claude": "pr-description", "codex": "pr-description"})["rule"]
        self.assertEqual((shared["skill"], shared["skills"]), ("pr-description", None))
        none = self.change(secret, action="update", id="pr-skill", expected_updated_at=shared["updated_at"],
                           skill="", skills={"claude": None})["rule"]
        self.assertEqual((none["skill"], none["skills"]), (None, None))
        self.change(secret, action="remove", id="pr-skill", expected_updated_at=none["updated_at"])
        self.assertEqual([item["id"] for item in self.listed(secret)["rules"]], ["naming"])

    def test_change_requests_must_be_same_origin_json(self):
        secret = self.secret()
        header = {map_access.HEADER: secret}
        body = {"action": "add", "id": "a", "scope": "global", "text": "x"}
        for name, options, expected in (
                ("no origin", {"origin": False}, 403),
                ("other origin", {"headers": dict(header, Origin="http://evil.test")}, 403),
                ("cross-site", {"headers": dict(header, **{"Sec-Fetch-Site": "cross-site"})}, 403),
                ("other host", {"headers": dict(header, Host="evil.test:%d" % self.port)}, 403),
                ("form post", {"headers": dict(header, **{"Content-Type": "text/plain"})}, 415),
                ("invalid json", {"headers": header, "raw": b"{"}, 400),
                ("too large", {"headers": header, "raw": b" " * (serve.RULES_BODY_LIMIT + 1)}, 413)):
            with self.subTest(name):
                options = dict(options)
                status, _payload = self.request("POST", "/api/rules", None if "raw" in options else body, **options)
                self.assertEqual(status, expected)
        self.assertEqual(self.request("POST", "/api/rules", {**body, "scope": "project", "project": "a\nb"},
                                      header)[0], 404)
        for invalid in ({**body, "unexpected": 1}, {**body, "id": "-x"}, {**body, "scope": "everywhere"},
                        {**body, "skill": "two words"},
                        {**body, "skills": {"gemini": "x"}}, {**body, "skills": {"Not An Agent": "x"}}, {**body, "skills": {"codex": "two words"}},
                        {**body, "skills": ["codex"]}, {**body, "skills": {"codex": 5}},
                        {**body, "text": "x" * (rules.MAX_TEXT + 1)}, {**body, "text": 5},
                        {"action": "drop", "id": "a"}, ["not", "an", "object"]):
            with self.subTest(invalid=invalid):
                status, payload = self.request("POST", "/api/rules", invalid, header)
                self.assertEqual(status, 400, payload)
        for method, path in (("PUT", "/api/rules"), ("DELETE", "/api/rules"), ("POST", "/api/graph"),
                             ("POST", "/api/rules?x=1")):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path, body, header)[0], 405)
        self.assertEqual(self.listed(secret)["rules"], [])

    def test_an_edit_ticket_works_once_and_only_same_origin(self):
        ticket = map_access.issue_ticket(self.database)
        self.assertEqual(self.request("POST", "/api/session", {"ticket": ticket}, origin=False)[0], 403)
        status, payload = self.request("POST", "/api/session", {"ticket": ticket})
        self.assertEqual(status, 200, payload)
        status, payload = self.request("POST", "/api/session", {"ticket": ticket})
        self.assertEqual((status, payload["code"]), (403, "ticket_invalid"))
        self.assertEqual(self.request("POST", "/api/session", ["list"])[0], 403)

    def agent_change(self, secret, expected=200, **body):
        status, payload = self.request("POST", "/api/agents", body, {map_access.HEADER: secret} if secret else {})
        self.assertEqual(status, expected, payload)
        return payload

    def agents(self, secret=None):
        status, payload = self.request("GET", "/api/agents", headers={map_access.HEADER: secret} if secret else {})
        self.assertEqual(status, 200, payload)
        return payload

    def test_agents_change_only_with_the_edit_secret(self):
        home = self.root / "home"
        write_skill(home / ".gemini" / "skills" / "review", "---\nname: review\n---\n")
        listed = self.agents()
        self.assertEqual(([agent["id"] for agent in listed["agents"]], listed["editing"], listed["problem"]),
                         (["claude", "codex"], False, None))
        self.assertIn("gemini-cli", [preset["id"] for preset in listed["presets"] if preset["exists"]])
        agent = {"action": "add", "id": "gemini-cli", "name": "Gemini CLI", "folders": ["~/.gemini/skills"]}
        self.assertEqual(self.agent_change(None, 403, **agent)["code"], "not_editing")
        self.assertEqual(self.agent_change("f" * 64, 403, **agent)["code"], "not_editing")
        self.assertFalse(agents.settings_path().exists())
        secret = self.secret()
        self.assertTrue(self.agents(secret)["editing"])
        added = self.agent_change(secret, **agent)
        self.assertEqual((added["results"], added["agent"]["skills"], added["agent"]["builtin"]),
                         ([{"id": "gemini-cli", "status": "created"}], 1, False))
        # One answer carries everything the page shows: the agents and the skill lists.
        self.assertEqual([item["id"] for item in added["listing"]["agents"]], ["claude", "codex", "gemini-cli"])
        self.assertEqual(sorted(skill["name"] for skill in added["listing"]["catalog"]["skills"]),
                         ["pr-description", "review"])
        self.assertEqual(sorted(skill["name"] for skill in self.agents()["catalog"]["skills"]),
                         ["pr-description", "review"])
        self.assertEqual(self.agent_change(secret, 409, **agent)["code"], "exists")
        # The skill list and the rules follow the registry at once.
        catalog = self.request("GET", "/api/skills")[1]
        self.assertEqual(catalog["agents"][-1], {"id": "gemini-cli", "name": "Gemini CLI"})
        self.assertEqual({skill["name"]: skill["agents"] for skill in catalog["skills"]},
                         {"pr-description": ["codex"], "review": ["gemini-cli"]})
        rule = self.change(secret, action="add", id="review", scope="global", text="Review.",
                           skills={"gemini-cli": "review", "codex": "pr-description"})["rule"]
        self.assertEqual(rule["skills"], {"codex": "pr-description", "gemini-cli": "review"})
        self.assertEqual(self.listed()["agents"][-1], {"id": "gemini-cli", "name": "Gemini CLI"})
        version = added["agent"]["updated_at"]
        self.assertEqual(self.agent_change(secret, 409, action="update", id="gemini-cli", name="Late")["code"], "conflict")
        renamed = self.agent_change(secret, action="update", id="gemini-cli", expected_updated_at=version,
                                    name="Gemini", variable="GEMINI_CLI=1")["agent"]
        self.assertEqual((renamed["name"], renamed["variables"]), ("Gemini", ["GEMINI_CLI=1"]))
        # A built-in agent takes adjustments of its skill sources only.
        adjusted = self.agent_change(secret, action="update", id="codex", disable=["agents-skills"])["agent"]
        self.assertEqual([(source["key"], source["enabled"]) for source in adjusted["sources"]][0], ("agents-skills", False))
        self.assertEqual(self.agent_change(secret, 400, action="update", id="codex", name="Mine",
                                           expected_updated_at=adjusted["updated_at"])["code"], "builtin")
        self.assertEqual(self.agent_change(secret, 404, action="update", id="codex", enable=["nothing"],
                                           expected_updated_at=adjusted["updated_at"])["code"], "not_found")
        reset = self.agent_change(secret, action="remove", id="codex", expected_updated_at=adjusted["updated_at"])
        self.assertEqual((reset["results"][0]["status"], reset["agent"]["updated_at"]), ("reset", None))
        self.assertEqual(self.agent_change(secret, action="remove", id="gemini-cli",
                                           expected_updated_at=renamed["updated_at"])["agent"], None)
        self.assertEqual([agent["id"] for agent in self.agents()["agents"]], ["claude", "codex"])
        for invalid in ({"action": "add", "id": "x", "name": "X", "folders": ["relative"]},
                        {"action": "add", "id": "x", "name": "X", "folders": ["~/x"], "unexpected": 1},
                        {"action": "add", "id": "Bad ID", "name": "X", "folders": ["~/x"]},
                        {"action": "update", "id": "claude", "folders": ["~/x"]},
                        {"action": "remove", "id": "claude", "name": "x"}, {"action": "drop", "id": "x"}, ["list"]):
            with self.subTest(invalid=invalid):
                status, payload = self.request("POST", "/api/agents", invalid, {map_access.HEADER: secret})
                self.assertEqual(status, 400, payload)
        # The same origin checks as for rules guard agent changes.
        header = {map_access.HEADER: secret}
        for options, expected in (({"origin": False}, 403), ({"headers": dict(header, Origin="http://evil.test")}, 403),
                                  ({"headers": dict(header, **{"Content-Type": "text/plain"})}, 415)):
            with self.subTest(options=options):
                self.assertEqual(self.request("POST", "/api/agents", agent, **options)[0], expected)
        self.assertEqual(self.request("PUT", "/api/agents", agent, header)[0], 405)
        self.assertEqual(self.request("POST", "/api/agents?x=1", agent, header)[0], 405)

    def test_health_and_installed_skills(self):
        status, health = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertIn("rules_editor", health["capabilities"])
        self.assertIn("agents_editor", health["capabilities"])
        status, catalog = self.request("GET", "/api/skills")
        self.assertEqual(status, 200)
        self.assertEqual(catalog["skills"], [{"name": "pr-description", "description": "Writes pull request descriptions.",
                                              "kind": "skills", "agents": ["codex"]}])


if __name__ == "__main__":
    unittest.main()
