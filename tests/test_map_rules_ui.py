"""The page's own logic for rules and agents: skills per agent, and commands the real CLI accepts."""
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from richi import agents, memory, rules, serve


MAP = serve.BASE / "map.html"
BEGIN, END = "  // rules-agents-pure: begin.", "  // rules-agents-pure: end."
PRELUDE = r"""
const assert = require('node:assert/strict');
const list = value => Array.isArray(value) ? value : [];
const RULE_SOURCE = %s;
"""
TRICKY = "--global\n'single' \"double\" $HOME `date` \\ -"


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser-script regressions")
class RulesPageLogicTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-map-ui-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        environment = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        for key in [key for key in os.environ if key.startswith("RICHI_")
                    or key in {"CODEX_THREAD_ID", "CLAUDECODE", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME"}]:
            del os.environ[key]
        agents._cache.clear()
        self.addCleanup(agents._cache.clear)

    def evaluate(self, script):
        """Run the page's DOM-free lines under Node; what the script prints, as JSON."""
        source = MAP.read_text(encoding="utf-8")
        self.assertIn("RULE_SOURCE='%s'" % serve.RULES_SOURCE, source)
        block = source[source.index(BEGIN):source.index(END)]
        result = subprocess.run([shutil.which("node"), "-e", PRELUDE % json.dumps(serve.RULES_SOURCE) + block + script],
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def run_command(self, command, ok=True, **replaced):
        """Run a command the page composed, as a shell would split it."""
        words = shlex.split(command)
        self.assertEqual(words[0], "richi", command)
        # The page writes the version it read; here it is known only after the earlier command ran.
        option = "--expected-updated-at="
        argv = [option + replaced.get(word[len(option):], word[len(option):]) if word.startswith(option) else word
                for word in words[1:]]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO("")):
            result = memory.main(argv)
        self.assertEqual(result == 0, ok, command + "\n" + out.getvalue() + err.getvalue())
        text = out.getvalue() if ok else err.getvalue()
        try:
            return json.loads(text)
        except ValueError:
            self.fail(command + " printed something other than one JSON value: " + repr(text))

    def test_skills_are_read_and_sent_per_agent(self):
        requests = self.evaluate(r"""
setMapAgents([{id:'claude',name:'Claude Code'},{id:'codex',name:'Codex'},{name:'no id'},null]);
assert.deepEqual(knownAgentIds(),['claude','codex']);
assert.equal(agentName('codex'),'Codex');
assert.equal(agentName('cursor'),'cursor');
// An answer without agents keeps the list; any answer with them replaces it.
setMapAgents(undefined);
assert.deepEqual(knownAgentIds(),['claude','codex']);
// A rule may name an agent this computer does not list.
const rule={skill:'review',skills:{cursor:'cursor-review',codex:null}};
assert.deepEqual(ruleAgentIds(rule),['claude','codex','cursor']);
assert.deepEqual(['claude','codex','cursor','other'].map(agent=>ruleSkillFor(rule,agent)),['review',null,'cursor-review','review']);
assert.equal(ruleSkillFor(null,'claude'),null);
assert.deepEqual(ruleSkillSetting({skill:'x'}),{skill:'x',skills:null});
assert.deepEqual(ruleSkillSetting(rule),{skill:null,skills:{claude:'review',cursor:'cursor-review'}});
assert.deepEqual(ruleSkillSetting({skill:null,skills:{claude:'x',codex:'x'}}),{skill:'x',skills:null});
assert.equal(ruleHasSkill({skills:{codex:null}}),false);
assert.equal(ruleHasSkill({skills:{codex:'x'}}),true);
assert.ok(sameRuleSkills({skill:'x'},{skill:null,skills:{claude:'x',codex:'x'}}));
assert.ok(!sameRuleSkills({skill:'x'},{skill:'x',skills:{codex:null}}));
assert.ok(!sameRuleSkills({skill:'x'},{skill:'x',skills:{cursor:'y'}}));
// One list for everybody sends one name.
assert.deepEqual(skillRequest(null,'pr'),{skill:'pr',skills:null});
assert.deepEqual(skillRequest('old',''),{skill:null,skills:null});
// Per-agent lists keep the rule's every-agent skill as the default for agents not listed here,
// and name only the agents that differ from it; "no skill" for one agent is said explicitly.
assert.deepEqual(skillRequest('review',{claude:'review',codex:'code-review'}),{skill:'review',skills:{codex:'code-review'}});
assert.deepEqual(skillRequest('review',{claude:'review',codex:null}),{skill:'review',skills:{codex:null}});
assert.deepEqual(skillRequest('review',{claude:'review',codex:'review'}),{skill:'review',skills:null});
assert.deepEqual(skillRequest(null,{claude:'a',codex:null}),{skill:null,skills:{claude:'a'}});
assert.deepEqual(skillRequest(undefined,{claude:'',codex:''}),{skill:null,skills:null});
assert.equal(changedBy('richi map'),'карта');
assert.equal(changedBy('claude:0123456789abcdef'),'Claude Code · чат 01234567');
assert.equal(changedBy('terminal:abc'),'terminal:abc');
assert.equal(changedBy('x'.repeat(60)).length,40);
assert.equal(changedBy(null),'');
assert.deepEqual([{key:'claude-app',kind:'plugins'},{key:'plugins',kind:'plugins'},{key:'system',kind:'system'},
  {key:'agents-skills',kind:'skills'},{key:'new-kind',kind:'unknown'},{key:'~/team',kind:'skills',added:true}]
  .map(source=>agentSourceLabel({builtin:true},source)),
  ['Плагины claude.ai из приложения Claude','Плагины','Встроенные скиллы','Свои скиллы','Свои скиллы','Добавленная папка']);
assert.equal(agentSourceLabel({builtin:false},{key:'~/x',added:true}),'Папка скиллов');
// What the server makes of each choice is checked against the choice itself.
const chosen=[['review',{claude:'review',codex:'code-review'}],['review',{claude:'review',codex:null}],
  ['review',{claude:'a',codex:'a'}],[null,{claude:'a',codex:null}],[null,'shared'],['review','']];
console.log(JSON.stringify(chosen.map(([base,choice])=>({base,choice,request:skillRequest(base,choice)}))));
""")
        database = self.root / "memory.sqlite3"
        conn = memory.connect(database, create=True)
        self.addCleanup(conn.close)
        memory.initialize(conn, database)
        for number, case in enumerate(requests):
            with self.subTest(case=case):
                identifier, choice = "rule-%d" % number, case["choice"]
                created = rules.change(conn, {"action": "add", "id": identifier, "scope": "global", "text": "x",
                                              "skill": case["base"]}, memory)["rule"]
                stored = rules.change(conn, {"action": "update", "id": identifier, **case["request"],
                                             "expected_updated_at": created["updated_at"]}, memory)["rule"]
                wanted = choice if isinstance(choice, dict) else dict.fromkeys(agents.ids(), choice or None)
                self.assertEqual({agent: rules.skill_for(stored, agent) for agent in wanted}, wanted)

    def test_a_composed_command_names_the_store_and_survives_a_shell(self):
        database = self.root / "my store" / "memory.sqlite3"
        database.parent.mkdir()
        repository = self.root / "service"
        repository.mkdir()
        for argv in (["init"], ["project", "add", str(repository), "--id", "service"]):
            with redirect_stdout(io.StringIO()), mock.patch.object(sys, "stdin", io.StringIO("")):
                self.assertEqual(memory.main(["--db", str(database), *argv]), 0)
        commands = self.evaluate(r"""
const store=['--db',%s],text=%s,removal={action:'remove',id:'x',expected_updated_at:'v'};
// Before the map says which store it shows, no command is composed: a bare one would change
// whatever store the terminal uses.
for(const unknown of [undefined,null,[],{workspace:'team'}])assert.equal(ruleCommand(removal,unknown),'');
assert.equal(ruleCommand(removal,['--workspace','team']),'richi --workspace team rules remove x --expected-updated-at=v');
assert.equal(ruleCommand(removal,['--config','/etc/r c.json','--db','/x']),
  "richi --config '/etc/r c.json' --db /x rules remove x --expected-updated-at=v");
// A damaged row is removed by whatever its key holds; a key with a leading dash goes after --.
assert.equal(ruleCommand({...removal,id:'two words'},['--db','/x']),"richi --db /x rules remove 'two words' --expected-updated-at=v");
assert.equal(ruleCommand({...removal,id:'-dash'},['--db','/x']),'richi --db /x rules remove --expected-updated-at=v -- -dash');
console.log(JSON.stringify({
  tricky:ruleCommand({action:'add',id:'tricky',scope:'global',text,skill:'pr-description',skills:{codex:'code-review'}},store),
  dashed:ruleCommand({action:'add',id:'dashed',scope:'project',project:'service',text:'- one instruction'},store),
  update:ruleCommand({action:'update',id:'tricky',expected_updated_at:'VERSION',text:'-x',skill:'pr-description',skills:{codex:null}},store),
  clear:ruleCommand({action:'update',id:'tricky',expected_updated_at:'VERSION',skill:null,skills:null},store),
  remove:ruleCommand({action:'remove',id:'tricky',expected_updated_at:'VERSION'},store),
  oddSpaced:ruleCommand({action:'remove',id:'two words',expected_updated_at:'invalid'},store),
  oddDashed:ruleCommand({action:'remove',id:'-dash',expected_updated_at:'invalid'},store)}));
""" % (json.dumps(str(database)), json.dumps(TRICKY)))
        self.assertTrue(all(command.startswith("richi --db '" + str(database) + "' rules ") for command in commands.values()))
        tricky = self.run_command(commands["tricky"])["rule"]
        self.assertEqual((tricky["text"], tricky["skill"], tricky["skills"], tricky["source"]),
                         (TRICKY, "pr-description", {"codex": "code-review"}, serve.RULES_SOURCE))
        dashed = self.run_command(commands["dashed"])["rule"]
        self.assertEqual((dashed["text"], dashed["project_id"]), ("- one instruction", "service"))
        updated = self.run_command(commands["update"], VERSION=tricky["updated_at"])["rule"]
        self.assertEqual((updated["text"], updated["skill"], updated["skills"]), ("-x", "pr-description", {"codex": None}))
        cleared = self.run_command(commands["clear"], VERSION=updated["updated_at"])["rule"]
        self.assertEqual((cleared["skill"], cleared["skills"]), (None, None))
        self.assertEqual(self.run_command(commands["remove"], ok=False, VERSION=updated["updated_at"])["code"], "conflict")
        self.assertEqual(self.run_command(commands["remove"], VERSION=cleared["updated_at"])["results"][0]["status"], "removed")
        with closing(sqlite3.connect(str(database))) as conn, conn:
            conn.executemany("INSERT INTO metadata(key, value) VALUES (?, '{}')", [("rule:two words",), ("rule:-dash",)])
        for name in ("oddSpaced", "oddDashed"):
            self.assertEqual(self.run_command(commands[name])["results"][0]["status"], "removed")

    def test_a_composed_agent_command_is_accepted_as_written(self):
        (self.home / ".gemini" / "skills").mkdir(parents=True)
        commands = self.evaluate(r"""
console.log(JSON.stringify({
  add:agentCommand({action:'add',id:'gemini-cli',name:"Gemini's CLI",folders:['~/.gemini/skills'],variable:'GEMINI_CLI=1',manual_skills:'front-matter'}),
  plain:agentCommand({action:'add',id:'other',name:'Other',folders:['/opt/team skills'],manual_skills:'none'}),
  update:agentCommand({action:'update',id:'gemini-cli',expected_updated_at:'VERSION',name:'-g',variable:'',manual_skills:'none',
    add_folders:['/opt/team skills'],disable:['~/.gemini/skills']}),
  adjust:agentCommand({action:'update',id:'claude',disable:['claude-app'],enable:[]}),
  remove:agentCommand({action:'remove',id:'gemini-cli',expected_updated_at:'VERSION'})}));
""")
        self.assertEqual(commands["plain"], "richi skill-sources add other --name=Other --skills='/opt/team skills'")
        self.assertEqual(commands["adjust"], "richi skill-sources update claude --disable=claude-app")
        added = self.run_command(commands["add"])["agent"]
        self.assertEqual((added["name"], added["variables"], added["manual_skills"], [source["key"] for source in added["sources"]]),
                         ("Gemini's CLI", ["GEMINI_CLI=1"], "front-matter", ["~/.gemini/skills"]))
        updated = self.run_command(commands["update"], VERSION=added["updated_at"])["agent"]
        self.assertEqual((updated["name"], updated["variables"], updated["manual_skills"]), ("-g", [], "none"))
        self.assertEqual([(source["key"], source["enabled"]) for source in updated["sources"]],
                         [("~/.gemini/skills", False), ("/opt/team skills", True)])
        self.run_command(commands["adjust"])
        self.assertEqual(self.run_command(commands["remove"], VERSION=updated["updated_at"])["results"][0]["status"], "removed")


if __name__ == "__main__":
    unittest.main()
