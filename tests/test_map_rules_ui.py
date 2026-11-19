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
// Whether the page may save; the forms read it when a save fails.
const rulesState = {editing: true};
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
                    or key in {"CODEX_THREAD_ID", "CLAUDECODE", "CURSOR_AGENT", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
                               "XDG_CONFIG_HOME"}]:
            del os.environ[key]
        agents.forget_settings()
        self.addCleanup(agents.forget_settings)

    def evaluate(self, script, sections=()):
        """Run the page's DOM-free lines under Node; what the script prints, as JSON."""
        source = MAP.read_text(encoding="utf-8")
        self.assertIn("RULE_SOURCE='%s'" % serve.RULES_SOURCE, source)
        block = source[source.index(BEGIN):source.index(END)]
        for begin, end in sections:
            start = source.index(begin)
            block += source[start:source.index(end, start)]
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
setMapAgents([{id:'claude',name:'Claude Code'},{id:'codex',name:'Codex'},{name:'no id'},null],++agentSequence);
assert.deepEqual(knownAgentIds(),['claude','codex']);
assert.equal(agentName('codex'),'Codex');
assert.equal(agentName('cursor'),'cursor');
// An answer without agents keeps the list; any answer with them replaces it.
setMapAgents(undefined,++agentSequence);
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
// The agents a computer does not list compare too: they get the rule's skill, here x or none.
assert.ok(!sameRuleSkills({skill:'x'},{skill:null,skills:{claude:'x',codex:'x'}}));
assert.ok(sameRuleSkills({skill:'x'},{skill:'x',skills:{claude:'x'}}));
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
// "No skill" for an agent the rule names and this computer does not list is said: the server keeps it
// beside the skill of the action, so that the agent still gets none once it is back.
assert.deepEqual(skillRequest(null,{claude:'a',codex:'a',cursor:null}),{skill:null,skills:{claude:'a',codex:'a',cursor:null}});
assert.deepEqual(skillRequest('a',{claude:'a',codex:'a',cursor:null}),{skill:'a',skills:{cursor:null}});
// The agents this computer lists decide; one the rule names beyond them keeps its own pick beside it.
assert.equal(becomesShared(null,{claude:'a',codex:'a',cursor:null}),true);
assert.equal(becomesShared(null,{claude:'a',codex:'a',cursor:'c'}),true);
assert.equal(becomesShared(null,{claude:'a',codex:null,cursor:'a'}),false);
assert.equal(changedBy('richi map'),'карта');
assert.equal(changedBy('claude:0123456789abcdef'),'Claude Code · чат 01234567');
// The agent the server stored names the chat also where the identity has no prefix (Codex).
assert.equal(changedBy('019a1b2c-3d4e-5f60','codex'),'Codex · чат 019a1b2c');
assert.equal(changedBy('claude:0123456789abcdef','claude'),'Claude Code · чат 01234567');
assert.equal(changedBy('claude:0123456789abcdef','cursor'),'Claude Code · чат 01234567');
assert.equal(changedBy('claude:0123456789abcdef','codex'),'Claude Code · чат 01234567');
assert.equal(changedBy('019a1b2c-3d4e-5f60',null),'019a1b2c-3d4e-5f60');
assert.equal(changedBy('richi map','claude'),'карта');
assert.equal(changedBy('terminal:abc'),'terminal:abc');
assert.equal(changedBy('x'.repeat(60)).length,40);
assert.equal(changedBy(null),'');
assert.deepEqual([{key:'claude-app',kind:'plugins'},{key:'plugins',kind:'plugins'},{key:'system',kind:'system'},
  {key:'agents-skills',kind:'skills'},{key:'new-kind',kind:'unknown'},{key:'~/team',kind:'skills',added:true}]
  .map(source=>agentSourceLabel({builtin:true},source)),
  ['Плагины claude.ai из приложения Claude','Плагины','Встроенные скиллы','Свои скиллы','Свои скиллы','Добавленная папка']);
