"""Tool navigation keeps native IDs opaque and recovers from busy workers."""

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import shutil
import subprocess
import threading
import unittest
from unittest.mock import patch

from richi import serve


MAP = serve.BASE / "map.html"


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser-script regressions")
class ToolBrowserTests(unittest.TestCase):
    def run_script(self, assertions):
        source = MAP.read_text(encoding="utf-8")
        # Execute the actual loader/rendering functions with a minimal DOM and
        # transport. The browser remains dependency-free; Node is test-only.
        render = source[source.index("  function renderTools(){"):source.index("  let toolsController=")]
        loaders = source[source.index("  function hydrateToolDetail(data){"):source.index("  async function loadTools(){")]
        script = r"""
const assert = require('node:assert/strict');
const realSetTimeout = setTimeout;
// Preserve asynchronous retry/abort ordering without spending seconds waiting.
global.setTimeout = (fn, delay) => realSetTimeout(fn, delay === 20000 ? delay : 0);
const state = {toolDetails:new Map(), toolsData:{tools:[]}, toolId:'', toolProject:'',
  toolRecord:null, toolFocusPending:false, mode:'tools', toolDetailRequest:0};
const element = {checked:false, replaceChildren(){}, setAttribute(){}, append(){}, querySelector(){return this;}};
const $ = () => element, list = value => Array.isArray(value) ? value : [];
const toolProjects = tool => list(tool.projects);
let toolDetailController = null, rendered = null;
const toolsFilterKey = () => 'false:false';
function toolEmpty(){} function toolWarning(){} function toolBreadcrumbs(){} function toolButton(){}
function renderToolCatalog(){} function closeToolRecord(){state.toolRecord=null;}
function renderToolDetail(root,tool){rendered={tool:tool.id};}
function renderToolProject(root,tool,project){rendered={tool:tool.id,project:project.id};}
function announce(){}
""" + render + loaders + "\n(async()=>{\n" + assertions + r"""
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which("node"), "-e", script], text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_prefix_ids_and_cache_keys_do_not_alias_graph_references(self):
        self.run_script(r"""
const requested=[];
global.fetch=async url=>{
  const id=new URL(url,'http://localhost').searchParams.get('tool');requested.push(id);
  return {ok:true,json:async()=>({tools:[{id,ref:'entity:'+id,title:id,projects:[],records:[]}],records:{}})};
};
for(const id of ['x','entity:x']){state.toolId=id;await loadToolDetail(id);}
assert.deepEqual(requested,['x','entity:x']);
assert.equal(state.toolDetails.size,2);
assert.equal(state.toolDetails.get('x').id,'x');
assert.equal(state.toolDetails.get('entity:x').id,'entity:x');
// Catalog lookup and project routing must use the same native-ID contract.
state.toolDetails.clear();
state.toolsData.tools=[{id:'x',ref:'entity:x',detail:true},
  {id:'entity:x',ref:'entity:entity:x',detail:true,projects:[
    {id:'p',ref:'project:p'},{id:'project:p',ref:'project:project:p'}]}];
state.toolId='entity:x';state.toolProject='project:p';renderTools();
assert.deepEqual(rendered,{tool:'entity:x',project:'project:p'});
""")

    def test_busy_workers_retry_but_other_failures_and_abort_do_not(self):
        self.run_script(r"""
let calls=0;
global.fetch=async()=>++calls<3
  ? {ok:false,status:503,json:async()=>({code:'busy'})}
  : {ok:true,json:async()=>({tools:[]})};
assert.deepEqual(await requestTools(new URLSearchParams(),new AbortController().signal),{tools:[]});
assert.equal(calls,3);
calls=0;
global.fetch=async()=>{calls++;return {ok:false,status:503,json:async()=>({code:'worker_failed'})};};
await assert.rejects(requestTools(new URLSearchParams(),new AbortController().signal),/HTTP 503/);
assert.equal(calls,1);
const controller=new AbortController();calls=0;
global.fetch=async()=>{calls++;return {ok:false,status:503,json:async()=>{
  realSetTimeout(()=>controller.abort(),0);return {code:'busy'};
}};};
await assert.rejects(requestTools(new URLSearchParams(),controller.signal),{name:'AbortError'});
assert.equal(calls,1);
""")


    def test_missing_tool_is_final_and_other_failures_keep_the_server_reason(self):
        self.run_script(r"""
