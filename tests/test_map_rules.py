"""The map changes standing rules only from a page opened by richi map; synthetic stores and homes only."""
from contextlib import closing
import errno
import html
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from richi import agent_commands, agents, map_access, memory, private_files, rules, serve, skill_catalog
from richi_launcher import config


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
    agents.forget_settings()
    test.addCleanup(agents.forget_settings)


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
        # A comment after a value is not part of it; a "#" inside a word or inside quotes is.
        commented = ("---\nname: review # the name\ndisable-model-invocation: true # user only\n"
                     "description: 'Fixes #1' # why\nlink: a#b\nempty: # nothing\nquoted: \"it \\\\\" # tail\n---\n")
        self.assertEqual(skill_catalog.front_matter(commented),
                         {"name": "review", "disable-model-invocation": "true", "description": "Fixes #1",
                          "link": "a#b", "empty": "", "quoted": "it \\\\"})
        # Only the beginning of a long file is read: what it holds still counts.
        self.assertEqual(skill_catalog.front_matter("---\nname: a\ndisable-model-invocation: true\nnotes: cut he", cut=True),
                         {"name": "a", "disable-model-invocation": "true"})
        # The last line read may be cut in two: half a name, half a character or half a "true".
        for tail in ("name: my-sk", "name: caf\ufffd", "disable-model-invocation: tr"):
            with self.subTest(tail=tail):
                self.assertEqual(skill_catalog.front_matter("---\ndescription: d\n" + tail, cut=True), {"description": "d"})
        # A file that opens with a Markdown rule and runs on has no header, however long it is.
        prose = "---\nSome text, no field.\nname: from-body\ndisable-model-invocation: true\n"
        self.assertEqual((skill_catalog.front_matter(prose, cut=True), skill_catalog.front_matter(prose)), ({}, {}))
        # A value may begin on the line after its key; a nested mapping there is not a value.
        below = ("---\nname: x\ndescription:\n  text on the next line\n  and more\ndisable-model-invocation:\n"
                 "  true\nmetadata:\n  author: y\nlist:\n  - a\n---\n")
        self.assertEqual(skill_catalog.front_matter(below), {"name": "x", "description": "text on the next line and more",
                                                             "disable-model-invocation": "true"})
        # Only a space or a tab before "#" starts a comment; text after a closing quote keeps the value whole.
        spaced = "---\na: n\u00b0\u00a0#12 du ticket\nb: \u4f7f\u7528\u3000#tag\nc: x\t# tab\nd: \"a\" b\n---\n"
        self.assertEqual(skill_catalog.front_matter(spaced), {"a": "n\u00b0\u00a0#12 du ticket", "b": "\u4f7f\u7528\u3000#tag",
                                                              "c": "x", "d": '"a" b'})

    def test_a_skill_only_the_user_invokes_stays_out_however_its_header_is_written(self):
        manual = "disable-model-invocation: true"
        with tempfile.TemporaryDirectory(prefix="richi-skills-") as temporary:
            home = Path(temporary)
            claude, codex = home / ".claude" / "skills", home / ".agents" / "skills"
            write_skill(claude / "plain-manual", "---\nname: plain-manual\n" + manual + "\n---\n")
            write_skill(claude / "bom-manual", "\ufeff---\nname: bom-manual\n" + manual + "\n---\n")
            write_skill(claude / "comment-manual", "---\nname: comment-manual\n" + manual + " # user only\n---\n")
            # A header longer than what is read of the file.
            write_skill(claude / "long-manual", "---\nname: long-manual\n" + manual + "\nnotes: >\n"
                        + "  note\n" * skill_catalog.MAX_HEADER + "---\n")
            write_skill(claude / "bom-offered", "\ufeff---\nname: bom-offered\ndescription: With a mark\n---\n")
            write_skill(claude / "folder-name", "---\nname: named # not the folder's name\n---\n")
            write_skill(codex / "codex-manual", "---\nname: codex-manual\n---\n")
            (codex / "codex-manual" / "agents").mkdir()
            (codex / "codex-manual" / "agents" / "openai.yaml").write_text(
                "\ufeffpolicy:\n  allow_implicit_invocation: false # the user asks for it\n", encoding="utf-8")
            result = skill_catalog.catalog({}, home)
            self.assertEqual({skill["name"]: skill["description"] for skill in result["skills"]},
                             {"bom-offered": "With a mark", "named": ""})
            # Nothing beyond the bound is read: a field after it is not seen, the ones before it are.
            write_skill(claude / "late", "---\nname: early\npadding: " + "x" * 100 + "\ndescription: Late\n---\n")
            with mock.patch.object(skill_catalog, "MAX_HEADER", 64):
                bounded = {skill["name"]: skill["description"] for skill in skill_catalog.catalog({}, home)["skills"]}
            self.assertEqual(bounded.get("early"), "")
            self.assertEqual({skill["name"]: skill["description"] for skill in skill_catalog.catalog({}, home)["skills"]}
                             ["early"], "Late")

    def test_a_full_list_is_cut_while_every_agent_is_still_counted(self):
        with tempfile.TemporaryDirectory(prefix="richi-skills-") as temporary:
            home = Path(temporary)
            for name in ("a1", "a2", "a3", "a4"):
                write_skill(home / ".claude" / "skills" / name, "---\nname: " + name + "\n---\n")
            for name in ("a1", "c1", "c2"):
                write_skill(home / ".agents" / "skills" / name, "---\nname: " + name + "\n---\n")
            full = skill_catalog.catalog({}, home)
            with mock.patch.object(skill_catalog, "MAX_SKILLS", 3):
                cut = skill_catalog.catalog({}, home)
                listed = {agent["id"]: (agent["skills"], [source["skills"] for source in agent["sources"]][0])
                          for agent in agent_commands.listing({}, home)["agents"]}
        self.assertEqual((len(full["skills"]), full["truncated"], full["totals"]), (6, False, {"claude": 4, "codex": 3}))
        self.assertEqual(([skill["name"] for skill in cut["skills"]], cut["truncated"]), (["a1", "a2", "a3"], True))
        # The agent read second is not left with nothing: its skills are counted, and a skill that
        # made the list names every agent that has it.
        self.assertEqual(cut["totals"], {"claude": 4, "codex": 3})
        self.assertEqual(cut["skills"][0]["agents"], ["claude", "codex"])
        self.assertEqual(listed, {"claude": (4, 4), "codex": (3, 3)})

    def test_a_plugin_manifest_made_to_break_the_parser_is_passed_over(self):
        with tempfile.TemporaryDirectory(prefix="richi-skills-") as temporary:
            home = Path(temporary)
            rpm = home / agents.CLAUDE_APP / "account-1" / "organization-1" / "rpm"
            for name, manifest in (("deep", "[" * 100000 + "]" * 100000), ("fine", json.dumps({"name": "engineering"})),
                                   ("long", '{"name": "long", "version": ' + "7" * 200 + "}")):
                (rpm / name / ".claude-plugin").mkdir(parents=True)
                (rpm / name / ".claude-plugin" / "plugin.json").write_text(manifest, encoding="utf-8")
                write_skill(rpm / name / "skills" / "review", "---\nname: review\n---\n")
            (rpm / "manifest.json").write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
            # What a plugin's author wrote must not take the lists down.
            result = skill_catalog.catalog({}, home)
            self.assertEqual([skill["name"] for skill in result["skills"]], ["engineering:review"])
            self.assertEqual(agent_commands.listing({}, home)["agents"][0]["skills"], 1)

    def test_versions_count_as_numbers_and_codex_lists_no_plugin_commands(self):
        with tempfile.TemporaryDirectory(prefix="richi-skills-") as temporary:
            home = Path(temporary)
            cache = home / ".codex" / "plugins" / "cache" / "market" / "tools"
            write_skill(cache / "1.9.0" / "skills" / "old", "---\nname: old\n---\n")
            write_skill(cache / "1.10.0" / "skills" / "new", "---\nname: new\n---\n")
            (cache / "1.10.0" / "commands").mkdir()
            (cache / "1.10.0" / "commands" / "run.md").write_text("---\ndescription: A command\n---\n", encoding="utf-8")
            # Two sources of one agent that give the same skill count it once.
            write_skill(home / ".agents" / "skills" / "twice", "---\nname: twice\n---\n")
            write_skill(home / ".codex" / "skills" / "twice", "---\nname: twice\n---\n")
            result = skill_catalog.catalog({}, home)
        self.assertEqual(sorted(skill["name"] for skill in result["skills"]), ["tools:new", "twice"])
        self.assertEqual(result["totals"]["codex"], 2)

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
        self.ticket, self.key = map_access.ticket_path(self.database), map_access.secret_path(self.database)

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_a_ticket_is_redeemed_once_for_a_private_rotating_secret(self):
        ticket = map_access.issue_ticket(self.database)
        self.assertEqual((self.mode(self.runtime), self.mode(self.ticket), self.ticket.parent), (0o700, 0o600, self.runtime))
        self.assertIsNone(map_access.redeem(self.database, "0" * 64))
        self.assertIsNone(map_access.redeem(self.database, "not a ticket"))
        first = map_access.redeem(self.database, ticket)
        self.assertRegex(first, r"\A[0-9a-f]{64}\Z")
        self.assertEqual(self.mode(self.key), 0o600)
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
        self.assertFalse(self.ticket.exists())
        exposed = map_access.issue_ticket(self.database)
        os.chmod(str(self.ticket), 0o644)
        self.assertIsNone(map_access.redeem(self.database, exposed))
        value = map_access.redeem(self.database, map_access.issue_ticket(self.database))
        os.chmod(str(self.key), 0o644)
        self.assertFalse(map_access.authorized(self.database, value))
        self.assertIsNone(map_access.secret(self.database))

    def test_stores_in_one_directory_do_not_share_the_ticket_or_the_secret(self):
        other = self.root / "other.sqlite3"
        first = map_access.redeem(self.database, map_access.issue_ticket(self.database))
        ticket = map_access.issue_ticket(other)
        # Another store's ticket opens nothing here, and stays good for its own map.
        self.assertIsNone(map_access.redeem(self.database, ticket))
        second = map_access.redeem(other, ticket)
        self.assertRegex(second, r"\A[0-9a-f]{64}\Z")
        # Opening the other store's map leaves this one's page editing.
        self.assertEqual((map_access.authorized(self.database, first), map_access.authorized(self.database, second),
                          map_access.authorized(other, second), map_access.authorized(other, first)),
                         (True, False, True, False))
        self.assertNotEqual(map_access.redirect_path(self.database), map_access.redirect_path(other))
        # The same store under another spelling of its path is the same store.
        link = self.root / "link"
        link.symlink_to(self.root)
        self.assertTrue(map_access.authorized(link / "memory.sqlite3", first))

    def test_a_ticket_is_spent_only_for_a_secret_that_was_given(self):
        before = map_access.redeem(self.database, map_access.issue_ticket(self.database))
        ticket = map_access.issue_ticket(self.database)
        # The secret cannot be written: the failure is the caller's to report, and the ticket is still good.
        with mock.patch.object(private_files, "replace", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(OSError):
                map_access.redeem(self.database, ticket)
        self.assertTrue(self.ticket.exists())
        self.assertTrue(map_access.authorized(self.database, before))
        after = map_access.redeem(self.database, ticket)
        self.assertEqual((map_access.authorized(self.database, after), map_access.authorized(self.database, before),
                          self.ticket.exists()), (True, False, False))
        # Without a place for the files there is no ticket to redeem.
        self.assertIsNone(map_access.redeem(self.root / "nowhere" / "memory.sqlite3", "0" * 64))

    def test_a_new_ticket_waits_while_the_previous_one_is_redeemed(self):
        first = map_access.issue_ticket(self.database)
        issued, reading = [], threading.Event()
        read = map_access._read_private

        def slow_read(path):
            # The page's request has read the ticket; a second `richi map` now issues another one.
            value = read(path)
            if not reading.is_set():
                reading.set()
                issuer.start()
                issuer.join(0.5)
                self.assertTrue(issuer.is_alive(), "a ticket was issued in the middle of redeeming another")
            return value
        issuer = threading.Thread(target=lambda: issued.append(map_access.issue_ticket(self.database)), daemon=True)
        with mock.patch.object(map_access, "_read_private", slow_read):
            self.assertIsNotNone(map_access.redeem(self.database, first))
        issuer.join(10)
        # The ticket issued meanwhile was not deleted for the first one: its page gets its secret.
        self.assertEqual(len(issued), 1)
        self.assertIsNotNone(map_access.redeem(self.database, issued[0]))

    def test_a_key_file_that_cannot_be_read_now_is_not_taken_for_no_key(self):
        value = map_access.redeem(self.database, map_access.issue_ticket(self.database))
        real_open = os.open

        def exhausted(path, *arguments, **options):
            if str(path) == str(self.key):
                raise OSError(errno.EMFILE, "Too many open files")
            return real_open(path, *arguments, **options)
        # "No key" would make the page drop a key that still works.
        with mock.patch.object(map_access.os, "open", exhausted):
            with self.assertRaises(OSError):
                map_access.authorized(self.database, value)
        self.assertTrue(map_access.authorized(self.database, value))
        # A link is no key file: nothing is followed.
        self.key.unlink()
        self.key.symlink_to(self.root / "elsewhere")
        self.assertIsNone(map_access.secret(self.database))

    def test_forgetting_the_key_ends_editing_for_every_page(self):
        value = map_access.redeem(self.database, map_access.issue_ticket(self.database))
        map_access.forget_secret(self.database)
        self.assertFalse(map_access.authorized(self.database, value))
        map_access.forget_secret(self.database)
        self.assertFalse(map_access.secret_path(self.database).exists())

    def test_a_ticket_lock_held_too_long_is_an_error_not_a_hang(self):
        ticket = map_access.issue_ticket(self.database)
        with private_files.locked(self.ticket), mock.patch.object(map_access, "LOCK_SECONDS", 0.2):
            with self.assertRaises(TimeoutError):
                map_access.redeem(self.database, ticket)
            with self.assertRaises(TimeoutError):
                map_access.issue_ticket(self.database)
        self.assertIsNotNone(map_access.redeem(self.database, ticket))

    def test_the_browser_gets_the_ticket_outside_its_arguments(self):
        address, redirect, opened = "http://127.0.0.1:1/", map_access.redirect_path(self.database), []
        with_ticket = r"\Ahttp://127\.0\.0\.1:1/#ticket=[0-9a-f]{64}\Z"

        def opens(url):
            opened.append(url)
            return True

        def refuses_first(url):
            opened.append(url)
            return len(opened) > 1
        environment = {key: value for key, value in os.environ.items() if key != "BROWSER"}
        with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(map_access.sys, "platform", "darwin"):
            # macOS hands the URL to its own controller on standard input, so the ticket may go in it.
            self.assertTrue(map_access.open_browser(address, self.database, opens))
            self.assertRegex(opened[0], with_ticket)
            self.assertEqual(len(opened), 1)
            self.assertIsNotNone(map_access.redeem(self.database, opened[0].rsplit("=", 1)[1]))
            # When that controller fails, the ticket does not go on to a browser that takes it as an argument.
            del opened[:]
            self.assertTrue(map_access.open_browser(address, self.database, refuses_first, wait=0))
            self.assertRegex(opened[0], with_ticket)
            self.assertEqual(opened[1:], [redirect.as_uri()])
            with mock.patch.object(map_access.webbrowser, "get",
                                   side_effect=map_access.webbrowser.Error("no controller")) as controller:
                self.assertFalse(map_access._tell_system_browser(address))
            controller.assert_called_once_with("MacOSX")
            # The user's own BROWSER is a command line again.
            del opened[:]
            with mock.patch.dict(os.environ, {"BROWSER": "firefox"}):
                map_access.open_browser(address, self.database, opens, wait=0)
            self.assertEqual(opened, [redirect.as_uri()])
        del opened[:]
        with mock.patch.object(map_access.sys, "platform", "linux"):
            map_access.open_browser(address, self.database, opens, wait=0)
        self.assertEqual(opened, [redirect.as_uri()])
        self.assertEqual(self.mode(redirect), 0o600)
        link = re.search(r'href="([^"]+)"', redirect.read_text(encoding="utf-8")).group(1)
        self.assertTrue(html.unescape(link).startswith("http://127.0.0.1:1/#ticket="))
        self.assertIsNotNone(map_access.redeem(self.database, html.unescape(link).rsplit("=", 1)[1]))
        del opened[:]
        problems = []
        with mock.patch.object(map_access, "issue_ticket", side_effect=PermissionError(13, "Permission denied")):
            map_access.open_browser(address, self.database, opens, problems=problems)
        self.assertEqual(opened, [address])
        # richi map says why the page it opened cannot save.
        self.assertIn("Permission denied", problems[0])

    def test_a_browser_that_cannot_read_the_redirect_still_gets_the_map(self):
        address, redirect = "http://127.0.0.1:1/", map_access.redirect_path(self.database).as_uri()
        with mock.patch.object(map_access.sys, "platform", "linux"):
            # A sandboxed browser shows an error for the private file and never redeems the ticket.
            opened, naps = [], []
            self.assertTrue(map_access.open_browser(address, self.database, lambda url: opened.append(url) or True,
                                                    wait=0.05, sleep=naps.append))
            self.assertEqual(opened, [redirect, address])
            # A browser that loads the redirect redeems the ticket: nothing else is opened.
            opened = []

            def redeeming(_seconds):
                ticket = json.loads(self.ticket.read_text())["ticket"]
                self.assertIsNotNone(map_access.redeem(self.database, ticket))
            map_access.open_browser(address, self.database, lambda url: opened.append(url) or True, wait=60,
                                    sleep=redeeming)
            self.assertEqual(opened, [redirect])
            # No browser at all: there is nobody to wait for.
            opened = []
            self.assertFalse(map_access.open_browser(address, self.database, lambda url: opened.append(url) and False,
                                                     wait=60, sleep=self.fail))
            self.assertEqual(opened, [redirect])


class MapKeyLifetimeTests(unittest.TestCase):
    """The edit key ends with the map process; the process that replaces a map keeps it."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-map-key-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        clean_environment(self, self.root / "home")
        self.database = self.root / "store" / "memory.sqlite3"
        self.database.parent.mkdir()
        conn = memory.connect(self.database, create=True)
        try:
            memory.initialize(conn, self.database)
        finally:
            conn.close()

    def serve(self, **environment):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen([sys.executable, "-m", "richi.serve", "--db", str(self.database), "--port", str(port)],
                                   env=dict(os.environ, **environment), stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        self.addCleanup(self.stop, process)
        deadline = time.monotonic() + 30
        while serve.read_health(port) is None:
            if process.poll() is not None or time.monotonic() > deadline:
                self.fail("the map did not start: " + (process.stdout.read() if process.poll() is not None else ""))
            time.sleep(0.1)
        return process

    def stop(self, process):
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(30)
        process.stdout.close()

    def test_a_new_map_process_ends_the_previous_key_and_so_does_a_stop(self):
        earlier = map_access.redeem(self.database, map_access.issue_ticket(self.database))
        process = self.serve()
        # A page left open from the map before holds a key nobody may use now.
        self.assertFalse(map_access.authorized(self.database, earlier))
        current = map_access.redeem(self.database, map_access.issue_ticket(self.database))
        self.assertTrue(map_access.authorized(self.database, current))
        self.stop(process)
        # Whatever answers on the port next must not be handed a key that still works.
        self.assertFalse(map_access.authorized(self.database, current))
        # A process that replaces its map (a reload, a hand-off) keeps the key of its pages.
        kept = map_access.redeem(self.database, map_access.issue_ticket(self.database))
        self.serve(**{serve.RESTARTED_ENV: "1"})
        self.assertTrue(map_access.authorized(self.database, kept))


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
        # The page never chooses the source, and every change is recorded as the map's.
        self.assertEqual(self.change(secret, action="add", id="sourced", scope="global", text="x",
                                     source="a chat")["rule"]["source"], "richi map")
        renamed = self.change(secret, action="update", id="naming", expected_updated_at=project["updated_at"],
                              text="Names.", source="someone else")["rule"]
        self.assertEqual((renamed["source"], renamed["changed_by"]), ("richi map", "richi map"))
        self.change(secret, 400, action="update", id="naming", expected_updated_at=renamed["updated_at"], source="x")

    def test_a_worker_is_fed_and_read_within_one_budget(self):
        request = b"x" * 1_000_000

        def worker(code):
            return subprocess.Popen([sys.executable, "-I", "-c", code], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # A worker that reads everything gets all of it, although it is far more than a pipe holds.
        status, output, _error = serve._read_worker_output(
            worker("import sys; sys.stdout.write(str(len(sys.stdin.buffer.read())))"),
            label="test", timeout=30, maximum=100, request=request)
        self.assertEqual((status, output), (0, b"1000000"))
        # One that answers without reading is heard, not replaced by a broken pipe.
        status, output, error = serve._read_worker_output(
            worker("import sys; sys.stderr.write('refused'); sys.exit(3)"),
            label="test", timeout=30, maximum=100, request=request)
        self.assertEqual((status, output, error), (3, b"", b"refused"))
        # One that neither reads nor answers costs the budget, not the thread.
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            serve._read_worker_output(worker("import time; time.sleep(60)"), label="test", timeout=1, maximum=100,
                                      request=request)
        self.assertLess(time.monotonic() - started, 10)
        # No request at all still closes the worker's input.
        status, output, _error = serve._read_worker_output(
            worker("import sys; sys.stdout.write(repr(sys.stdin.read()))"), label="test", timeout=30, maximum=100,
            request=b"")
        self.assertEqual((status, output), (0, b"''"))

    def test_the_rules_answer_carries_what_the_form_needs(self):
        secret = self.secret()
        listing = self.listed()
        self.assertEqual((listing["projects"], listing["agents"]),
                         ([{"id": "service", "name": "Service"}],
                          [{"id": "claude", "name": "Claude Code"}, {"id": "codex", "name": "Codex"}]))
        self.assertNotIn("invalid_rules", listing)
        self.change(secret, action="add", id="kept", scope="global", text="Stays.")
        with closing(sqlite3.connect(str(self.database))) as conn, conn:
            conn.execute("INSERT INTO metadata(key, value) VALUES ('rule:broken', '{not json')")
        # A damaged row does not take the list down: it is named, and the page can remove it.
        listing = self.listed(secret)
        self.assertEqual([rule["id"] for rule in listing["rules"]], ["kept"])
        self.assertEqual([(item["id"], item["updated_at"]) for item in listing["invalid_rules"]], [("broken", "invalid")])
        self.assertEqual(self.change(secret, 409, action="remove", id="broken", expected_updated_at="x")["code"], "conflict")
        self.change(secret, action="remove", id="broken", expected_updated_at="invalid")
        self.assertNotIn("invalid_rules", self.listed())

    def test_bodies_made_to_break_the_parser_are_refused(self):
        header = {map_access.HEADER: self.secret()}
        nested = b"[" * 20000 + b"]" * 20000
        lone = "\\ud800"
        for path, raw, expected in (
                ("/api/rules", nested, 400), ("/api/agents", nested, 400),
                ("/api/rules", ('{"action":"add","id":"s","scope":"global","text":"%s"}' % lone).encode(), 400),
                ("/api/rules", ('{"action":"add","id":"s","scope":"global","text":"x","skill":"%s"}' % lone).encode(), 400),
                ("/api/agents", ('{"action":"add","id":"s","name":"%s","folders":["~/x"]}' % lone).encode(), 400),
                ("/api/agents", ('{"action":"add","id":"s","name":"S","folders":["~/%s"]}' % lone).encode(), 400)):
            with self.subTest(path=path, raw=raw[:40]):
                status, payload = self.request("POST", path, headers=header, raw=raw)
                self.assertEqual((status, "error" in payload), (expected, True), payload)
        # A change the rules command refuses is answered with its refusal whatever its size: escaped
        # for the worker, this body is larger than a pipe holds and still within what the command reads.
        def sent_as_typed(letters):
            # A page sends text as UTF-8; the server escapes it for the worker, three times as long here.
            body = {"action": "add", "id": "s", "scope": "global", "text": "x", "unexpected": "я" * letters}
            return self.request("POST", "/api/rules", headers=header, raw=json.dumps(body, ensure_ascii=False).encode("utf-8"))
        status, payload = sent_as_typed(15000)
        self.assertEqual((status, "fields only" in payload["error"]), (400, True), payload)
        # Beyond what the command reads, the server says so itself.
        status, payload = sent_as_typed(30000)
        self.assertEqual((status, payload.get("code")), (413, "limit"), payload)
        # Without the secret a body is not parsed at all, and a ticket is never this long.
        self.assertEqual(self.request("POST", "/api/rules", raw=nested)[0], 403)
        self.assertEqual(self.request("POST", "/api/session", raw=nested)[1]["code"], "ticket_invalid")
        self.assertEqual((self.listed()["rules"], [agent["id"] for agent in self.agents()["agents"]]),
                         ([], ["claude", "codex"]))

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
        # Skill folders behind the lists stay on the server, and only an editing page learns where
        # the settings file is.
        self.assertTrue(all("roots" not in answer["catalog"] for answer in (added["listing"], self.agents(secret))))
        self.assertEqual(("settings_file" in self.agents(), self.agents(secret)["settings_file"],
                          added["listing"]["settings_file"]), (False, str(agents.settings_path()), str(agents.settings_path())))
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

    def test_a_change_that_is_not_one_is_answered_not_dropped(self):
        header = {map_access.HEADER: self.secret()}
        lone = "half \ud800"
        # Each of these once closed the connection without an answer, or blamed the runtime.
        for path, body in (
                ("/api/agents", {"action": ["add"], "id": "s"}), ("/api/agents", {"action": {}, "id": "s"}),
                ("/api/agents", {"action": "add", "id": "s", "name": "S", "folders": ["~/x"], "variable": "A=" + lone}),
                ("/api/agents", {"action": "update", "id": "claude", "disable": [lone]}),
                ("/api/agents", {"action": "update", "id": "claude", "expected_updated_at": lone, "disable": ["skills"]}),
                ("/api/rules", {"action": ["add"], "id": "s"}),
                ("/api/rules", {"action": "remove", "id": lone, "expected_updated_at": "t"}),
                ("/api/rules", {"action": "remove", "id": "s", "expected_updated_at": lone})):
            with self.subTest(path=path, body=body):
                status, payload = self.request("POST", path, body, header)
                self.assertIn(status, (400, 404, 409), payload)
                for word in ("codec", "unhashable", "runtime"):
                    self.assertNotIn(word, payload["error"])
        # A refused change leaves nothing behind, not even the settings directory.
        self.assertFalse(agents.settings_path().parent.exists())
        self.assertEqual((self.listed()["rules"], [agent["id"] for agent in self.agents()["agents"]]),
                         ([], ["claude", "codex"]))

    def test_a_failure_nobody_foresaw_is_answered_too(self):
        header = {map_access.HEADER: self.secret()}
        # The server's log keeps the traceback; here it would only be noise.
        with mock.patch.object(self.server, "handle_error"):
            with mock.patch.object(serve.agent_commands, "listing", side_effect=RuntimeError("inner detail")):
                status, payload = self.request("GET", "/api/agents")
            self.assertEqual((status, payload), (500, {"error": "The map could not answer: RuntimeError",
                                                       "code": "internal"}))
            with mock.patch.object(serve, "agent_change", side_effect=KeyError("inner detail")):
                status, payload = self.request("POST", "/api/agents", {"action": "remove", "id": "x"}, header)
            self.assertEqual((status, payload["code"]), (500, "internal"))
        self.assertEqual(self.request("GET", "/api/agents")[0], 200)

    def test_a_map_that_cannot_write_its_key_says_so(self):
        ticket = map_access.issue_ticket(self.database)
        with mock.patch.object(private_files, "replace", side_effect=PermissionError(13, "Permission denied")):
            status, payload = self.request("POST", "/api/session", {"ticket": ticket})
        self.assertEqual((status, payload["code"]), (503, "unavailable"), payload)
        self.assertIn("Permission denied", payload["error"])
        # The ticket was not spent on a secret nobody got.
        self.assertEqual(self.request("POST", "/api/session", {"ticket": ticket})[0], 200)

    def test_a_change_does_not_wait_behind_readings(self):
        secret = self.secret()
        started, release, answers = threading.Event(), threading.Event(), []
        run = serve._run_rules_request

        def slow_reading(database, operation, *arguments, **options):
            if operation == ["rules", "list"]:
                started.set()
                release.wait(30)
            return run(database, operation, *arguments, **options)
        with mock.patch.object(serve, "_run_rules_request", slow_reading), mock.patch.object(serve, "TURN_TIMEOUT", 1):
            reader = threading.Thread(target=lambda: answers.append(self.request("GET", "/api/rules")), daemon=True)
            reader.start()
            self.assertTrue(started.wait(10))
            # Any client of the port may read; the user's change has a turn of its own.
            self.change(secret, action="add", id="saved", scope="global", text="Saved while a reading waited.")
            # Another reading waits for the first one, and is told to try again when its time is up.
            self.assertEqual(self.request("GET", "/api/rules"), (503, {"error": "The rules are being read; try again",
                                                                       "code": "busy"}))
            release.set()
            reader.join(30)
        self.assertEqual([rule["id"] for rule in answers[0][1]["rules"]], ["saved"])

    def test_a_store_that_another_command_holds_is_busy_not_a_bad_request(self):
        secret = self.secret()
        # What the rules command prints when SQLite gives up waiting for the store.
        code = ("import json, sys; sys.stdin.read(); "
                "sys.stderr.write(json.dumps({'error': 'database is locked', 'code': 'busy'})); sys.exit(1)")
        with mock.patch.object(serve, "runtime_command", return_value=[sys.executable, "-I", "-c", code]):
            payload = self.change(secret, 503, action="add", id="late", scope="global", text="x")
        self.assertEqual(payload, {"error": "database is locked", "code": "busy"})

    def test_a_key_that_cannot_be_read_now_is_not_taken_for_none(self):
        secret = self.secret()
        header = {map_access.HEADER: secret}
        with mock.patch.object(map_access, "secret", side_effect=OSError(errno.EMFILE, "Too many open files")):
            for method, path, body in (("GET", "/api/rules", None), ("GET", "/api/agents", None),
                                       ("POST", "/api/rules", {"action": "add", "id": "x", "scope": "global",
                                                               "text": "x"})):
                with self.subTest(method=method, path=path):
                    status, payload = self.request(method, path, body, header)
                    self.assertEqual((status, payload["code"]), (503, "unavailable"), payload)
        self.assertEqual((self.listed(secret)["editing"], self.listed()["rules"]), (True, []))

    def test_a_failure_is_logged_even_when_the_client_has_gone(self):
        class Gone(serve.handler_for(self.database)):
            def __init__(self):
                pass

            def send_data(self, *_arguments, **_options):
                raise BrokenPipeError("the client closed the connection")

        # What the server's log receives is the failure, not the broken pipe of its answer.
        with self.assertRaises(RuntimeError):
            Gone().answer(lambda: (_ for _ in ()).throw(RuntimeError("inner")))

    def test_nothing_follows_an_answer_already_sent(self):
        class Late(serve.handler_for(self.database)):
            def get(self):
                self.send_data(200, {"ok": True})
                raise RuntimeError("after the answer")
        server = serve.LoopbackHTTPServer(("127.0.0.1", 0), Late)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port, received = server.server_address[1], b""
        with mock.patch.object(server, "handle_error"):
            with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
                client.sendall(b"GET /api/health HTTP/1.0\r\n\r\n")
                while True:
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    received += chunk
        self.assertEqual(received.count(b"HTTP/1.0 "), 1, received)
        self.assertIn(b'{"ok": true}', received)

    def test_an_agents_change_does_not_wait_behind_readings(self):
        secret = self.secret()
        started, release, answers = threading.Event(), threading.Event(), []
        listing = agent_commands.listing

        def slow(*arguments, **options):
            if not started.is_set():
                started.set()
                release.wait(30)
            return listing(*arguments, **options)
        with mock.patch.object(serve.agent_commands, "listing", slow), mock.patch.object(serve, "TURN_TIMEOUT", 2):
            reader = threading.Thread(target=lambda: answers.append(self.request("GET", "/api/agents")), daemon=True)
            reader.start()
            self.assertTrue(started.wait(10))
            added = self.agent_change(secret, action="add", id="quick", name="Quick", folders=["~/team"])
            release.set()
            reader.join(30)
        self.assertEqual((added["results"], answers[0][0]), ([{"id": "quick", "status": "created"}], 200))
        # What a refusal is about travels to the page beside its code.
        refused = self.agent_change(secret, 409, action="update", id="claude", add_folders=["~/.claude/skills"])
        self.assertEqual((refused["code"], refused["subject"]), ("exists", "source"))

    def test_an_agents_change_waits_for_another_only_so_long(self):
        secret = self.secret()
        inside, release = threading.Event(), threading.Event()
        change = serve.agent_change

        def slow(body):
            inside.set()
            release.wait(30)
            return change(body)
        with mock.patch.object(serve, "agent_change", slow), mock.patch.object(serve, "TURN_TIMEOUT", 1):
            first = threading.Thread(target=lambda: self.request("POST", "/api/agents", {
                "action": "add", "id": "first", "name": "First", "folders": ["~/team"]}, {map_access.HEADER: secret}),
                daemon=True)
            first.start()
            self.assertTrue(inside.wait(10))
            refused = self.agent_change(secret, 503, action="add", id="second", name="Second", folders=["~/team"])
            release.set()
            first.join(30)
        self.assertEqual(refused["code"], "busy")

    def test_a_body_that_is_null_or_too_deep_is_answered(self):
        header = {map_access.HEADER: self.secret()}
        # null once left the connection without an answer.
        self.assertEqual(self.request("POST", "/api/session", raw=b"null")[1]["code"], "ticket_invalid")
        status, payload = self.request("POST", "/api/rules", headers=header, raw=b"null")
        self.assertEqual(status, 400)
        self.assertIn("A rule change is an object", payload["error"])
        status, payload = self.request("POST", "/api/agents", headers=header, raw=b"null")
        self.assertEqual((status, payload["code"]), (400, "invalid_request"))
        # Some Pythons parse far deeper than they can write back: no change nests that deep.
        deep = b'{"action":"add","id":"d","scope":"global","text":"x","source":' + b"[" * 40 + b"]" * 40 + b"}"
        for path in ("/api/rules", "/api/agents"):
            with self.subTest(path=path):
                self.assertEqual(self.request("POST", path, headers=header, raw=deep),
                                 (400, {"error": "Invalid JSON", "code": "invalid_request"}))
        self.assertEqual(self.listed()["rules"], [])

    def test_a_reading_page_is_not_told_where_the_settings_are(self):
        secret = self.secret()
        path = agents.settings_path()
        path.parent.mkdir(parents=True)
        for text, reason in (("{not json", "Expecting"),
                             ('{"version":1,"agents":{"x":{"updated_at":"t","name":"X","folders":[{"path":"~/x",'
                              '"enabled":true}],"deep":' + "[" * 100000 + "]" * 100000 + "}}}", "nested too deeply")):
            with self.subTest(reason=reason):
                path.write_text(text, encoding="utf-8")
                agents.forget_settings()
                reading, editing = self.agents(), self.agents(secret)
                # The path names the user; a page that only reads learns that there is a problem, not where.
                self.assertTrue(reading["problem"])
                self.assertNotIn(str(self.root), json.dumps(reading))
                self.assertIn(str(path), editing["problem"])
                self.assertIn(reason, editing["problem"])
                self.assertEqual([agent["id"] for agent in reading["agents"]], ["claude", "codex"])
                refused = self.agent_change(secret, 409, action="update", id="claude", disable=["skills"])
                self.assertEqual((refused["code"], reason in refused["error"]), ("settings", True))
                self.assertEqual(self.listed()["agents"], [{"id": "claude", "name": "Claude Code"},
                                                           {"id": "codex", "name": "Codex"}])
                # The rules answer says so too, and the place only to an editing page.
                self.assertEqual(self.listed()["agent_settings_problem"]["error"], serve.SETTINGS_PROBLEM)
                self.assertIn(str(path), self.listed(secret)["agent_settings_problem"]["error"])

    def test_health_and_installed_skills(self):
        status, health = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertIn("rules_editor", health["capabilities"])
        self.assertIn("agents_editor", health["capabilities"])
        status, catalog = self.request("GET", "/api/skills")
        self.assertEqual(status, 200)
        self.assertEqual(catalog["skills"], [{"name": "pr-description", "description": "Writes pull request descriptions.",
                                              "kind": "skills", "agents": ["codex"]}])
        self.assertNotIn("roots", catalog)
        self.assertEqual(health["store_arguments"], ["--workspace", "default", "--db", str(self.database)])

    def test_a_command_names_the_workspace_only_when_the_name_leads_to_this_store(self):
        own = config.resolve_settings(workspace="default").database
        # The default workspace is named with the file: --db alone is refused in a terminal whose
        # current workspace is a named one.
        by_file = ["--workspace", "default", "--db", str(self.database)]
        self.assertEqual(serve.store_arguments(own, None, "default"), ["--workspace", "default"])
        # A map started with --db keeps the workspace's name and shows another file.
        self.assertEqual(serve.store_arguments(self.database, None, "default"), by_file)
        # What selected storage for the map's own process says nothing about the user's terminal.
        with mock.patch.dict(os.environ, {"RICHI_DB": str(self.database), "RICHI_DATA_DIR": str(self.root)}):
            self.assertEqual(serve.store_arguments(self.database, None, "default"), by_file)
            self.assertEqual(serve.store_arguments(own, None, "default"), ["--workspace", "default"])
            # The launcher resolves a name with the variables it is given, the process's own by default.
            self.assertEqual((config.resolve_settings(workspace="default").database,
                              config.resolve_settings(workspace="default", environ={}).database), (self.database, own))
        settings = self.root / "config.json"
        self.assertEqual(serve.store_arguments(self.database, settings, None, True),
                         ["--config", str(settings), "--db", str(self.database)])
        # An installed launcher older than this runtime cannot resolve the name by itself: the file is named.
        with mock.patch.object(serve, "resolve_settings", lambda workspace=None: config.resolve_settings(workspace=workspace)):
            self.assertEqual(serve.store_arguments(own, None, "default"), ["--workspace", "default", "--db", str(own)])
        for workspace in ("no-such-workspace", "Not A Name", None):
            self.assertEqual(serve.store_arguments(self.database, None, workspace), by_file)


if __name__ == "__main__":
    unittest.main()