assert.equal(agentSourceLabel({builtin:false},{key:'~/x',added:true}),'Папка скиллов');
// The every-agent skill that no listed agent uses still belongs to the rule: the card names it.
assert.equal(ruleOtherSkill({skill:'general',skills:{claude:'special',codex:'special'}}),'general');
assert.equal(ruleOtherSkill({skill:'general',skills:{claude:'special',codex:null}}),'general');
assert.equal(ruleOtherSkill({skill:'general',skills:{codex:'special'}}),null);
assert.equal(ruleOtherSkill({skill:null,skills:{claude:'a',codex:'b'}}),null);
assert.equal(ruleOtherSkill({skill:'general'}),null);
// The words a skill rule is composed of, and the ID suggested from a text.
assert.equal(skillPhrase({skill:'pr-description'}),'используй скилл pr-description');
assert.equal(skillPhrase({skill:null,skills:{claude:'a'}}),'в Claude Code используй скилл a');
assert.equal(skillPhrase({skill:null,skills:{claude:'a',codex:'b'}}),'используй скилл a в Claude Code и b в Codex');
assert.equal(skillPhrase({}),'');
assert.equal(ruleSlug('Отвечай на русском языке, а код оставляй'),'otvechay-na-russkom-yazyke-a');
assert.equal(ruleSlug('  --PR: описание!  '),'pr-opisanie');
assert.equal(ruleSlug(''),'');
// A shell expands $HOME, ~ and (zsh) a word that begins with "=": such words are quoted.
for(const word of ['$HOME','~','~/x','=ls','two words',"it's",''])assert.notEqual(shellQuote(word),word);
for(const word of ['a=b','plain-1.2','plugin:skill','/opt/skills'])assert.equal(shellQuote(word),word);
// The editor shows one skill for every agent only when the agents it does not list get that skill too:
// otherwise saving the shown skill unchanged would change it for them.
assert.equal(ruleSharesSkill({}),true);
assert.equal(ruleSharesSkill({skill:'a'}),true);
assert.equal(ruleSharesSkill({skill:'a',skills:{claude:'a'}}),true);
assert.equal(ruleSharesSkill({skill:'x',skills:{claude:'a',codex:'a'}}),false);
assert.equal(ruleSharesSkill({skill:null,skills:{claude:'x',codex:'x'}}),false);
assert.equal(ruleSharesSkill({skill:'a',skills:{cursor:'c'}}),false);
// Picks that are all the same, with nothing of its own for the other agents, become the skill for every agent.
assert.equal(becomesShared(null,{claude:'a',codex:'a'}),true);
assert.equal(becomesShared('',{claude:'a',codex:'a'}),true);
assert.equal(becomesShared('x',{claude:'a',codex:'a'}),false);
assert.equal(becomesShared(null,{claude:'a',codex:'b'}),false);
assert.equal(becomesShared(null,{claude:null,codex:null}),false);
assert.equal(becomesShared(null,{}),false);
// What the server makes of each choice is checked against the choice itself.
setMapAgents(%s,++agentSequence);
const allPick=skill=>Object.fromEntries(knownAgentIds().map(agent=>[agent,skill]));
const chosen=[['review',{claude:'review',codex:'code-review'}],['review',{claude:'review',codex:null}],
  ['review',allPick('a')],[null,{claude:'a',codex:null}],[null,'shared'],['review',''],
  [null,allPick('a')],['general',allPick('special')]];
console.log(JSON.stringify(chosen.map(([base,choice])=>({base,choice,request:skillRequest(base,choice),
  shared:typeof choice==='object'&&becomesShared(base,choice)}))));
""" % json.dumps(agents.describe()))
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
                # An agent this computer does not list: the per-agent lists leave it the rule's
                # every-agent skill, and one list for everybody gives it that choice too. Per-agent
                # picks that are all the same become that skill, as the form says beforehand.
                unlisted = (choice["claude"] if case["shared"] else case["base"]) if isinstance(choice, dict) \
                    else choice or None
                self.assertEqual(rules.skill_for(stored, "unlisted"), unlisted)

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
  oddSpaced:ruleCommand({action:'remove',id:'two words',expected_updated_at:'VERSION'},store),
  oddDashed:ruleCommand({action:'remove',id:'-dash',expected_updated_at:'VERSION'},store)}));
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
        invalid = self.run_command("richi --db " + shlex.quote(str(database)) + " rules list")["invalid_rules"]
        versions = {item["id"]: item["updated_at"] for item in invalid}
        for name, identifier in (("oddSpaced", "two words"), ("oddDashed", "-dash")):
            self.assertEqual(self.run_command(commands[name], VERSION=versions[identifier])["results"][0]["status"], "removed")

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

    def test_the_forms_check_what_the_commands_check(self):
        samples = {
            "id": ["gemini-cli", "x1", "Gemini", "-x", "a" * 32, "a" * 33, "", "a b", "1x"],
            "variable": ["GEMINI_CLI", "GEMINI_CLI=1", "_x", "1BAD", "A B", "A=", "A=" + "v" * 100, "A=" + "v" * 101,
                         "A=значение", "A=two words", "A" * 64, "A" * 65,
                         # Counted in characters, as Python counts them; half of a surrogate pair is refused.
                         "A=\ud800", "A=\U0001F600", "A=" + "\U0001F600" * 100, "A=" + "\U0001F600" * 101],
            "name": ["pr-description", "plugin:skill", "x.y_z", "two words", "-x", "a" * 100, "a" * 101, "", "скилл"],
            "folder": ["~", "~/skills", "/opt/team skills", "/", "relative", "~x", "", "./x",
                       "/" + "a" * (agents.MAX_FOLDER - 1), "/" + "a" * agents.MAX_FOLDER,
                       # Characters as Python counts them: an emoji is one, though two UTF-16 units.
                       "/opt/" + "\U0001F600" * 600, "/" + "\U0001F600" * agents.MAX_FOLDER,
                       # One line, no half of a surrogate pair, and nothing Python's strip() removes at an end.
                       "/opt/a\tb", "/opt/a\x7fb", "/opt/\ud800", "/opt/x\u00a0", "/opt/x\x85", " /opt/x",
                       "/opt/a\u2028b", "/opt/x\ufeff", "~/команда"],
            "agent_name": ["Gemini CLI", "", " x", "x ", "a" * agents.MAX_NAME, "a" * (agents.MAX_NAME + 1),
                           "\U0001F600" * agents.MAX_NAME, "\U0001F600" * (agents.MAX_NAME + 1), "a\tb", "\ud800",
                           "Агент\u3000", "x\u2028y"],
        }
        page = self.evaluate(r"""