let empty=null;
toolEmpty=(root,title,description,retry)=>{empty={title,retry:Boolean(retry)};};
global.fetch=async()=>({ok:false,status:404,json:async()=>({error:'Tool not found',code:'not_found'})});
state.toolsData={tools:[{id:'gone',ref:'entity:gone',title:'Gone'}]};state.toolId='gone';
await loadToolDetail('gone');
assert.equal(state.toolDetailMissing,'gone');
renderTools();
assert.deepEqual(empty,{title:'Инструмент не найден',retry:false});
// The verdict belongs to the requested tool only.
state.toolDetails.set('kept',{id:'kept',ref:'entity:kept',title:'Kept',detail:true,projects:[]});
state.toolId='kept';rendered=null;renderTools();
assert.deepEqual(rendered,{tool:'kept'});
global.fetch=async()=>({ok:false,status:503,json:async()=>({error:'Graph export timed out'})});
await assert.rejects(requestTools(new URLSearchParams(),new AbortController().signal),/HTTP 503: Graph export timed out/);
""")

    def test_busy_retry_uses_the_full_loader_deadline(self):
        self.run_script(r"""
// A deterministic clock runs real loader deadlines and retry waits together.
let clock=0,nextTimer=0;const timers=new Map();
global.setTimeout=(callback,delay)=>{const id=++nextTimer;timers.set(id,{at:clock+delay,callback});return id;};
global.clearTimeout=id=>timers.delete(id);
async function settle(){for(let i=0;i<12;i++)await Promise.resolve();}
async function advance(delay){
  const until=clock+delay;await settle();
  for(;;){
    const next=[...timers].sort((a,b)=>a[1].at-b[1].at)[0];
    if(!next||next[1].at>until)break;
    clock=next[1].at;timers.delete(next[0]);next[1].callback();await settle();
  }
  clock=until;await settle();
}
let calls=0;
global.fetch=async()=>{
  calls++;
  return clock<13000
    ? {ok:false,status:503,json:async()=>({code:'busy'})}
    : {ok:true,json:async()=>({tools:[{id:'slow',title:'Slow',projects:[],records:[]}],records:{}})};
};
state.toolId='slow';const slow=loadToolDetail('slow');
await advance(12000);assert.equal(state.toolDetailLoading,true);
await advance(2000);await slow;
assert.equal(state.toolDetails.get('slow').id,'slow');
assert.equal(state.toolDetailError,'');
assert.equal(state.toolDetailLoading,false);
// A permanently busy worker must stop at the same 20-second deadline.
global.fetch=async()=>{calls++;return {ok:false,status:503,json:async()=>({code:'busy'})};};
state.toolId='forever';const forever=loadToolDetail('forever');
await advance(19999);assert.equal(state.toolDetailLoading,true);
await advance(1);await forever;
assert.equal(state.toolDetailLoading,false);
assert.match(state.toolDetailError,/20/);
assert.equal(state.toolDetails.has('forever'),false);
const stoppedCalls=calls;await advance(60000);assert.equal(calls,stoppedCalls);
assert.equal(timers.size,0);
""")

    def test_new_tool_waits_for_the_aborted_requests_worker(self):
        self.run_script(r"""
let busy=false;const requested=[];
global.fetch=async(url,options)=>{
  const id=new URL(url,'http://localhost').searchParams.get('tool');requested.push(id);
  if(id==='first'){
    busy=true;
    return new Promise((resolve,reject)=>options.signal.addEventListener('abort',
      ()=>reject(new DOMException('Request aborted','AbortError')),{once:true}));
  }
  if(busy){busy=false;return {ok:false,status:503,json:async()=>({code:'busy'})};}
  return {ok:true,json:async()=>({tools:[{id,title:id,projects:[],records:[]}],records:{}})};
};
state.toolId='first';const first=loadToolDetail('first');
await Promise.resolve();state.toolId='second';await loadToolDetail('second');await first;
assert.deepEqual(requested,['first','second','second']);
assert.deepEqual([...state.toolDetails.keys()],['second']);
assert.equal(state.toolDetailError,'');
assert.equal(state.toolDetailLoading,false);
""")


class ToolBusyAPITests(unittest.TestCase):
    def test_busy_response_is_explicit_and_lock_recovers(self):
        # No store is opened: the bounded worker is replaced with a held request.
        server = serve.LoopbackHTTPServer(("127.0.0.1", 0), serve.handler_for("/unread-synthetic-database"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        entered, release = threading.Event(), threading.Event()

        def worker(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release worker")
            return 200, {"tools": []}

        def request():
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=6)
            try:
                connection.request("GET", "/api/tools?tool=entity%3Asynthetic")
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        try:
            with patch.object(serve, "_run_graph_request", side_effect=worker), ThreadPoolExecutor(1) as pool:
                pending = pool.submit(request)
                try:
                    self.assertTrue(entered.wait(3))
                    status, body = request()
                    self.assertEqual((status, body["code"]), (503, "busy"))
                finally:
                    release.set()
                self.assertEqual(pending.result()[0], 200)
                self.assertEqual(request()[0], 200)
        finally:
            release.set()
            server.shutdown()
            thread.join(3)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