const samples=%s,limit=%d,nameLimit=%d;
console.log(JSON.stringify({id:samples.id.map(value=>AGENT_ID.test(value)),variable:samples.variable.map(value=>AGENT_VARIABLE.test(value)),
  name:samples.name.map(value=>RULE_NAME.test(value)),folder:samples.folder.map(value=>agentFolderOk(value,limit)),
  agent_name:samples.agent_name.map(value=>agentNameOk(value,nameLimit))}));
""" % (json.dumps(samples), agents.MAX_FOLDER, agents.MAX_NAME))
        self.assertEqual(page, {
            "id": [agents.valid_id(value) for value in samples["id"]],
            "variable": [agents.valid_variable(value) for value in samples["variable"]],
            # One shape serves rule IDs and skill names.
            "name": [agents.valid_skill(value) for value in samples["name"]],
            "folder": [agents.valid_folder(value) for value in samples["folder"]],
            "agent_name": [agents.valid_name(value) for value in samples["agent_name"]]})
        for value, valid in zip(samples["name"], page["name"]):
            with self.subTest(rule_id=value):
                try:
                    rules.rule_id(value)
                    accepted = True
                except ValueError:
                    accepted = False
                self.assertEqual(accepted, valid)

    def test_no_skill_for_an_agent_this_computer_does_not_list_is_kept(self):
        # A rule names gemini-cli, which the user then removed from this computer. In the per-agent lists
        # they pick one skill for all known agents, and "Без скилла" for gemini-cli.
        added = self.run_command("richi skill-sources add gemini-cli --name 'Gemini CLI' --skills ~/.gemini/skills")
        database = self.root / "memory.sqlite3"
        conn = memory.connect(database, create=True)
        self.addCleanup(conn.close)
        memory.initialize(conn, database)
        with memory.transaction(conn):
            created = rules.change(conn, {"action": "add", "id": "review", "scope": "global", "text": "x",
                                          "skills": {**dict.fromkeys(agents.builtin_ids(), "old"), "gemini-cli": "gem"}},
                                   memory)["rule"]
        self.run_command("richi skill-sources remove gemini-cli --expected-updated-at=VERSION",
                         VERSION=added["agent"]["updated_at"])
        listed = json.dumps(agents.describe())
        request = self.evaluate(r"""
setMapAgents(%s,++agentSequence);
const rule=%s,picks=Object.fromEntries(knownAgentIds().map(agent=>[agent,'review']));
picks['gemini-cli']=null;
// Announced: the skill every agent of this computer gets is the skill of the action, and gemini-cli keeps its none.
assert.equal(becomesShared(null,picks),true);
console.log(JSON.stringify(ruleUpdate(rule,rule,rule.text,skillRequest(null,picks))));
""" % (listed, json.dumps(created)))
        with memory.transaction(conn):
            stored = rules.change(conn, request, memory)["rule"]
        self.assertEqual((stored["skill"], stored["skills"]), ("review", {"gemini-cli": None}))
        # Opened again, the form shows what is stored and has nothing to save.
        unchanged = self.evaluate(r"""
setMapAgents(%s,++agentSequence);
const rule=%s,picks=Object.fromEntries(ruleAgentIds(rule).map(agent=>[agent,ruleSkillFor(rule,agent)]));
console.log(JSON.stringify({picks,update:ruleUpdate(rule,rule,rule.text,skillRequest(rule.skill,picks)),
  announced:becomesShared(rule.skill,picks)}));
""" % (listed, json.dumps(stored)))
        self.assertEqual(unchanged, {"picks": {**dict.fromkeys(agents.builtin_ids(), "review"), "gemini-cli": None},
                                     "update": None, "announced": False})

    def test_a_retry_after_a_conflict_sends_only_what_the_form_changed(self):
        self.evaluate(r"""
setMapAgents([{id:'claude',name:'Claude Code'},{id:'codex',name:'Codex'}],++agentSequence);
const opened={id:'review',text:'Review code.',skill:null,skills:null,scope:'global',project_id:null,created_at:'v1',updated_at:'v1'};
// Meanwhile another window gave the rule a skill for one agent.
const fresh={...opened,skills:{codex:'review-helper'},updated_at:'v2'};
const unchangedSkills=skillRequest(opened.skill,{claude:null,codex:null});
assert.match(ruleUpdate(opened,{...fresh,created_at:'v2'},'draft',unchangedSkills),/создали заново/);
assert.match(ruleUpdate(opened,{...fresh,scope:'project',project_id:'other'},'draft',unchangedSkills),/другой области/);
// The form changed the text only: the retry names the new version and leaves the skills alone.
assert.deepEqual(ruleUpdate(opened,fresh,'Review code carefully.',unchangedSkills),
  {action:'update',id:'review',expected_updated_at:'v2',text:'Review code carefully.'});
// It changed the skills too: both changes go, as the form shows them.
assert.deepEqual(ruleUpdate(opened,fresh,'Review code carefully.',skillRequest(null,'code-review')),
  {action:'update',id:'review',expected_updated_at:'v2',text:'Review code carefully.',skill:'code-review',skills:null});
// Nothing was changed in the form, whatever happened elsewhere.
assert.equal(ruleUpdate(opened,fresh,'Review code.',unchangedSkills),null);
// A text field gives line ends as \n: a stored \r is no change of the text.
const crlf={...opened,text:'One.\r\nTwo.\r'};
assert.equal(ruleUpdate(crlf,crlf,'One.\nTwo.\n',unchangedSkills),null);
assert.equal(ruleUpdate(crlf,crlf,'One.\nTwo.',unchangedSkills).text,'One.\nTwo.');
// "No skill" for an agent that another window took out of the rule, and this computer does not list,
// is not sent: the server would refuse the unknown agent.
const named={...opened,skills:{claude:'a',codex:'a',cursor:'c'}},dropped={...named,skills:{claude:'a',codex:'a'},updated_at:'v3'};
const picked=skillRequest(null,{claude:'b',codex:'b',cursor:null});
assert.deepEqual(ruleUpdate(named,named,'Review code.',picked).skills,{claude:'b',codex:'b',cursor:null});
assert.deepEqual(ruleUpdate(named,dropped,'Review code.',picked).skills,{claude:'b',codex:'b'});
// A skill for that agent which the form left as it was gives way to the latest version; one chosen
// here cannot be stored, and the form says so instead of sending what the server refuses.
const kept=skillRequest(null,{claude:'b',codex:'b',cursor:'c'});
assert.deepEqual(ruleUpdate(named,dropped,'Review code.',kept).skills,{claude:'b',codex:'b'});
assert.match(ruleUpdate(named,dropped,'Review code.',skillRequest(null,{claude:'b',codex:'b',cursor:'d'})),/«cursor».*Без скилла/);
// The server takes no entry for that agent, "no skill" included: it gets what «Другие агенты» gives. A pick
// equal to that is no change; "no skill" beside another skill there would be one, and is refused too.
const withOthers=pick=>ruleUpdate(named,dropped,'Review code.',skillRequest('S',{claude:'b',codex:'b',cursor:pick}));
assert.deepEqual(withOthers('S').skills,{claude:'b',codex:'b'});
assert.equal(withOthers('S').skill,'S');
assert.match(withOthers(null),/«cursor».*то же, что «Другие агенты».*«S»/);
assert.match(withOthers('d'),/«S»/);
// What a save stored is said when the form did not say it beforehand.
assert.equal(skillOutcome({skill:'a'},{skill:'a',skills:null},false),'');
assert.match(skillOutcome({skill:null,skills:{claude:'a',codex:'a'}},{skill:'a',skills:null},false),/«a» сохранён как общий/);
assert.equal(skillOutcome({skill:null,skills:{claude:'a',codex:'a'}},{skill:'a',skills:null},true),'');
assert.match(skillOutcome({skill:null,skills:{claude:'a',codex:'a'}},{skill:null,skills:{claude:'a',codex:'a'}},true),/для каждого агента отдельно/);
assert.equal(skillOutcome({skill:null,skills:{claude:'a',codex:'b'}},{skill:null,skills:{claude:'a',codex:'b'}},false),'');
assert.equal(skillOutcome({skill:null,skills:{claude:'a'}},undefined,false),'');
// The answer says why nothing was merged when Richi could not use every own agent's settings.
assert.match(skillOutcome({skill:null,skills:{claude:'a',codex:'a'}},{skill:null,skills:{claude:'a',codex:'a'}},true,{code:'agents'}),/настройки некоторых агентов/);
// Merged with an exception kept beside it: as announced, nothing to add.
assert.equal(skillOutcome({skill:null,skills:{claude:'a',codex:'a',cursor:null}},{skill:'a',skills:{cursor:null}},true),'');
assert.equal(ruleUpdate(opened,opened,'Review code.',skillRequest(null,'')),null);
// The same for an own agent. Its form changed the variable only.
const agent={id:'gem',name:'Foo',variables:['GEM=1'],manual_skills:'none',created_at:'a1',updated_at:'a1'};
const typed={name:'Foo',variable:'GEM=2',manual_skills:'none'};
assert.deepEqual(agentUpdate(agent,agent,typed),{action:'update',id:'gem',variable:'GEM=2',expected_updated_at:'a1'});
// Meanwhile another window renamed the agent: the retry names the new version and does not undo the rename.
const renamed={...agent,name:'Bar',updated_at:'a2'};
assert.match(agentUpdate(agent,{...renamed,created_at:'a2'},typed),/создать заново/);
const legacy={...agent,created_at:null};
assert.match(agentUpdate(legacy,{...legacy,updated_at:'a2'},typed),/создать заново/);
assert.deepEqual(agentUpdate(agent,renamed,typed),{action:'update',id:'gem',variable:'GEM=2',expected_updated_at:'a2'});
assert.equal(agentDifference(agent,renamed),'имя «Bar»');
assert.equal(agentDifference(agent,{...agent,variables:[],manual_skills:'front-matter'}),'переменной больше нет, скиллы только по вызову скрываются');
assert.equal(agentDifference(agent,agent),'');
// Nothing was changed in the form; a cleared variable is sent as an empty one; a form may change everything.
assert.equal(agentUpdate(agent,renamed,{name:'Foo',variable:'GEM=1',manual_skills:'none'}),null);
assert.deepEqual(agentUpdate(agent,agent,{name:'Gem',variable:'',manual_skills:'front-matter'}),
  {action:'update',id:'gem',name:'Gem',variable:'',manual_skills:'front-matter',expected_updated_at:'a1'});
""")

    def test_a_refusal_is_told_by_its_code_not_by_its_wording(self):
        self.evaluate(r"""
const refusal=(code,subject,message='Some English text')=>Object.assign(new Error(message),{code,subject});
// One code covers three limits of a rule; the wording of the message decides nothing.
assert.match(ruleRefusal(refusal('limit','rules','Text in all')),/Все правила workspace/);
assert.match(ruleRefusal(refusal('limit','record')),/слишком велико/);
assert.match(ruleRefusal(refusal('limit','text','Rules of one workspace may hold at most 20000 characters in all')),/Текст правила длиннее/);
assert.match(ruleRefusal(refusal('limit')),/Текст правила длиннее/);
assert.match(ruleRefusal(refusal('conflict')),/изменили после загрузки/);
// An agent and a skill source share codes too.
assert.match(agentRefusal(refusal('exists','source')),/папка уже есть/);
assert.match(agentRefusal(refusal('exists','agent','claude is built into Richi; adjust its skill sources with skill-sources update')),/Агент с таким ID/);
assert.match(agentRefusal(refusal('limit','folders')),/папок/);
assert.match(agentRefusal(refusal('limit','agents')),/агентов/);
assert.match(agentRefusal(refusal('limit','size')),/не поместятся/);
assert.match(agentRefusal(refusal('not_found','source')),/Источник не найден/);
assert.match(agentRefusal(refusal('not_found','agent')),/Агент не найден/);
assert.match(agentRefusal(refusal('settings'),'/home/u/agents.json'),/\/home\/u\/agents\.json/);
assert.match(agentRefusal(refusal('settings')),/agents\.json/);
assert.match(agentRefusal(refusal('settings','damaged'),'/home/u/agents.json'),/Исправьте или удалите файл: \/home\/u\/agents\.json/);
// Settings a newer Richi wrote are not damage: updating helps, removing them would lose them.
assert.match(agentRefusal(refusal('settings','newer')),/более новая версия Richi.*richi update/);
assert.doesNotMatch(agentRefusal(refusal('settings','newer')),/удалите/);
// One agent's settings that this version cannot use: not the file, and not an agent that is missing.
assert.match(agentRefusal(refusal('settings','agent')),/Настройки этого агента.*richi update/);
assert.doesNotMatch(agentRefusal(refusal('settings','agent'),'/home/u/agents.json'),/agents\.json|не найден/);
assert.match(agentSettingsProblem('newer','/x/agents.json'),/richi update/);
assert.doesNotMatch(agentSettingsProblem('newer','/x/agents.json'),/удалите/);
assert.match(agentSettingsProblem('unreadable','/x/agents.json'),/не прочитались.*\/x\/agents\.json/);
assert.match(agentSettingsProblem('damaged','/x/agents.json'),/Исправьте или удалите файл: \/x\/agents\.json/);
assert.match(agentSettingsProblem(undefined),/повреждён.*agents\.json/);
assert.match(unknownAgentText({id:'future'}),/записанный более новой версией Richi/);
assert.match(unknownAgentText({id:'x',error:'invalid skill folder for x',next:'richi skill-sources remove x --expected-updated-at=t'}),/или удалите их: richi skill-sources remove x/);
assert.doesNotMatch(unknownAgentText({id:'Bad ID',error:'not an agent ID'}),/удалите их/);
// What is not about the change itself reads the same on every form.
for(const explain of [ruleRefusal,agentRefusal]){
  assert.match(explain(refusal('busy')),/Повторите через несколько секунд/);
  assert.match(explain(refusal('timeout')),/могло сохраниться/);
  assert.match(explain(refusal('offline')),/richi map/);
  assert.match(explain(Object.assign(new Error('aborted'),{name:'AbortError'})),/не ответил за 60 секунд/);
  // A code the page has no words for is shown as the server put it.
  assert.equal(explain(refusal('internal',undefined,'The map could not answer: TypeError')),'The map could not answer: TypeError');
  assert.match(explain(refusal('unreadable')),/могло сохраниться/);
}
""")

    def test_overlapping_reads_cannot_replace_a_newer_skill_catalog_or_agent_list(self):
        self.evaluate(r"""
const state={mode:'agents'},agentsState={request:0},RULE_HEADER='X-Richi-Map-Secret';
Object.assign(rulesState,{skills:null,request:0});
let agentsController=null,rulesController=null,ruleSecret='';
function renderAgents(){} function renderRules(){} function setMapEditing(){} function refreshRulePicker(){}
const pending=new Map();
const fetch=url=>new Promise((resolve,reject)=>pending.set(url,{
  respond:data=>resolve({ok:true,json:async()=>data}),fail:()=>reject(new Error('offline'))}));
const listing=name=>({agents:[{id:name,name}],catalog:{agents:[{id:name,name}],skills:[{name}]}});
function current(agent,skill){assert.deepEqual(knownAgentIds(),[agent]);assert.deepEqual([...rulesState.skillMap.keys()],[skill]);}
(async()=>{
  // A change finishes first. Neither the old rules answer nor either outcome of the old skill scan
  // changes the saved registry. The rule list itself remains useful.
  for(const failed of [false,true]){
    const skills=loadSkills(),rules=loadRules(),agents=loadAgents();
    acceptAgents(listing('saved'));
    pending.get('/api/rules').respond({rules:[{id:'keep'}],agents:listing('old').agents});await rules;
    pending.get('/api/agents').respond(listing('old'));await agents;
    const reading=pending.get('/api/skills');if(failed)reading.fail();else reading.respond(listing('old').catalog);
    await skills;current('saved','saved');assert.equal(rulesState.data.rules[0].id,'keep');
  }
  // A newer agent reading supplies both lists. A slower skills reading cannot replace either.
  let skills=loadSkills(),agents=loadAgents();
  pending.get('/api/agents').respond(listing('later'));await agents;
  pending.get('/api/skills').respond(listing('earlier').catalog);await skills;current('later','later');
  // Conversely the newer skill reading wins even if the older agent listing finishes last.
  // Its agent details still load through their own endpoint.
  agents=loadAgents();skills=loadSkills();
  pending.get('/api/skills').respond(listing('latest').catalog);await skills;
  pending.get('/api/agents').respond(listing('earlier'));await agents;current('latest','latest');
  assert.equal(agentsState.data.agents[0].id,'earlier');
  // Rules supply names only: accept the skills from an earlier reading without rolling those names back.
  skills=loadSkills();const rules=loadRules();
  pending.get('/api/rules').respond({rules:[],agents:listing('names').agents});await rules;
  pending.get('/api/skills').respond(listing('catalog').catalog);await skills;current('names','catalog');
})().catch(error=>{console.error(error);process.exitCode=1;});
""", sections=(("  async function loadSkills(", "  async function redeemRuleTicket("),
               ("  async function loadAgents(", "  function agentChangeProblem("),
               ("  function setAgents(", "  // Closing a saving form")))

    def test_a_late_response_from_the_previous_edit_session_keeps_the_new_session(self):
        self.evaluate(r"""
const state={mode:'map'},agentsState={},location={pathname:'/',search:'',hash:''},history={replaceState(){}};
const RULE_HEADER='X-Richi-Map-Secret';let ruleSecret='before',ruleKeyVersion=1,ticketRequest=0;
function storeRuleSecret(value){ruleSecret=value;ruleKeyVersion++;}
function setMapEditing(editing){rulesState.editing=editing;storeRuleSecret('');}
const pending=new Map(),fetch=(path,options)=>new Promise(resolve=>{
  const key=path==='/api/session'?JSON.parse(options.body).ticket:path;
  pending.set(key,(status,data)=>resolve({status,ok:status===200,json:async()=>data}));
});
(async()=>{
  const oldSave=sendMapChange('/api/agents',{});
  const old='a'.repeat(64),fresh='b'.repeat(64);
  location.hash='#ticket='+old;const early=redeemRuleTicket();
  location.hash='#ticket='+fresh;const later=redeemRuleTicket();
  pending.get(fresh)(200,{secret:'current'});await later;
  pending.get(old)(200,{secret:'old'});await early;
  assert.equal(ruleSecret,'current');assert.equal(rulesState.notice,'');
  pending.get('/api/agents')(403,{code:'not_editing'});
  await assert.rejects(oldSave,error=>/ключ редактирования сменился/.test(editKeyProblem(error)));
  assert.equal(ruleSecret,'current');assert.equal(rulesState.editing,true);
  const currentSave=sendMapChange('/api/rules',{});
  pending.get('/api/rules')(403,{code:'not_editing'});
  await assert.rejects(currentSave,error=>/больше не действует/.test(editKeyProblem(error)));
  assert.equal(ruleSecret,'');assert.equal(rulesState.editing,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
""", sections=(("  async function redeemRuleTicket(", "  // A tab is rebuilt"),))

    def test_open_skill_select_refreshes_options_without_losing_manual_input(self):
        self.evaluate(r"""
class Element {
  constructor(){this.children=[];this.value='';}
  append(...children){this.children.push(...children);}
  add(option){this.append(option);}
  replaceChildren(){this.children=[];this.value='';}
  setAttribute(){}
  addEventListener(){}
}
const Option=class {constructor(label,value){this.value=value;}},make=()=>new Element(),cut=x=>x;
const agent='codex',catalog=names=>({skills:names.map(name=>({name,agents:[agent],description:name,kind:'skills'}))});
setSkills(catalog([]),++agentSequence);rulesState.skillsLoading=true;
const picker=skillSelect(agent,''),input=picker.element.children[1],hint=picker.element.children[2];
assert.match(hint.textContent,/Загружаем/);
setSkills(catalog(['review']),++agentSequence);rulesState.skillsLoading=false;picker.refresh();
assert.deepEqual(picker.select.children.flatMap(e=>e.children||[e]).map(opt=>opt.value),['','review','__other__']);
picker.set('review');assert.equal(picker.value(),'review');
setSkills(catalog([]),++agentSequence);picker.refresh();assert.equal(picker.select.value,'__other__');assert.equal(input.value,'review');
// A scan that now includes the typed name must not move focus out of its input or replace its raw text.
input.value=' pending ';setSkills(catalog(['pending']),++agentSequence);picker.refresh();
assert.equal(picker.select.value,'__other__');assert.equal(input.value,' pending ');assert.equal(input.hidden,false);
""", sections=(("  function skillSelect(", "  function ruleSkillPicker("),))

    def test_agent_saves_from_overlapping_forms_apply_listings_in_commit_order(self):
        self.evaluate(r"""
const pending=new Map(),sent=[],applied=[];
function sendMapChange(url,change){
  assert.equal(url,'/api/agents');sent.push(change);
  return new Promise((resolve,reject)=>pending.set(change,{resolve,reject}));
}
function acceptAgents(listing){applied.push(listing);}
(async()=>{
  // The first form was closed while saving; the next one submits a different agent. Its response
  // must not overtake the first and let the stale first listing replace both committed changes.
  const first=saveAgentChange('first'),second=saveAgentChange('second');await Promise.resolve();
  assert.deepEqual(sent,['first']);
  pending.get('first').resolve({listing:['first']});await first;await Promise.resolve();
  assert.deepEqual(sent,['first','second']);
  pending.get('second').resolve({listing:['first','second']});await second;
  assert.deepEqual(applied,[['first'],['first','second']]);
  const stale=saveAgentChange('stale'),next=saveAgentChange('next');await Promise.resolve();
  pending.get('stale').reject(new Error('conflict'));await assert.rejects(stale,/conflict/);await Promise.resolve();
  assert.equal(sent.at(-1),'next');
  pending.get('next').resolve({listing:['next']});await next;
  assert.deepEqual(applied.at(-1),['next']);
})().catch(error=>{console.error(error);process.exitCode=1;});
""", sections=(("  let agentSave=", "  const closeFolderForm="),))

    def test_removal_cannot_adopt_an_unseen_replacement_under_the_old_confirmation(self):
        self.evaluate(r"""
const sent=[],messages=[],agentCommandHint='',event={preventDefault(){}};
let submit,current;
const control=()=>({append(){},focus(){}});
const make=control,toolButton=control,cut=value=>value;
function ruleDialog(){return {alive:()=>true,dialog:{showModal(){}},form:{addEventListener(type,fn){submit=fn;}},
  body:control(),foot:control()};}
const ruleProblem=()=>text=>messages.push(text),ruleCommandBox=()=>()=>{};
const ruleScopeText=rule=>rule.scope,agentRules=()=>[];
const ruleList=()=>[current],ruleChangeProblem=()=>'',agentChangeProblem=()=>'';
async function loadRules(){} async function loadAgents(){}
async function refused(request){sent.push(request.expected_updated_at);throw Object.assign(new Error(),{code:'conflict'});}
const sendRuleChange=refused,saveAgentChange=refused;
(async()=>{
  const original={id:'same-id',scope:'global',text:'Same wording',created_at:'v1',updated_at:'v1'};
  current={...original,scope:'project',project_id:'other',created_at:'v2',updated_at:'v2'};
  openRuleRemoval(original);await submit(event);await submit(event);
  assert.deepEqual(sent.splice(0),['v1','v1']);assert.match(messages.at(-1),/создали заново/);
  // An ordinary update of the same rule still shows the new wording before a second confirmation.
  current={...original,text:'Revised wording',updated_at:'v3'};
  openRuleRemoval(original);await submit(event);await submit(event);assert.deepEqual(sent.splice(0),['v1','v3']);
  openAgentRemoval({id:'same-agent',name:'Old name',updated_at:'a1'});await submit(event);await submit(event);
  assert.deepEqual(sent,['a1','a1']);assert.match(messages.at(-1),/Закройте форму/);
})().catch(error=>{console.error(error);process.exitCode=1;});
""", sections=(("  function openRuleRemoval(", "  function ruleMatches("),
               ("  function openAgentRemoval(", "  function renderAgentDetail(")))

    def test_saving_from_a_form_reports_what_really_happened(self):
        self.evaluate(r"""
function form(alive=true){
  const log=[],submit={disabled:false,textContent:'Сохранить'};
  const panel={body:{inert:false},open:alive,alive(){return this.open;},dialog:{close(){log.push('closed');panel.open=false;}}};
  const steps=extra=>({busy:'Сохраняем…',send:async()=>({ok:true}),saved:answer=>log.push('saved:'+answer.ok),
    explain:error=>'explained:'+error.message,problem:text=>log.push('problem:'+text),lost:text=>log.push('lost:'+text),
    commands:()=>log.push('commands'),label:()=>{submit.textContent='Сохранить';},...extra});
  return {log,submit,panel,steps};
}
(async()=>{
  // Saved: the form closes and the answer is taken in.
  let f=form();await saveFromForm(f.panel,f.submit,f.steps());
  assert.deepEqual(f.log,['closed','saved:true']);
  // Saved after the user closed the form: nothing is closed twice.
  f=form(false);await saveFromForm(f.panel,f.submit,f.steps());
  assert.deepEqual(f.log,['saved:true']);
  // Refused: the form stays, says why and can be sent again.
  f=form();await saveFromForm(f.panel,f.submit,f.steps({send:async()=>{throw new Error('conflict');}}));
  assert.deepEqual(f.log,['problem:explained:conflict']);
  assert.deepEqual([f.submit.disabled,f.submit.textContent,f.panel.open,f.panel.body.inert],[false,'Сохранить',true,false]);
  // A better explanation, found by looking again, replaces the plain one.
  f=form();await saveFromForm(f.panel,f.submit,f.steps({send:async()=>{throw new Error('conflict');},failed:async()=>'reloaded'}));
  assert.deepEqual(f.log,['problem:reloaded']);
  // Refused after the form was closed: the tab carries the message.
  f=form(false);await saveFromForm(f.panel,f.submit,f.steps({send:async()=>{throw new Error('gone');}}));
  assert.deepEqual(f.log,['lost:explained:gone']);
  // The edit key stopped working: the form offers the command instead.
  rulesState.editing=false;
  f=form();await saveFromForm(f.panel,f.submit,f.steps({send:async()=>{throw new Error('not editing');}}));
  assert.deepEqual(f.log,['problem:explained:not editing','commands']);
  rulesState.editing=true;
  // A failure while the page takes a successful answer in is not a failed save.
  f=form();
  await assert.rejects(saveFromForm(f.panel,f.submit,f.steps({saved:()=>{throw new Error('render');}})),/render/);
  assert.deepEqual(f.log,['closed']);
  // While it is being sent the button cannot send it again.
  f=form();let release;const sending=saveFromForm(f.panel,f.submit,f.steps({send:()=>new Promise(resolve=>{release=resolve;})}));
  assert.deepEqual([f.submit.disabled,f.submit.textContent,f.panel.body.inert],[true,'Сохраняем…',true]);
  await saveFromForm(f.panel,f.submit,f.steps());assert.deepEqual(f.log,[]);
  release({ok:true});await sending;
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


if __name__ == "__main__":
    unittest.main()
