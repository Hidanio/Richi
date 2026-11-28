'use strict';
  // scenarios-pure: begin. No DOM here: tests/test_map_scenarios.py runs these lines under Node.
  const SCN_ID_RE=/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$/;
  const SCN_STEP_ID_RE=/^[a-z][a-z0-9_-]{0,47}$/;
  const SCN_INPUT_NAME_RE=/^[a-z][a-z0-9_]{0,47}$/;
  const SCN_NODE_W=224,SCN_NODE_H=98,SCN_COL_GAP=92,SCN_ROW_GAP=34,SCN_PAD=36;
  const SCN_STEP_TYPES=[
    {id:'instruction',label:'Инструкция',role:'agent',desc:'Свободная инструкция агенту'},
    {id:'skill',label:'Скилл',role:'agent',desc:'Вызов скилла агента'},
    {id:'command',label:'Команда',role:'agent',desc:'Запуск shell-команды (шаблоны через | shell)'},
    {id:'gate',label:'Вопрос человеку',role:'human',desc:'Останавливает запуск до ответа в чате'},
    {id:'checklist',label:'Чек-лист',role:'human',desc:'Контрольный список для подтверждения человеком'},
    {id:'memory.recall',label:'Поиск в памяти',role:'richi',desc:'Автоматический поиск в Richi'},
    {id:'memory.brief',label:'Сводка из памяти',role:'richi',desc:'Автоматическая сводка Richi по вопросу + прошлые запуски'},
    {id:'memory.record',label:'Записать в память',role:'richi',desc:'Сохраняет подтверждённый вывод в базу знаний Richi'},
    {id:'compare',label:'Сравнить с baseline',role:'richi',desc:'Сравнивает метрики шага с эталонным прошлым запуском'},
    {id:'condition',label:'Условие',role:'richi',desc:'Вычисление выражения для ветвления'},
  ];
  const SCN_STEP_META=Object.fromEntries(SCN_STEP_TYPES.map(item=>[item.id,item]));
  const SCN_INPUT_TYPES=['string','text','number','boolean','select','multiselect','secret'];
  const SCN_ENTRY_KINDS=['task','decision','fact','note','investigation','experiment','procedure'];

  function scnShellQuote(value){
    const text=String(value??'');
    return /^[A-Za-z0-9_./:@%+,-]+$/.test(text)?text:"'"+text.replace(/'/g,"'\"'\"'")+"'";
  }
  function scenarioStepRole(type){return SCN_STEP_META[type]?.role||'agent';}
  function scenarioDefaultParams(type){
    if(type==='skill')return {skill:{default:''},instruction:''};
    if(type==='command')return {command:''};
    if(type==='gate')return {question:'',options:['Да','Нет'],allow_comment:true};
    if(type==='checklist')return {items:[{text:'Проверить результат',required:true}]};
    if(type==='memory.recall')return {query:'',limit:10};
    if(type==='memory.brief')return {question:'',limit:10,include_past_runs:true};
    if(type==='memory.record')return {kind:'decision',title:'',summary:'',knowledge_state:'confirmed',link_task:true};
    if(type==='compare')return {step:'',metrics:[]};
    if(type==='condition')return {expr:''};
    return {text:''};
  }
  function scenarioAncestors(steps,stepId){
    const byId=new Map((Array.isArray(steps)?steps:[]).map(step=>[step.id,step]));
    const visited=new Set(),stack=Array.isArray(byId.get(stepId)?.needs)?[...byId.get(stepId).needs]:[];
    while(stack.length){
      const cur=stack.pop();
      if(!cur||visited.has(cur))continue;
      visited.add(cur);
      const node=byId.get(cur);
      if(node&&Array.isArray(node.needs))for(const dep of node.needs)if(!visited.has(dep))stack.push(dep);
    }
    return visited;
  }
  function scenarioWouldCycle(steps,fromId,toId){
    if(!fromId||!toId||fromId===toId)return true;
    return scenarioAncestors(steps,fromId).has(toId);
  }
  function scenarioAutoLayout(steps){
    const items=Array.isArray(steps)?steps:[];
    const byId=new Map(items.map(step=>[step.id,step]));
    const inDeg=new Map(),children=new Map(),depth=new Map();
    for(const step of items){inDeg.set(step.id,0);children.set(step.id,[]);depth.set(step.id,0);}
    for(const step of items){
      const seen=new Set();
      for(const need of Array.isArray(step.needs)?step.needs:[]){
        if(!byId.has(need)||need===step.id||seen.has(need))continue;
        seen.add(need);
        inDeg.set(step.id,inDeg.get(step.id)+1);
        children.get(need).push(step.id);
      }
    }
    const queue=[];
    for(const step of items)if(inDeg.get(step.id)===0)queue.push(step.id);
    const ordered=[];
    while(queue.length){
      const id=queue.shift();
      ordered.push(id);
      const d=depth.get(id);
      for(const nxt of children.get(id)){
        if(d+1>depth.get(nxt))depth.set(nxt,d+1);
        const rem=inDeg.get(nxt)-1;
        inDeg.set(nxt,rem);
        if(rem===0)queue.push(nxt);
      }
    }
    for(const step of items)if(!ordered.includes(step.id))ordered.push(step.id);
    const cols=new Map();
    for(const id of ordered){
      const col=depth.get(id)||0;
      if(!cols.has(col))cols.set(col,[]);
      cols.get(col).push(id);
    }
    const positions={};
    for(const [col,ids] of cols.entries()){
      ids.forEach((id,row)=>{
        positions[id]={x:SCN_PAD+col*(SCN_NODE_W+SCN_COL_GAP),y:SCN_PAD+row*(SCN_NODE_H+SCN_ROW_GAP)};
      });
    }
    return positions;
  }
  function scenarioEdgePath(x1,y1,x2,y2){
    const dx=Math.max(48,Math.abs(x2-x1)*0.48);
    if(x2<x1-16){
      const loopY=(y1+y2)/2+(y2>=y1?64:-64);
      return `M${Math.round(x1)},${Math.round(y1)} C${Math.round(x1+72)},${Math.round(y1)} ${Math.round(x1+72)},${Math.round(loopY)} ${Math.round((x1+x2)/2)},${Math.round(loopY)} S${Math.round(x2-72)},${Math.round(y2)} ${Math.round(x2)},${Math.round(y2)}`;
    }
    return `M${Math.round(x1)},${Math.round(y1)} C${Math.round(x1+dx)},${Math.round(y1)} ${Math.round(x2-dx)},${Math.round(y2)} ${Math.round(x2)},${Math.round(y2)}`;
  }
  function scnInt(value){
    if(typeof value==='number')return Number.isInteger(value)?value:null;
    if(typeof value==='string'&&value.trim()!==''){const n=Number(value.trim());return Number.isInteger(n)?n:null;}
    return null;
  }
  function scenarioNormalizeDraft(raw){
    const src=raw&&typeof raw==='object'?raw:{};
    const out={format:'richi-scenario',format_version:1,id:String(src.id||'').trim(),title:String(src.title||'').trim()};
    if(String(src.description||'').trim())out.description=String(src.description).trim();
    if(String(src.when_to_use||'').trim())out.when_to_use=String(src.when_to_use).trim();
    if(String(src.run_title||'').trim())out.run_title=String(src.run_title).trim();
    const tags=Array.isArray(src.tags)?src.tags.map(t=>String(t||'').trim()).filter(Boolean):[];
    if(tags.length)out.tags=[...new Set(tags)];
    const inputs={};
    if(src.inputs&&typeof src.inputs==='object'&&!Array.isArray(src.inputs)){
      for(const [rawName,spec] of Object.entries(src.inputs)){
        const name=String(rawName||'').trim();
        if(!name||!spec||typeof spec!=='object')continue;
        const type=SCN_INPUT_TYPES.includes(spec.type)?spec.type:'string';
        const clean={type};
        if(String(spec.title||'').trim())clean.title=String(spec.title).trim();
        if(String(spec.description||'').trim())clean.description=String(spec.description).trim();
        if(spec.required)clean.required=true;
        if(spec.picker)clean.picker=String(spec.picker);
        if(type==='select'||type==='multiselect'){
          const opts=Array.isArray(spec.options)?[...new Set(spec.options.map(v=>String(v||'').trim()).filter(Boolean))]:[];
          if(opts.length)clean.options=opts;
        }
        if(spec.default!==undefined&&spec.default!==''&&type!=='secret'){
          if(type==='number'){if(typeof spec.default==='number'&&Number.isFinite(spec.default))clean.default=spec.default;}
          else if(type==='boolean'){if(typeof spec.default==='boolean')clean.default=spec.default;}
          else if(type==='select'){if(Array.isArray(clean.options)&&clean.options.includes(spec.default))clean.default=spec.default;}
          else if(type==='multiselect'){
            if(Array.isArray(spec.default)&&Array.isArray(clean.options)){
              const kept=[...new Set(spec.default.filter(v=>clean.options.includes(v)))];
              if(kept.length)clean.default=kept;
            }
          }else{clean.default=spec.default;}
        }
        inputs[name]=clean;
      }
    }
    if(Object.keys(inputs).length)out.inputs=inputs;
    const rawSteps=Array.isArray(src.steps)?src.steps:[];
    const stepIds=new Set(rawSteps.map(s=>String(s?.id||'').trim()).filter(Boolean));
    out.steps=rawSteps.map((step,index)=>{
      const rawType=String(step?.type||'').trim();
      const type=rawType||'instruction';
      const id=String(step?.id||'').trim();
      const title=String(step?.title||'').trim()||id;
      let needs;
      if(step?.needs===undefined){
        const prevId=index>0?String(rawSteps[index-1]?.id||'').trim():'';
        needs=prevId&&prevId!==id&&stepIds.has(prevId)?[prevId]:[];
      }else{
        needs=Array.isArray(step.needs)?[...new Set(step.needs.map(n=>String(n||'').trim()).filter(n=>n&&n!==id&&stepIds.has(n)))]:[];
      }
      const p=step?.params&&typeof step.params==='object'?step.params:{};
      let params={};
      if(type==='instruction')params={text:String(p.text||'')};
      else if(type==='skill'){
        const rawSk=p.skill,sk={};
        if(typeof rawSk==='string'&&rawSk.trim())sk.default=rawSk.trim();
        else if(rawSk&&typeof rawSk==='object'&&!Array.isArray(rawSk)){
          if(typeof rawSk.default==='string'&&rawSk.default.trim())sk.default=rawSk.default.trim();
          if(rawSk.per_agent&&typeof rawSk.per_agent==='object'&&!Array.isArray(rawSk.per_agent)){
            const pa={};
            for(const [ak,av] of Object.entries(rawSk.per_agent))if(String(ak).trim()&&typeof av==='string'&&av.trim())pa[String(ak).trim()]=av.trim();
            if(Object.keys(pa).length)sk.per_agent=pa;
          }
        }
        params={skill:sk};
        if(String(p.instruction||'').trim())params.instruction=String(p.instruction);
      }else if(type==='command'){
        params={command:String(p.command||'')};
        if(String(p.cwd||'').trim())params.cwd=String(p.cwd).trim();
        const expExit=scnInt(p.expect_exit),tailLines=scnInt(p.tail_lines);
        if(expExit!==null)params.expect_exit=expExit;
        if(tailLines!==null)params.tail_lines=tailLines;
      }else if(type==='gate'){
        params={question:String(p.question||'')};
        const opts=Array.isArray(p.options)?[...new Set(p.options.map(v=>String(v||'').trim()).filter(Boolean))]:[];
        if(opts.length)params.options=opts;
        if(p.allow_comment!==undefined)params.allow_comment=Boolean(p.allow_comment);
      }else if(type==='checklist'){
        const items=Array.isArray(p.items)?p.items.map(it=>{
          if(typeof it==='string'){const t=it.trim();return t?{text:t,required:true}:null;}
          if(it&&typeof it==='object'){const t=String(it.text||'').trim();if(!t)return null;const o={text:t};if(typeof it.required==='boolean')o.required=it.required;return o;}
          return null;
        }).filter(Boolean):[];
        params={items};
      }else if(type==='memory.recall'){
        params={query:String(p.query||'')};
        const lim=scnInt(p.limit);
        if(lim!==null)params.limit=lim;
        const kinds=Array.isArray(p.kinds)?p.kinds.filter(k=>SCN_ENTRY_KINDS.includes(k)):[];
        if(kinds.length)params.kinds=kinds;
      }else if(type==='memory.brief'){
        params={question:String(p.question||'')};
        const lim=scnInt(p.limit);
        if(lim!==null)params.limit=lim;
        if(p.include_past_runs===false)params.include_past_runs=false;
        if(p.include_learnings===true)params.include_learnings=true;
      }else if(type==='memory.record'){
        params={
          kind:SCN_ENTRY_KINDS.includes(p.kind)?p.kind:'decision',
          title:String(p.title||''),
          summary:String(p.summary||''),
        };
        if(String(p.entry_id||'').trim())params.entry_id=String(p.entry_id).trim();
        if(p.knowledge_state==='hypothesis'||p.knowledge_state==='confirmed')params.knowledge_state=p.knowledge_state;
        if(p.link_task===false)params.link_task=false;
        const tags=Array.isArray(p.tags)?p.tags.map(t=>String(t||'').trim()).filter(Boolean).slice(0,10):[];
        if(tags.length)params.tags=tags;
      }else if(type==='compare'){
        params={step:String(p.step||'').trim()};
        const metrics=Array.isArray(p.metrics)?p.metrics.map(m=>String(m||'').trim()).filter(Boolean).slice(0,20):[];
        if(metrics.length)params.metrics=metrics;
        if(String(p.baseline_run||'').trim())params.baseline_run=String(p.baseline_run).trim();
      }else if(type==='condition'){
        params={expr:String(p.expr||'')};
      }else{
        params=Object.assign({},p);
      }
      const cleanStep={id,type,title,needs,params};
      if(String(step?.if||'').trim())cleanStep.if=String(step.if).trim();
      const role=scenarioStepRole(type);
      if(role==='agent'&&step?.continue_on_failure)cleanStep.continue_on_failure=true;
      if(role==='agent'&&step?.executor&&typeof step.executor==='object'&&String(step.executor.agent||'').trim()){
        cleanStep.executor={agent:String(step.executor.agent).trim()};
      }
      if((role==='agent'||role==='human')&&step?.matrix&&typeof step.matrix==='object'&&Object.keys(step.matrix).length){
        cleanStep.matrix=step.matrix;
        if(step.matrix_mode==='orthogonal'||step.matrix_mode==='replicated')cleanStep.matrix_mode=step.matrix_mode;
      }
      if(role==='agent'&&step?.outputs&&typeof step.outputs==='object'&&Object.keys(step.outputs).length)cleanStep.outputs=step.outputs;
      if(role==='agent'&&Array.isArray(step?.checks)&&step.checks.length)cleanStep.checks=step.checks;
      const retryMax=role==='agent'&&step?.retry&&typeof step.retry==='object'?scnInt(step.retry.max):null;
      if(retryMax!==null&&retryMax>0)cleanStep.retry={max:retryMax};
      if(role==='agent'&&step?.hints&&typeof step.hints==='object'){
        const hints={};
        if(typeof step.hints.subagent==='boolean')hints.subagent=step.hints.subagent;
        if(typeof step.hints.worktree==='boolean')hints.worktree=step.hints.worktree;
        const tmin=scnInt(step.hints.timeout_minutes);
        if(tmin!==null&&tmin>0)hints.timeout_minutes=tmin;
        if(Object.keys(hints).length)cleanStep.hints=hints;
      }
      return cleanStep;
    });
    if(src.layout&&typeof src.layout==='object'&&src.layout.positions&&typeof src.layout.positions==='object'){
      const positions={};
      for(const step of out.steps){
        const pos=src.layout.positions[step.id];
        if(pos&&Number.isFinite(Number(pos.x))&&Number.isFinite(Number(pos.y))){
          positions[step.id]={x:Math.round(Number(pos.x)),y:Math.round(Number(pos.y))};
        }
      }
      if(Object.keys(positions).length)out.layout={positions};
    }
    return out;
  }
  function scenarioValidateDraft(def){
    if(!def||typeof def!=='object')return 'Определение сценария должно быть объектом.';
    if(!SCN_ID_RE.test(def.id||''))return 'ID сценария: 1–100 символов (латиница, цифры, «.», «_», «:», «-»), начиная с буквы или цифры.';
    if(!String(def.title||'').trim())return 'Укажите название сценария.';
    if(def.inputs&&typeof def.inputs==='object'){
      for(const [name,spec] of Object.entries(def.inputs)){
        if(!SCN_INPUT_NAME_RE.test(name))return 'Имя параметра «'+name+'»: строчная латиница, цифры и «_», начиная с буквы.';
        if((spec.type==='select'||spec.type==='multiselect')&&(!Array.isArray(spec.options)||!spec.options.length)){
          return 'Для параметра «'+name+'» типа '+spec.type+' укажите хотя бы один вариант.';
        }
      }
    }
    if(!Array.isArray(def.steps)||!def.steps.length)return 'Добавьте хотя бы один шаг в пайплайн.';
    const seen=new Set();
    for(const step of def.steps){
      if(!SCN_STEP_ID_RE.test(step.id||''))return 'ID шага «'+(step.id||'')+'»: строчная латиница, цифры, «_» или «-», начиная с буквы.';
      if(seen.has(step.id))return 'Повторяющийся ID шага: «'+step.id+'».';
      seen.add(step.id);
      if(!String(step.title||'').trim())return 'Укажите название шага «'+step.id+'».';
      if(!SCN_STEP_META[step.type])return 'Шаг «'+step.id+'»: неизвестный тип шага «'+step.type+'».';
      const p=step.params||{};
      if(step.type==='instruction'&&!String(p.text||'').trim())return 'Шаг «'+step.id+'»: заполните текст инструкции.';
      if(step.type==='skill'){
        const sk=p.skill,hasDefault=Boolean(sk&&typeof sk==='object'&&typeof sk.default==='string'&&sk.default.trim());
        const hasPer=Boolean(sk&&typeof sk==='object'&&sk.per_agent&&Object.keys(sk.per_agent).length);
        if(!hasDefault&&!hasPer)return 'Шаг «'+step.id+'»: выберите или укажите скилл.';
      }
      if(step.type==='command'){
        if(!String(p.command||'').trim())return 'Шаг «'+step.id+'»: укажите команду.';
        if(/[\r\n]/.test(String(p.command||'')))return 'Шаг «'+step.id+'»: команда должна быть в одну строку (без переносов).';
        if(p.expect_exit!==undefined&&(p.expect_exit<0||p.expect_exit>255))return 'Шаг «'+step.id+'»: ожидаемый код выхода должен быть от 0 до 255.';
        if(p.tail_lines!==undefined&&(p.tail_lines<0||p.tail_lines>500))return 'Шаг «'+step.id+'»: число строк лога должно быть от 0 до 500.';
      }
      if(step.type==='gate'){
        if(!String(p.question||'').trim())return 'Шаг «'+step.id+'»: укажите вопрос человеку.';
        if(Array.isArray(p.options)&&p.options.length&&(p.options.length<2||p.options.length>10))return 'Шаг «'+step.id+'»: если заданы варианты ответа, их должно быть от 2 до 10.';
      }
      if(step.type==='checklist'&&(!Array.isArray(p.items)||!p.items.length))return 'Шаг «'+step.id+'»: добавьте хотя бы один пункт чек-листа.';
      if(step.type==='memory.recall'){
        if(!String(p.query||'').trim())return 'Шаг «'+step.id+'»: укажите запрос для поиска в памяти.';
        if(p.limit!==undefined&&(p.limit<1||p.limit>50))return 'Шаг «'+step.id+'»: максимум записей должен быть от 1 до 50.';
      }
      if(step.type==='memory.brief'){
        if(!String(p.question||'').trim())return 'Шаг «'+step.id+'»: укажите вопрос для сводки.';
        if(p.limit!==undefined&&(p.limit<1||p.limit>50))return 'Шаг «'+step.id+'»: максимум записей должен быть от 1 до 50.';
      }
      if(step.type==='memory.record'){
        if(!SCN_ENTRY_KINDS.includes(p.kind))return 'Шаг «'+step.id+'»: выберите допустимый тип записи.';
        if(!String(p.title||'').trim())return 'Шаг «'+step.id+'»: укажите заголовок записи в память.';
        if(!String(p.summary||'').trim())return 'Шаг «'+step.id+'»: укажите текст записи в память.';
      }
      if(step.type==='compare'){
        if(!String(p.step||'').trim())return 'Шаг «'+step.id+'»: выберите предыдущий шаг для сравнения с baseline.';
      }
      if(step.type==='condition'&&!String(p.expr||'').trim())return 'Шаг «'+step.id+'»: укажите условие.';
      if(step.retry&&step.retry.max!==undefined&&(step.retry.max<0||step.retry.max>5))return 'Шаг «'+step.id+'»: число повторов должно быть от 0 до 5.';
    }
    for(const step of def.steps){
      for(const need of Array.isArray(step.needs)?step.needs:[]){
        if(!seen.has(need))return 'Шаг «'+step.id+'» ссылается на несуществующий шаг «'+need+'».';
        if(scenarioWouldCycle(def.steps,need,step.id))return 'В графе шагов обнаружен цикл с участием «'+step.id+'» и «'+need+'».';
      }
    }
    return '';
  }
  function scenarioApplyCommand(request,storeArgs=[]){
    const prefix=['richi',...((Array.isArray(storeArgs)?storeArgs:[]).map(scnShellQuote)),'scenario','apply','--json','-'].join(' ');
    const payload=JSON.stringify(request,null,2);
    let delim='RICHI_SCENARIO';
    while(payload.includes(delim))delim+='_END';
    return prefix+" <<'"+delim+"'\n"+payload+"\n"+delim;
  }
  function scenarioRunCommand(scenarioId,scope,projectId,inputValues={},storeArgs=[]){
    const parts=['richi',...((Array.isArray(storeArgs)?storeArgs:[]).map(scnShellQuote)),'scenario','run',scnShellQuote(scenarioId)];
    if(projectId)parts.push('--project',scnShellQuote(projectId));
    for(const [k,v] of Object.entries(inputValues||{})){
      if(v===undefined||v===null||v==='')continue;
      const text=Array.isArray(v)?v.join(','):String(v);
      parts.push('--input',scnShellQuote(k+'='+text));
    }
    return parts.join(' ');
  }
  function scenarioAnswerCommand(runId,stepKey,value,comment='',storeArgs=[]){
    const parts=['richi',...((Array.isArray(storeArgs)?storeArgs:[]).map(scnShellQuote)),'scenario','answer',scnShellQuote(runId),scnShellQuote(stepKey)];
    if(String(comment||'').trim())parts.push('--comment',scnShellQuote(String(comment).trim()));
    const valStr=String(value??'');
    if(valStr.startsWith('-'))parts.push('--');
    parts.push(scnShellQuote(valStr));
    return parts.join(' ');
  }
  function scenarioParseTokens(text){
    const raw=String(text||''),parts=[];
    const re=/\{\{\s*([^}]+?)\s*\}\}/g;
    let last=0,m;
    while((m=re.exec(raw))!==null){
      if(m.index>last)parts.push({type:'text',value:raw.slice(last,m.index)});
      const expr=m[1].trim(),segs=expr.split('|').map(s=>s.trim());
      const path=segs[0]||'',filter=segs.slice(1).join(' | ');
      const dot=path.split('.');
      const rawTarget=dot[1]||'',target=rawTarget.replace(/\[\d+\]$/,'');
      parts.push({type:'token',raw:m[0],expr,path,filter,root:dot[0]||'',target,rawTarget,field:dot.slice(2).join('.')});
      last=m.index+m[0].length;
    }
    if(last<raw.length)parts.push({type:'text',value:raw.slice(last)});
    return parts;
  }
  function scenarioLintDraft(rawDef,installedSkillNames=null){
    const rawSteps=Array.isArray(rawDef?.steps)?rawDef.steps:[];
    const rawIds=new Set(rawSteps.map(s=>String(s?.id||'').trim()).filter(Boolean));
    const def=scenarioNormalizeDraft(rawDef);
    const issues=[],byStep={};
    const push=(stepId,severity,message)=>{
      issues.push({stepId,severity,message});
      if(stepId){
        const cur=byStep[stepId]||(byStep[stepId]={severity:'ok',messages:[]});
        cur.messages.push(message);
        if(severity==='err'||(severity==='warn'&&cur.severity!=='err'))cur.severity=severity;
      }
    };
    if(!SCN_ID_RE.test(def.id||''))push(null,'err','Неверный ID сценария (нужны латиница, цифры, «.», «_», «:», «-»).');
    if(!String(def.title||'').trim())push(null,'err','Не указано название сценария.');
    if(!Array.isArray(def.steps)||!def.steps.length){push(null,'err','Добавьте хотя бы один шаг.');return {ok:false,issues,byStep};}
    for(const rs of rawSteps){
      const sid=String(rs?.id||'').trim();
      if(Array.isArray(rs?.needs)){
        for(const need of rs.needs){
          const n=String(need||'').trim();
          if(n&&n!==sid&&!rawIds.has(n))push(sid||null,'err','Ссылка на несуществующий шаг «'+n+'» в needs.');
        }
      }
    }
    const inputs=def.inputs&&typeof def.inputs==='object'?def.inputs:{};
    const secrets=new Set(Object.entries(inputs).filter(([,s])=>s?.type==='secret').map(([k])=>k));
    const stepById=new Map();
    for(const step of def.steps){
      if(!SCN_STEP_ID_RE.test(step.id||''))push(step.id,'err','Некорректный ID шага.');
      if(stepById.has(step.id))push(step.id,'err','Повторяющийся ID шага.');
      stepById.set(step.id,step);
    }
    const knownSkills=Array.isArray(installedSkillNames)?new Set(installedSkillNames):null;
    const checkTemplates=(step,text,opts={})=>{
      const ancestors=scenarioAncestors(def.steps,step.id);
      for(const part of scenarioParseTokens(text)){
        if(part.type!=='token')continue;
        if(opts.requireShell&&!/(^|\|\s*)shell\s*$/.test(part.filter)){
          push(step.id,'err','В команде подстановка '+part.raw+' должна заканчиваться на | shell.');
        }
        if(part.root==='inputs'){
          if(!inputs[part.target])push(step.id,'err','Необъявленный параметр «'+part.target+'» в '+part.raw+'.');
          else if(opts.forbidSecret&&secrets.has(part.target))push(step.id,'err','Секрет «'+part.target+'» нельзя использовать в этом поле.');
        }else if(part.root==='steps'){
          const src=stepById.get(part.target);
          if(!src)push(step.id,'err','Ссылка на несуществующий шаг «'+part.target+'» в '+part.raw+'.');
          else if(!ancestors.has(part.target))push(step.id,'err','Шаг «'+part.target+'» не является предком — сначала соедините его стрелкой (needs).');
          else if(src.matrix&&(src.type==='gate'||src.type==='checklist')){
            const topField=String(part.field||'').split('.')[0].replace(/\[\d+\]$/,'');
            if(topField&&!['items','status'].includes(topField))push(step.id,'err','Для матричного шага «'+part.target+'» доступны только поля items и status (не «'+topField+'»).');
          }
        }
      }
    };
    for(const step of def.steps){
      const p=step.params||{};
      for(const need of Array.isArray(step.needs)?step.needs:[]){
        if(!stepById.has(need))push(step.id,'err','Ссылка на несуществующий шаг «'+need+'».');
        else if(scenarioWouldCycle(def.steps,need,step.id))push(step.id,'err','Обнаружен цикл зависимостей через «'+need+'».');
      }
      if(step.retry&&step.retry.max!==undefined&&(step.retry.max<0||step.retry.max>5))push(step.id,'err','Повторы должны быть в диапазоне 0–5.');
      if(step.type==='instruction'){
        if(!String(p.text||'').trim())push(step.id,'err','Заполните текст задания для агента.');
        else checkTemplates(step,p.text);
      }else if(step.type==='skill'){
        const sk=p.skill||{},defName=typeof sk.default==='string'?sk.default.trim():'';
        const pa=sk.per_agent&&typeof sk.per_agent==='object'?Object.values(sk.per_agent):[];
        if(!defName&&!pa.length)push(step.id,'err','Не выбран скилл.');
        else if(knownSkills){
          for(const nm of [defName,...pa].filter(Boolean)){
            if(!knownSkills.has(nm))push(step.id,'warn','Скилл «'+nm+'» не найден среди установленных на этом компьютере.');
          }
        }
        if(p.instruction)checkTemplates(step,p.instruction);
      }else if(step.type==='command'){
        if(!String(p.command||'').trim())push(step.id,'err','Укажите консольную команду.');
        else{
          if(/[\r\n]/.test(String(p.command)))push(step.id,'err','Консольная команда должна быть в одну строку (без переносов).');
          checkTemplates(step,p.command,{requireShell:true});
        }
        if(p.expect_exit!==undefined&&(p.expect_exit<0||p.expect_exit>255))push(step.id,'err','Ожидаемый код выхода должен быть от 0 до 255.');
        if(p.tail_lines!==undefined&&(p.tail_lines<0||p.tail_lines>500))push(step.id,'err','Число строк лога должно быть от 0 до 500.');
      }else if(step.type==='gate'){
        if(!String(p.question||'').trim())push(step.id,'err','Укажите вопрос пользователю.');
        else checkTemplates(step,p.question,{forbidSecret:true});
        if(Array.isArray(p.options)&&p.options.length&&(p.options.length<2||p.options.length>10))push(step.id,'err','Вариантов ответа должно быть 0 либо от 2 до 10.');
      }else if(step.type==='checklist'){
        if(!Array.isArray(p.items)||!p.items.length)push(step.id,'err','Добавьте хотя бы один пункт чек-листа.');
        else for(const it of p.items)checkTemplates(step,it.text||'',{forbidSecret:true});
      }else if(step.type==='memory.recall'){
        if(!String(p.query||'').trim())push(step.id,'err','Укажите поисковый запрос в память.');
        else checkTemplates(step,p.query,{forbidSecret:true});
        if(p.limit!==undefined&&(p.limit<1||p.limit>50))push(step.id,'err','Максимум записей должен быть от 1 до 50.');
      }else if(step.type==='memory.brief'){
        if(!String(p.question||'').trim())push(step.id,'err','Укажите вопрос для сводки из памяти.');
        else checkTemplates(step,p.question,{forbidSecret:true});
        if(p.limit!==undefined&&(p.limit<1||p.limit>50))push(step.id,'err','Максимум записей должен быть от 1 до 50.');
      }else if(step.type==='memory.record'){
        if(!String(p.title||'').trim())push(step.id,'err','Укажите заголовок записи в память.');
        else checkTemplates(step,p.title,{forbidSecret:true});
        if(!String(p.summary||'').trim())push(step.id,'err','Укажите текст записи в память.');
        else checkTemplates(step,p.summary,{forbidSecret:true});
      }else if(step.type==='compare'){
        const target=String(p.step||'').trim();
        if(!target)push(step.id,'err','Выберите шаг-предок для сравнения с baseline.');
        else if(!scenarioAncestors(def.steps,step.id).has(target))push(step.id,'err','Шаг «'+target+'» должен быть предком в needs для compare.');
      }else if(step.type==='condition'){
        if(!String(p.expr||'').trim())push(step.id,'err','Укажите условие.');
      }else if(!SCN_STEP_META[step.type]){
        push(step.id,'err','Неизвестный тип шага «'+step.type+'».');
      }
      if(!byStep[step.id])byStep[step.id]={severity:'ok',messages:[]};
    }
    return {ok:!issues.some(i=>i.severity==='err'),issues,byStep};
  }
  function scenarioTokenKey(token){
    return String(token||'').replace(/^\{\{\s*/,'').replace(/\s*\}\}$/,'').replace(/\s*\|\s*/g,' | ').trim();
  }
  function scenarioHasToken(text,token){
    const key=scenarioTokenKey(token);if(!key)return false;
    return scenarioParseTokens(text).some(pt=>pt.type==='token'&&scenarioTokenKey(pt.raw)===key);
  }
  function scenarioToggleToken(text,token,cursorPos=null){
    const raw=String(text||''),key=scenarioTokenKey(token);
    if(!key)return {value:raw,active:false};
    const parts=scenarioParseTokens(raw);
    if(parts.some(pt=>pt.type==='token'&&scenarioTokenKey(pt.raw)===key)){
      let out='';
      for(let i=0;i<parts.length;i++){
        const pt=parts[i];
        if(pt.type==='token'&&scenarioTokenKey(pt.raw)===key){
          if(out.endsWith('(')&&parts[i+1]?.type==='text'&&parts[i+1].value.startsWith(')')){
            out=out.slice(0,-1);
            parts[i+1]={type:'text',value:parts[i+1].value.slice(1)};
          }
          continue;
        }
        out+=pt.type==='token'?pt.raw:pt.value;
      }
      out=out.replace(/[ \t]{2,}/g,' ').replace(/ ([.,;:!?])/g,'$1').trim();
      return {value:out,active:false};
    }
    const pos=typeof cursorPos==='number'&&cursorPos>=0&&cursorPos<=raw.length?cursorPos:raw.length;
    const before=raw.slice(0,pos),after=raw.slice(pos);
    const padBefore=before&&!/[\s([{"']$/.test(before)?' ':'';
    const padAfter=after&&!/^[\s.,;:!?)\]}"']/.test(after)?' ':'';
    return {value:before+padBefore+token+padAfter+after,active:true};
  }
  function scenarioDraftChanged(draft,baseline){
    return JSON.stringify(draft||null)!==JSON.stringify(baseline||null);
  }
  function scenarioAvailableStepTypes(platform){
    const blocks=Array.isArray(platform?.step_blocks)?platform.step_blocks:[];
    if(!blocks.length)return SCN_STEP_TYPES;
    const avail=new Map();
    for(const b of blocks){
      const key=b&&(b.type||b.kind);
      if(key)avail.set(key,b.available!==false);
    }
    return SCN_STEP_TYPES.filter(t=>avail.has(t.id)?avail.get(t.id):true);
  }
  function scenarioProjectBoundChecks(platform){
    const checks=Array.isArray(platform?.checks)?platform.checks:[];
    if(!checks.length)return new Set(['no_dirty_git','findings_grounded','file_exists','evidence_git']);
    return new Set(checks.filter(c=>c&&c.project_bound&&c.kind).map(c=>c.kind));
  }
  // scenarios-pure: end.

  const scenariosState={
    data:null,loading:false,error:'',request:0,
    subtab:'definitions',scope:'',query:'',
    editing:null,selectedStepId:'',viewMode:'graph',showJson:false,metaOpen:false,advOpen:false,
    camera:{x:20,y:20,k:1},cameraFitNeeded:true,lintResult:null,
    busy:false,saveError:'',outcome:'',commandText:'',
    runFilter:'all',selectedRunId:'',runDetail:null,runDetailLoading:false,runDetailLoadingId:'',runDetailError:'',runDetailRequest:0,
    logFilter:'all',
    reviewData:null,reviewLoading:false,reviewError:'',reviewRequest:0,
    reviewProject:'',reviewSeverity:'all',reviewStatus:'open',
    reviewSelected:new Set(),triageCart:[],triageBusy:new Set(),triageReloadPending:false,
  };
  let scenariosController=null,scenarioRunController=null,scenarioReviewController=null;

  const SCN_STATUS_LABEL={
    running:'Выполняется',waiting:'Ждёт ответа',paused:'На паузе',done:'Завершён',failed:'Ошибка',cancelled:'Отменён',
    pending:'Ожидает',ready:'Готов',claimed:'В работе',waiting_human:'Ждёт ответа человека',skipped:'Пропущен',blocked:'Заблокирован',
  };
  const SCN_ROLE_LABEL={richi:'Richi',agent:'Агент',human:'Человек'};
  const SCN_ACTION_LABEL={
    scenario_created:'Создан сценарий',scenario_updated:'Обновлён сценарий',scenario_removed:'Удалён сценарий',
    run_started:'Запуск стартовал',run_running:'Запуск в работе',run_waiting:'Ожидание ответа',run_done:'Запуск завершён',
    run_failed:'Запуск с ошибкой',paused:'Пауза',resumed:'Возобновлён',cancelled:'Отменён',
    claimed:'Шаг взят в работу',released:'Шаг освобождён',report_accepted:'Отчёт принят',report_rejected:'Отчёт отклонён',
    step_done:'Авто-шаг выполнен',step_failed:'Шаг завершился с ошибкой',step_skipped:'Шаг пропущен',step_blocked:'Шаг заблокирован',
    waiting_human:'Ожидание ответа человека',answered:'Ответ на вопрос',matrix_expanded:'Матрица развёрнута',
    retried:'Повтор шага',lease_expired:'Истёк lease',
    stop_enforced:'Хук Stop: продолжение шага',stop_limit_reached:'Хук Stop: достигнут лимит',
    baseline_set:'Назначен эталон (baseline)',baseline_cleared:'Снят эталон (baseline)',
    finding_feedback:'Триаж находок ревью',run_forked:'Ответвление запуска (fork)',
  };
  function scenariosNote(){
    if(scenariosState.loading&&!scenariosState.data)return 'Загружаем сценарии…';
    if(scenariosState.error&&!scenariosState.data)return 'Сценарии не загрузились';
    const count=list(scenariosState.data?.scenarios).length,runs=list(scenariosState.data?.runs).length;
    return (rulesState.editing?'Сценарии · редактирование':'Сценарии · режим команд')+' · '+count+' сохр. · '+runs+' запусков';
  }
  function scenarioProjectNames(){
    const map=new Map();
    for(const item of list(scenariosState.data?.projects))if(item&&typeof item.id==='string')map.set(item.id,item.name||item.id);
    for(const node of state.nodes)if(groupFor(node)==='project'){const id=node.id.replace(/^project:/,'');if(!map.has(id))map.set(id,titleFor(node));}
    return [...map.entries()].sort((a,b)=>a[1].localeCompare(b[1],'ru'));
  }
  function scenarioScopeText(item){
    if(item.builtin||item.scope==='builtin')return 'Встроенный шаблон';
    if(item.scope==='project'){
      const found=scenarioProjectNames().find(([id])=>id===item.project_id);
      return 'Проект · '+(found?found[1]:item.project_id);
    }
    return 'Глобальный';
  }
  async function loadScenarios(){
    if(scenariosController)scenariosController.abort();
    const request=++scenariosState.request,sent=ruleSecret;
    scenariosState.loading=true;scenariosState.error='';
    if(state.mode==='scenarios'&&!scenariosState.editing)renderScenarios();
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),20000);scenariosController=controller;
    try{
      const response=await fetch('/api/scenarios',{cache:'no-store',headers:sent?{[RULE_HEADER]:sent}:{},signal:controller.signal});
      let data=null;try{data=await response.json();}catch{data=null;}
      if(!response.ok)throw new Error(data&&data.error||'Сервер вернул HTTP '+response.status);
      if(!data||!Array.isArray(data.scenarios))throw new Error('Неожиданный формат ответа сервера');
      if(request!==scenariosState.request)return;
      if(sent!==ruleSecret){scenariosState.loading=false;loadScenarios();return;}
      scenariosState.data=data;setMapEditing(data.editing===true,sent);
      if(data.editing===true)scenariosState.triageCart=[];
      const knownRun=list(data.runs).some(r=>r.id===scenariosState.selectedRunId)||list(data.waiting_queue).some(w=>w.run_id===scenariosState.selectedRunId);
      if(scenariosState.selectedRunId&&!knownRun){
        scenariosState.selectedRunId='';scenariosState.runDetail=null;
      }
      if(scenariosState.subtab==='runs'&&scenariosState.selectedRunId)loadScenarioRun(scenariosState.selectedRunId);
      if(scenariosState.subtab==='review')loadScenarioReview(scenariosState.reviewProject);
    }catch(error){
      if(request!==scenariosState.request)return;
      scenariosState.error=error.name==='AbortError'?'Сервер не ответил за 20 секунд.':error.message;
    }finally{
      clearTimeout(timeout);
      if(request===scenariosState.request){
        scenariosState.loading=false;scenariosController=null;
        if(state.mode==='scenarios'){
          $('data-note').textContent=scenariosNote();
          if(!scenariosState.editing){renderProjectOptions();renderScenarios();}
        }
      }
    }
  }
  async function loadScenarioRun(runId){
    if(!runId)return;
    if(scenarioRunController)scenarioRunController.abort();
    const request=++scenariosState.runDetailRequest;
    scenariosState.selectedRunId=runId;scenariosState.runDetailLoading=true;scenariosState.runDetailLoadingId=runId;scenariosState.runDetailError='';
    renderScenarios();
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),20000);scenarioRunController=controller;
    try{
      const response=await fetch('/api/scenario-run?id='+encodeURIComponent(runId),{cache:'no-store',signal:controller.signal});
      let data=null;try{data=await response.json();}catch{data=null;}
      if(!response.ok)throw new Error(data&&data.error||'Сервер вернул HTTP '+response.status);
      if(request!==scenariosState.runDetailRequest||scenariosState.selectedRunId!==runId)return;
      scenariosState.runDetail=data;
    }catch(error){
      if(request!==scenariosState.runDetailRequest||scenariosState.selectedRunId!==runId)return;
      scenariosState.runDetailError=error.name==='AbortError'?'Сервер не ответил за 20 секунд.':error.message;
    }finally{
      clearTimeout(timeout);
      if(request===scenariosState.runDetailRequest){scenariosState.runDetailLoading=false;scenariosState.runDetailLoadingId='';scenarioRunController=null;if(state.mode==='scenarios')renderScenarios();}
    }
  }
  function availableScenarioStepTypes(){
    return scenarioAvailableStepTypes(scenariosState.data?.platform);
  }
  async function loadScenarioReview(projectId='',opts={}){
    if(scenarioReviewController)scenarioReviewController.abort();
    const request=++scenariosState.reviewRequest,sent=ruleSecret;
    const nextProject=projectId||'';
    if(nextProject!==scenariosState.reviewProject)scenariosState.reviewSelected.clear();
    scenariosState.reviewProject=nextProject;
    scenariosState.reviewLoading=true;
    if(!opts.preserveError)scenariosState.reviewError='';
    if(state.mode==='scenarios'&&scenariosState.subtab==='review')renderScenarios();
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),20000);scenarioReviewController=controller;
    try{
      const qs=nextProject?('?project='+encodeURIComponent(nextProject)):'';
      const reviewEndpoint=list(scenariosState.data?.platform?.domain_views).find(v=>v&&v.id==='review')?.endpoint||'/api/scenario-review';
      const response=await fetch(reviewEndpoint+qs,{cache:'no-store',headers:sent?{[RULE_HEADER]:sent}:{},signal:controller.signal});
      let data=null;try{data=await response.json();}catch{data=null;}
      if(!response.ok)throw new Error(data&&data.error||'Сервер вернул HTTP '+response.status);
      if(request!==scenariosState.reviewRequest)return;
      if(sent!==ruleSecret){scenariosState.reviewLoading=false;loadScenarioReview(nextProject,opts);return;}
      scenariosState.reviewData=data;
      if(typeof data.editing==='boolean'){
        setMapEditing(data.editing===true,sent);
        if(data.editing===true)scenariosState.triageCart=[];
      }
    }catch(error){
      if(request!==scenariosState.reviewRequest)return;
      scenariosState.reviewError=error.name==='AbortError'?'Сервер не ответил за 20 секунд.':error.message;
    }finally{
      clearTimeout(timeout);
      if(request===scenariosState.reviewRequest){
        scenariosState.reviewLoading=false;scenarioReviewController=null;
        if(state.mode==='scenarios'&&scenariosState.subtab==='review')renderScenarios();
      }
    }
  }
  const sendScenarioFeedback=(runId,items)=>sendMapChange('/api/scenario-feedback',{run_id:runId,items});
  const sendScenarioChange=change=>sendMapChange('/api/scenarios',change);
  function scenarioClone(obj){return JSON.parse(JSON.stringify(obj));}
  function ensureDraftPositions(def){
    if(!def.layout||typeof def.layout!=='object')def.layout={};
    if(!def.layout.positions||typeof def.layout.positions!=='object')def.layout.positions={};
    const auto=scenarioAutoLayout(def.steps);
    for(const step of list(def.steps)){
      const p=def.layout.positions[step.id];
      if(!p||!Number.isFinite(Number(p.x))||!Number.isFinite(Number(p.y)))def.layout.positions[step.id]=auto[step.id]||{x:SCN_PAD,y:SCN_PAD};
    }
    return def.layout.positions;
  }
  function uniqueScenarioId(base){
    const existing=new Set(list(scenariosState.data?.scenarios).map(s=>s.id));
    let id=base.replace(/[^A-Za-z0-9_.:-]+/g,'-').replace(/^[^A-Za-z0-9]+/,'')||'new-scenario';
    id=id.slice(0,80);
    if(!existing.has(id))return id;
    for(let i=2;i<100;i++)if(!existing.has(id+'-'+i))return id+'-'+i;
    return id+'-'+Date.now().toString(36).slice(-4);
  }
  function uniqueStepId(steps,prefix='step'){
    const used=new Set(list(steps).map(s=>s.id));
    let base=prefix.toLowerCase().replace(/[^a-z0-9_-]+/g,'_').replace(/^[^a-z]+/,'')||'step';
    base=base.slice(0,36);
    if(!used.has(base))return base;
    for(let i=1;i<500;i++)if(!used.has(base+'_'+i))return base+'_'+i;
    return 'step_'+Date.now().toString(36).slice(-4);
  }
  function openScenarioEditor(options={}){
    const record=options.record||null,copyFrom=options.copyFrom||null;
    const baseScope=record?record.scope:(scenariosState.scope.startsWith('project:')?'project':'global');
    const baseProject=record?(record.project_id||''):(scenariosState.scope.startsWith('project:')?scenariosState.scope.slice(8):'');
    let def;
    if(record){
      def=scenarioClone(record.definition);
    }else if(copyFrom){
      def=scenarioClone(copyFrom.definition);
      def.id=uniqueScenarioId(copyFrom.id+'-custom');
      def.title=(def.title||copyFrom.id)+' (копия)';
    }else{
      def={
        format:'richi-scenario',format_version:1,
        id:uniqueScenarioId('custom-pipeline'),title:'Новый сценарий',description:'',when_to_use:'',run_title:'',tags:[],
        inputs:{},
        steps:[
          {id:'analyze_task',type:'instruction',title:'1. Разобраться в задаче и контексте',needs:[],params:{text:'Изучи текущую задачу, изменения в репозитории и связанные файлы. Сформулируй короткий план работы.'}},
          {id:'execute_task',type:'instruction',title:'2. Выполнить основную работу',needs:['analyze_task'],params:{text:'Выполни задачу по плану ({{steps.analyze_task.summary}}), проверь граничные случаи и запусти релевантные тесты.'}},
          {id:'human_confirm',type:'gate',title:'3. Подтверждение результата',needs:['execute_task'],params:{question:'Проверь итог работы: {{steps.execute_task.summary}}. Продолжаем?',options:['Подтвердить','Доработать'],allow_comment:true}},
        ],
      };
    }
    if(!def.inputs||typeof def.inputs!=='object')def.inputs={};
    if(!Array.isArray(def.tags))def.tags=[];
    ensureDraftPositions(def);
    scenariosState.editing={
      mode:record?'update':'add',
      fromCopy:Boolean(copyFrom),
      idTouched:Boolean(record||copyFrom),
      invalidFields:new Map(),
      original:record,
      scope:baseScope,
      project_id:baseProject,
      expected_updated_at:record?.updated_at||null,
      draft:def,
      baseline:scenarioClone(def),
      baselineScope:baseScope,
      baselineProject:baseProject,
      undoStack:[],
      dirty:false,
    };
    scenariosState.selectedStepId=def.steps[0]?.id||'';
    scenariosState.saveError='';
    scenariosState.commandText='';
    scenariosState.showJson=false;
    scenariosState.cameraFitNeeded=true;
    scenariosState.lintResult=null;
    scenariosState.subtab='definitions';
    renderScenarios();
    syncScenarioTabBadge();
  }
  function syncScenarioTabBadge(){
    const tab=$('scenarios-tab');if(!tab)return;
    const isDirty=Boolean(scenariosState.editing?.dirty);
    let dot=tab.querySelector('.tab-dirty-dot');
    if(isDirty&&!dot){
      dot=make('span','tab-dirty-dot');dot.setAttribute('aria-hidden','true');
      tab.append(dot);
    }else if(!isDirty&&dot){dot.remove();}
    tab.title=isDirty?'Сценарии — есть несохранённый черновик (нажмите, чтобы вернуться к списку)':'Сценарии';
  }
  function pushScenarioUndo(ed){
    if(!ed)return;
    const snap={draft:scenarioClone(ed.draft),selectedStepId:scenariosState.selectedStepId,scope:ed.scope,project_id:ed.project_id};
    const prev=ed.undoStack[ed.undoStack.length-1];
    if(prev&&JSON.stringify(prev.draft)===JSON.stringify(snap.draft)&&prev.scope===snap.scope&&prev.project_id===snap.project_id)return;
    ed.undoStack.push(snap);
    if(ed.undoStack.length>30)ed.undoStack.shift();
  }
  function markScenarioDirty(ed){
    if(!ed)return;
    ed.dirty=scenarioDraftChanged(ed.draft,ed.baseline)||ed.scope!==ed.baselineScope||ed.project_id!==ed.baselineProject;
    syncScenarioTabBadge();
    const root=$('scenarios-content');if(!root)return;
    const dirtyBar=root.querySelector('.scn-dirty-bar');
    if(dirtyBar)dirtyBar.hidden=!ed.dirty;
    const pill=root.querySelector('.scn-status-pill');
    if(pill){
      pill.className='scn-status-pill '+(ed.dirty?'dirty':'clean');
      pill.textContent=ed.dirty?'● Не сохранено':(ed.mode==='update'?'✓ Сохранён'+(ed.original?.version?' · v'+ed.original.version:''):'Новый черновик');
    }
    for(const btn of root.querySelectorAll('[data-scn-undo]'))btn.disabled=!ed.undoStack.length;
    for(const btn of root.querySelectorAll('[data-scn-reset]'))btn.disabled=!ed.dirty;
    for(const btn of root.querySelectorAll('[data-scn-run]'))btn.hidden=ed.dirty;
  }
  function undoScenarioEdit(){
    const ed=scenariosState.editing;if(!ed||!ed.undoStack.length)return;
    const prev=ed.undoStack.pop();
    ed.draft=prev.draft;ed.scope=prev.scope;ed.project_id=prev.project_id;
    scenariosState.selectedStepId=list(ed.draft.steps).some(s=>s.id===prev.selectedStepId)?prev.selectedStepId:(ed.draft.steps[0]?.id||'');
    ed.dirty=scenarioDraftChanged(ed.draft,ed.baseline)||ed.scope!==ed.baselineScope||ed.project_id!==ed.baselineProject;
    if(ed.invalidFields)ed.invalidFields.clear();
    scenariosState.lintResult=null;
    syncScenarioTabBadge();
    announce('Последнее изменение отменено');
    renderScenarios();
  }
  function resetScenarioDraft(){
    const ed=scenariosState.editing;if(!ed)return;
    pushScenarioUndo(ed);
    ed.draft=scenarioClone(ed.baseline);
    ed.scope=ed.baselineScope;ed.project_id=ed.baselineProject;
    ensureDraftPositions(ed.draft);
    if(!list(ed.draft.steps).some(s=>s.id===scenariosState.selectedStepId))scenariosState.selectedStepId=ed.draft.steps[0]?.id||'';
    ed.dirty=false;if(ed.invalidFields)ed.invalidFields.clear();scenariosState.lintResult=null;scenariosState.saveError='';scenariosState.commandText='';
    syncScenarioTabBadge();
    announce('Изменения сброшены до исходной версии');
    renderScenarios();
  }
  function discardAndLeaveScenarioEditor(onAfter){
    scenariosState.editing=null;scenariosState.selectedStepId='';scenariosState.saveError='';scenariosState.commandText='';scenariosState.lintResult=null;
    syncScenarioTabBadge();
    if(onAfter)onAfter();
    renderScenarios();
  }
  function confirmLeaveScenarioEditor(onAfter,leaveVerb='выйти к списку'){
    const ed=scenariosState.editing;
    if(!ed||!ed.dirty){discardAndLeaveScenarioEditor(onAfter);return;}
    const panel=ruleDialog('Несохранённые изменения',ed.draft?.title||ed.draft?.id||'Черновик сценария');
    const {dialog,body,foot}=panel;
    body.append(make('p','rule-confirm','Изменения в этом сценарии пока никуда не записаны. Вы можете сохранить их сейчас, сбросить до исходной версии или остаться в конструкторе.'));
    const stayBtn=toolButton('Остаться в редакторе',()=>dialog.close());
    const discardBtn=toolButton('Сбросить и '+leaveVerb,()=>{dialog.close();discardAndLeaveScenarioEditor(onAfter);},'danger');
    foot.append(stayBtn,discardBtn);
    if(rulesState.editing){
      const saveBtn=toolButton('Сохранить и '+leaveVerb,async()=>{
        dialog.close();
        const ok=await saveScenarioDraft();
        if(ok)discardAndLeaveScenarioEditor(onAfter);
      },'scn-btn-primary');
      foot.append(saveBtn);
    }
    dialog.showModal();stayBtn.focus();
  }
  function closeScenarioEditor(){
    confirmLeaveScenarioEditor(null,'выйти к списку');
  }
  function scenarioCommandHint(actionText){
    const wsOnly=state.mapStore.length===2&&state.mapStore[0]==='--workspace';
    return wsOnly
      ?(actionText+' (workspace «'+state.mapStore[1]+'»).')
      :(actionText+' Выполните в терминале (чат агента, привязанный к workspace, команду с --db/--config не примет).');
  }
  function openScenarioRunModal(item){
    const def=item.definition||{};
    const panel=ruleDialog('Как запустить: '+(def.title||item.id),scenarioScopeText(item)+' · '+item.id);
    const {dialog,body,foot}=panel;
    const schemaVersion=Number(scenariosState.data?.schema_version)||0;
    const capScenarios=scenariosState.data?.platform?.capabilities?.scenarios;
    const needsMigration=capScenarios===false||(capScenarios===undefined&&schemaVersion<4);
    if(needsMigration){
      const migBox=make('div','tools-warning');
      migBox.append(make('strong','','Сначала обновите схему хранилища (сейчас v'+schemaVersion+')'));
      migBox.append(make('p','','Для запуска сценариев требуется миграция таблиц сценариев. Без обновления команда запуска вернёт ошибку schema_upgrade_required. Перед обновлением Richi сам создаст резервную копию базы:'));
      const migCmd=['richi',...list(state.mapStore).map(scnShellQuote),'migrate'].join(' ');
      const showMig=ruleCommandBox(migBox,scenarioCommandHint('Команда обновит схему базы с резервной копией'));showMig(migCmd);
      body.append(migBox);
    }
    const chatBox=make('div','scn-chat-prompt');
    const chatPhrase='Запусти сценарий «'+(def.title||item.id)+'» ('+item.id+')';
    const chatPre=make('pre','',chatPhrase);
    const copyChat=toolButton('Скопировать фразу для чата',async()=>{
      try{await navigator.clipboard.writeText(chatPre.textContent);copyChat.textContent='Скопировано';setTimeout(()=>copyChat.textContent='Скопировать фразу для чата',1600);}
      catch{copyChat.textContent='Выделите текст';}
    },'small primary');
    chatBox.append(
      make('div','field-label','Самый простой способ — напишите агенту в чате:'),
      chatPre,
      make('div','tools-inline-note','Агент сам возьмёт текущую ветку, изменения и контекст чата и проведёт вас по шагам сценария.'),
      copyChat
    );
    body.append(chatBox);

    const cliFold=make('details','tools-fold');
    cliFold.append(make('summary','','Запуск вручную через команду терминала (CLI)'));
    const cliBody=make('div');cliBody.style.display='grid';cliBody.style.gap='10px';cliBody.style.marginTop='10px';
    const values={};
    const PROJECT_CHECKS=scenarioProjectBoundChecks(scenariosState.data?.platform);
    const needsProject=list(def.steps).some(s=>list(s?.checks).some(c=>c&&PROJECT_CHECKS.has(c.kind)));
    const projEntries=scenarioProjectNames();
    let selectedProject=(!item.builtin&&item.scope==='project')?(item.project_id||''):'';
    if(item.builtin||item.scope!=='project'){
      const preferred=scenariosState.subtab==='review'?scenariosState.reviewProject:(scenariosState.scope.startsWith('project:')?scenariosState.scope.slice(8):'');
      if(preferred&&projEntries.some(([pid])=>pid===preferred))selectedProject=preferred;
      const projSel=make('select');
      projSel.add(new Option(needsProject?'— выберите проект (обязательно для проверок) —':'— без проекта (весь workspace) —',''));
      for(const [pid,pname] of projEntries)projSel.add(new Option(pname+' ('+pid+')',pid));
      projSel.value=selectedProject;
      projSel.addEventListener('change',()=>{selectedProject=projSel.value;updateCmd();});
      cliBody.append(ruleField(
        'Проект (--project)'+(needsProject?' *':''),
        projSel,
        needsProject?'Сценарий использует проверки репозитория (findings_grounded / no_dirty_git / file_exists / evidence_git) — укажите проект для запуска.':'Необязательно: привяжет запуск и память к выбранному проекту.'
      ));
    }
    const inputs=def.inputs&&typeof def.inputs==='object'?Object.entries(def.inputs):[];
    if(!inputs.length){
      cliBody.append(make('p','tools-inline-note','Этот сценарий не требует ручных параметров — его можно запускать одной командой.'));
    }else{
      cliBody.append(make('p','tools-inline-note','Необязательно заполнять вручную: в чате агент обычно подставляет эти параметры сам. Если запускаете в терминале — укажите значения ниже:'));
      for(const [name,spec] of inputs){
        if(spec.type==='secret'){
          const note=make('div','rule-readonly','Секретное поле ('+name+'): не вводите секреты в браузер — при шаге передавайте через переменную окружения (--secret-env '+name+'=ENV_VAR).');
          cliBody.append(ruleField((spec.title||name)+(spec.required?' *':''),note,spec.description||''));
          continue;
        }
        values[name]=spec.default!==undefined?(Array.isArray(spec.default)?[...spec.default]:spec.default):'';
        let ctrl;
        if(spec.type==='boolean'){
          ctrl=make('select');ctrl.add(new Option('— не задано —',''));ctrl.add(new Option('Да (true)','true'));ctrl.add(new Option('Нет (false)','false'));
          ctrl.value=values[name]===''?'':String(values[name]);
          ctrl.addEventListener('change',()=>{values[name]=ctrl.value;updateCmd();});
        }else if(spec.type==='select'){
          ctrl=make('select');ctrl.add(new Option('— выберите —',''));
          for(const opt of list(spec.options))ctrl.add(new Option(opt,opt));
          ctrl.value=String(values[name]||'');
          ctrl.addEventListener('change',()=>{values[name]=ctrl.value;updateCmd();});
        }else if(spec.type==='multiselect'&&list(spec.options).length){
          ctrl=make('div','scn-needs-grid');
          const chosen=new Set(Array.isArray(values[name])?values[name]:[]);
          for(const opt of list(spec.options)){
            const lbl=make('label','scn-needs-item'),cb=make('input');cb.type='checkbox';cb.checked=chosen.has(opt);
            cb.addEventListener('change',()=>{if(cb.checked)chosen.add(opt);else chosen.delete(opt);values[name]=[...chosen];updateCmd();});
            lbl.append(cb,document.createTextNode(opt));ctrl.append(lbl);
          }
        }else{
          ctrl=make('input');ctrl.type='text';
          ctrl.value=Array.isArray(values[name])?values[name].join(','):String(values[name]||'');
          ctrl.placeholder=spec.type==='multiselect'?'значение1,значение2':'Введите значение…';
          ctrl.addEventListener('input',()=>{values[name]=ctrl.value;updateCmd();});
        }
        cliBody.append(ruleField((spec.title||name)+(spec.required?' *':''),ctrl,spec.description||''));
      }
    }
    const missingReqNote=make('div','rule-field-hint');missingReqNote.style.color='var(--danger)';missingReqNote.hidden=true;
    cliBody.append(missingReqNote);
    const showCmd=ruleCommandBox(cliBody,scenarioCommandHint('Команда запускает сценарий'));
    const updateCmd=()=>{
      const missing=inputs.filter(([n,s])=>s.required&&s.type!=='secret'&&(values[n]===''||values[n]===undefined||(Array.isArray(values[n])&&!values[n].length))).map(([n,s])=>s.title||n);
      if(needsProject&&!selectedProject)missing.unshift('Проект (--project)');
      missingReqNote.hidden=!missing.length;
      if(missing.length)missingReqNote.textContent='Для запуска в терминале заполните обязательные параметры: '+missing.join(', ');
      const scopeArg=item.builtin?null:item.scope;
      const projArg=selectedProject||null;
      let cmd=scenarioRunCommand(item.id,scopeArg,projArg,values,state.mapStore);
      for(const [n,s] of inputs)if(s.type==='secret'&&s.required)cmd+=' --secret-env '+scnShellQuote(n+'='+n.toUpperCase());
      showCmd(cmd);
    };
    cliFold.append(cliBody);body.append(cliFold);
    foot.append(toolButton('Закрыть',()=>dialog.close(),'primary'));
    updateCmd();dialog.showModal();
  }
  function openScenarioRemoval(record){
    const panel=ruleDialog('Удалить сценарий?',record.id+' · '+scenarioScopeText(record));
    const {dialog,form,body,foot}=panel;
    body.append(make('p','rule-confirm','Удалённый сценарий исчезнет из списка определений. Уже созданные запуски сохранят свою замороженную копию определения.'));
    const problem=ruleProblem(body),showCmd=ruleCommandBox(body,scenarioCommandHint('Команда удаляет сценарий'));
    const cancel=toolButton('Отмена',()=>dialog.close());
    const submit=make('button',rulesState.editing?'danger solid':'primary',rulesState.editing?'Удалить сценарий':'Показать команду');
    submit.type='submit';foot.append(cancel,submit);
    form.addEventListener('submit',async event=>{
      event.preventDefault();problem('');
      const request={action:'remove',id:record.id,scope:record.scope,expected_updated_at:record.updated_at};
      if(record.scope==='project'&&record.project_id)request.project=record.project_id;
      if(!rulesState.editing){showCmd(scenarioApplyCommand(request,state.mapStore));return;}
      await saveFromForm(panel,submit,{
        busy:'Удаляем…',
        send:()=>sendScenarioChange(request),
        explain:err=>editKeyProblem(err)||err.message,
        problem,
        saved:()=>{scenariosState.outcome='Сценарий «'+record.id+'» удалён.';announce('Сценарий удалён');loadScenarios();},
        failed:async err=>{if(err.code==='conflict'){await loadScenarios();return 'Сценарий изменили, пока вы подтверждали удаление. Закройте окно и повторите.';}return '';},
        lost:msg=>{scenariosState.error=msg;renderScenarios();},
        commands:()=>{submit.className='primary';showCmd(scenarioApplyCommand(request,state.mapStore));renderScenarios();},
        label:()=>{submit.textContent=rulesState.editing?'Удалить сценарий':'Показать команду';},
      });
    });
    dialog.showModal();cancel.focus();
  }
  async function saveScenarioDraft(){
    const ed=scenariosState.editing;if(!ed||scenariosState.busy)return false;
    scenariosState.saveError='';scenariosState.commandText='';
    if(ed.invalidFields&&ed.invalidFields.size){
      const names=[...ed.invalidFields.values()].map(v=>v&&typeof v==='object'?(v.stepId+'.'+v.label):String(v));
      scenariosState.saveError='Исправьте поля с ошибками ('+names.join(', ')+') перед сохранением.';
      renderScenarios();return false;
    }
    const def=scenarioNormalizeDraft(ed.draft);
    const invalid=scenarioValidateDraft(def);
    if(invalid){scenariosState.saveError=invalid;renderScenarios();return false;}
    if(ed.scope==='project'&&!ed.project_id){scenariosState.saveError='Выберите проект для проектного сценария.';renderScenarios();return false;}
    const request={action:ed.mode,scope:ed.scope,definition:def};
    if(ed.scope==='project')request.project=ed.project_id;
    if(ed.mode==='update'){request.id=ed.original.id;request.expected_updated_at=ed.expected_updated_at;}
    if(!rulesState.editing){
      scenariosState.commandText=scenarioApplyCommand(request,state.mapStore);
      renderScenarios();return false;
    }
    scenariosState.busy=true;renderScenarios();
    let savedOk=false;
    try{
      const answer=await sendScenarioChange(request);
      const saved=answer?.scenario;
      scenariosState.outcome=ed.mode==='add'?'Сценарий «'+def.id+'» создан.':'Сценарий «'+def.id+'» сохранён (v'+(saved?.version||'?')+').';
      if(saved){
        ed.mode='update';ed.original=saved;ed.expected_updated_at=saved.updated_at;
        ed.draft=scenarioClone(saved.definition);ensureDraftPositions(ed.draft);
      }
      ed.baseline=scenarioClone(ed.draft);ed.baselineScope=ed.scope;ed.baselineProject=ed.project_id;
      ed.undoStack=[];ed.dirty=false;savedOk=true;
      syncScenarioTabBadge();
      announce(scenariosState.outcome);
      await loadScenarios();
    }catch(error){
      const keyMsg=editKeyProblem(error);
      if(keyMsg){
        scenariosState.saveError=keyMsg;
        scenariosState.commandText=scenarioApplyCommand(request,state.mapStore);
      }else if(error.code==='conflict'&&ed.mode==='update'){
        await loadScenarios();
        const fresh=list(scenariosState.data?.scenarios).find(s=>s.id===ed.original.id&&s.scope===ed.scope&&s.project_id===(ed.project_id||null));
        if(fresh){ed.original=fresh;ed.expected_updated_at=fresh.updated_at;scenariosState.saveError='Сценарий был изменён в другом месте (теперь v'+fresh.version+'). Проверьте изменения и нажмите «Сохранить» ещё раз, чтобы записать вашу версию поверх.';}
        else scenariosState.saveError=error.message;
      }else{
        scenariosState.saveError=error.message||'Не удалось сохранить сценарий.';
      }
    }finally{
      scenariosState.busy=false;
      syncScenarioTabBadge();
      renderScenarios();
    }
    return savedOk;
  }
  function prettyStepLabel(step){
    const raw=String(step.title||step.id||'');
    return raw.replace(/\{\{\s*matrix\.([a-zA-Z0-9_]+)\s*\}\}/g,'(по каждому пункту)').replace(/\{\{[^}]+\}\}/g,'…');
  }
  function renderScenarioMiniSvg(def){
    const steps=list(def.steps),auto=scenarioAutoLayout(steps);
    const saved=def.layout&&typeof def.layout==='object'&&def.layout.positions&&typeof def.layout.positions==='object'?def.layout.positions:{};
    const pos={};
    let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
    const nw=108,nh=38;
    for(const s of steps){
      const p=saved[s.id]&&Number.isFinite(Number(saved[s.id].x))&&Number.isFinite(Number(saved[s.id].y))
        ?{x:Number(saved[s.id].x)*0.55,y:Number(saved[s.id].y)*0.55}
        :{x:(auto[s.id]?.x||36)*0.55,y:(auto[s.id]?.y||36)*0.55};
      pos[s.id]=p;
      if(p.x<minX)minX=p.x;if(p.y<minY)minY=p.y;
      if(p.x+nw>maxX)maxX=p.x+nw;if(p.y+nh>maxY)maxY=p.y+nh;
    }
    if(!Number.isFinite(minX)){minX=0;minY=0;maxX=240;maxY=100;}
    const padX=22,padY=30,vbW=Math.max(260,(maxX-minX)+padX*2),vbH=Math.max(104,(maxY-minY)+padY*2);
    const svg=makeSvg('svg',{viewBox:`${Math.round(minX-padX)} ${Math.round(minY-padY)} ${Math.round(vbW)} ${Math.round(vbH)}`,preserveAspectRatio:'xMidYMid meet'});
    for(const s of steps){
      for(const dep of list(s.needs)){
        const a=pos[dep],b=pos[s.id];if(!a||!b)continue;
        const d=scenarioEdgePath(a.x+nw,a.y+nh/2,b.x,b.y+nh/2);
        svg.append(makeSvg('path',{d,fill:'none',stroke:'var(--edge)','stroke-width':'2.2','stroke-linecap':'round'}));
      }
    }
    const roleColor={agent:'var(--accent)',richi:'var(--record)',human:'var(--entity)'};
    steps.forEach((s,idx)=>{
      const p=pos[s.id],role=scenarioStepRole(s.type),col=roleColor[role]||'var(--accent)';
      const g=makeSvg('g');
      g.append(makeSvg('rect',{x:Math.round(p.x),y:Math.round(p.y),width:nw,height:nh,rx:'8',fill:'var(--panel)',stroke:col,'stroke-width':'1.8'}));
      g.append(makeSvg('rect',{x:Math.round(p.x),y:Math.round(p.y),width:'6',height:nh,rx:'3',fill:col}));
      const txt=makeSvg('text',{x:Math.round(p.x+14),y:Math.round(p.y+23),fill:'var(--ink)','font-size':'11','font-weight':'600'});
      txt.textContent='#'+(idx+1)+' '+cut(prettyStepLabel(s).replace(/^\d+\.\s*/,''),12);
      g.append(txt);svg.append(g);
    });
    return svg;
  }
  function renderScenarioCard(item){
    const def=item.definition||{},steps=list(def.steps),card=make('article','scn-card');
    const openEdit=()=>item.builtin?openScenarioEditor({copyFrom:item}):openScenarioEditor({record:item});
    const preview=make('div','scn-card-preview');
    preview.title='Открыть пайплайн в визуальном конструкторе';
    preview.append(renderScenarioMiniSvg(def));
    const pBadges=make('div','scn-card-preview-badges');
    const scopeCls=item.builtin?'tpl':(item.scope==='project'?'project':'global');
    pBadges.append(
      make('span','scn-pill-badge '+scopeCls,item.builtin?'Готовый шаблон':scenarioScopeText(item)),
      make('span','scn-pill-badge count',plural(steps.length,['этап','этапа','этапов'])+(!item.builtin&&item.version?' · v'+item.version:''))
    );
    preview.append(pBadges);
    preview.addEventListener('click',openEdit);
    card.append(preview);

    const body=make('div','scn-card-body');
    body.append(make('div','scn-card-title',def.title||item.id));
    if(def.description)body.append(make('div','scn-card-desc',def.description));
    const stepBox=make('div','scn-card-steps');
    steps.slice(0,7).forEach((step,idx)=>{
      const role=scenarioStepRole(step.type),row=make('div','scn-card-step');
      row.append(make('span','scn-card-step-num role-'+role,String(idx+1)));
      row.append(make('span','scn-card-step-name',prettyStepLabel(step).replace(/^\d+\.\s*/,'')));
      const tag=step.hints?.subagent?'субагент':(step.type==='instruction'?'':(SCN_STEP_META[step.type]?.label||''));
      if(tag)row.append(make('span','scn-card-step-tag',tag));
      stepBox.append(row);
    });
    if(steps.length>7)stepBox.append(make('div','scn-card-id','+ ещё '+plural(steps.length-7,['этап','этапа','этапов'])));
    body.append(stepBox);
    card.append(body);

    const footer=make('div','scn-card-footer');
    footer.append(toolButton(item.builtin?'Открыть и настроить':'Открыть конструктор',openEdit,'small scn-btn-primary'));
    footer.append(toolButton('Запустить в чате',()=>openScenarioRunModal(item),'small'));
    const extra=make('div','scn-card-footer-extra');
    if(!item.builtin){
      extra.append(toolButton('Копия',()=>openScenarioEditor({copyFrom:item}),'small'));
      extra.append(toolButton('Удалить',()=>openScenarioRemoval(item),'small danger'));
    }else if(item.shadowed){
      extra.append(make('span','scn-card-id','есть копия'));
    }
    footer.append(extra);
    card.append(footer);
    return card;
  }
  function scenarioMatchesFilter(item){
    if(scenariosState.scope==='global'&&(item.builtin||item.scope!=='global'))return false;
    if(scenariosState.scope==='builtin'&&!item.builtin)return false;
    if(scenariosState.scope.startsWith('project:')&&(item.builtin||item.scope!=='project'||item.project_id!==scenariosState.scope.slice(8)))return false;
    const q=normalized(scenariosState.query);if(!q)return true;
    const def=item.definition||{};
    const hay=[item.id,def.title,def.description,def.when_to_use,...list(def.tags),...list(def.steps).flatMap(s=>[s.id,s.title,s.type])].map(normalized).join(' ');
    return q.split(/\s+/).filter(Boolean).every(w=>hay.includes(w));
  }
  function openAgentAuthorDialog(){
    const panel=ruleDialog('Собрать сценарий с помощью агента','Опишите процесс простыми словами — агент создаст валидный сценарий прямо в Richi');
    const {dialog,body,foot}=panel;
    const goal=make('textarea');goal.rows=3;goal.placeholder='Например: Сделай сценарий регрессионного тестирования API: прочитать прошлые баги из памяти, прогнать pytest, сравнить время ответа с baseline и спросить меня перед релизом.';
    const scopeSel=make('select');
    scopeSel.add(new Option('Глобальный (для всех проектов workspace)','global'));
    for(const [pid,pname] of scenarioProjectNames())scopeSel.add(new Option('Проект: '+pname,'project:'+pid));
    if(scenariosState.scope.startsWith('project:'))scopeSel.value=scenariosState.scope;
    body.append(ruleField('Что должен делать сценарий?',goal),ruleField('Куда сохранить',scopeSel));
    const outBox=make('div','scn-chat-prompt');
    const outPre=make('pre','');
    const copyBtn=toolButton('Скопировать промпт для чата',async()=>{
      try{await navigator.clipboard.writeText(outPre.textContent);copyBtn.textContent='Скопировано ✓';setTimeout(()=>copyBtn.textContent='Скопировать промпт для чата',1500);}
      catch{copyBtn.textContent='Выделите текст';}
    },'small scn-btn-primary');
    outBox.append(make('div','field-label','Отправьте этот запрос в чат Claude / Codex / Cursor / Antigravity:'),outPre,copyBtn);
    body.append(outBox);
    const syncPrompt=()=>{
      const desc=goal.value.trim()||'<опишите задачу и этапы>';
      const sc=scopeSel.value.startsWith('project:')?('проекта '+scopeSel.value.slice(8)):'всего workspace (global)';
      outPre.textContent='Создай сценарий в Richi для '+sc+': '+desc+'\n\nПрочитай справочник SCENARIOS.md (или SCENARIOS.ru.md), собери валидный JSON определения (с понятными русскими названиями, типами шагов, needs и проверками), проверь через `richi scenario plan` и сохрани через `richi scenario apply --json -`. После сохранения покажи ID и краткий список этапов.';
    };
    goal.addEventListener('input',syncPrompt);scopeSel.addEventListener('change',syncPrompt);
    syncPrompt();
    foot.append(toolButton('Закрыть',()=>dialog.close()));
    dialog.showModal();goal.focus();
  }
  function openScenarioPreviewDialog(ed){
    const def=ed.draft;
    const panel=ruleDialog('Предпросмотр сценария для агента',def.title||def.id||'Черновик');
    const {dialog,body,foot}=panel;
    const agentsList=knownAgentIds();
    const agentSel=make('select');
    for(const aid of agentsList)agentSel.add(new Option(agentName(aid)+' ('+aid+')',aid));
    if(!agentsList.length)agentSel.add(new Option('Агент по умолчанию',''));
    body.append(ruleField('Смотреть глазами агента',agentSel,'Показывает выбранный скилл (с учётом per_agent) и подставленные значения входов.'));
    const inputVals={};
    for(const [k,spec] of Object.entries(def.inputs||{})){
      if(spec?.type==='secret')inputVals[k]='<secret:'+k+'>';
      else if(spec?.default!==undefined)inputVals[k]=spec.default;
      else if(spec?.type==='boolean')inputVals[k]=true;
      else if(spec?.type==='number')inputVals[k]=1;
      else if(spec?.type==='select'&&list(spec.options).length)inputVals[k]=spec.options[0];
      else if(spec?.type==='multiselect'&&list(spec.options).length)inputVals[k]=list(spec.options).slice(0,2);
      else inputVals[k]='<'+k+'>';
    }
    const previewList=make('div');previewList.style.display='grid';previewList.style.gap='10px';
    const renderPreview=()=>{
      previewList.replaceChildren();
      const aid=agentSel.value;
      const renderSampleText=raw=>String(raw||'').replace(/\{\{\s*([^}]+?)\s*\}\}/g,(_,expr)=>{
        const key=expr.split('|')[0].trim();
        if(key.startsWith('inputs.')){
          const name=key.slice(7).split('[')[0];
          const v=inputVals[name];
          if(v!==undefined)return Array.isArray(v)?v.join(', '):String(v);
        }
        if(key==='run.id')return 'run-'+(def.id||'draft')+'-0001';
        if(key==='project.path')return '/path/to/repo';
        if(key==='agent.id')return aid||'agent';
        if(key==='now.date')return new Date().toISOString().slice(0,10);
        return '['+key+']';
      });
      const allSkills=list(rulesState.skills?.skills);
      list(def.steps).forEach((step,idx)=>{
        const card=make('div','scn-step-row');
        const head=make('div','scn-run-top');
        head.append(make('strong','','#'+(idx+1)+' '+prettyStepLabel(step)+' ('+step.id+')'),make('span','scn-node-badge',SCN_STEP_META[step.type]?.label||step.type));
        card.append(head);
        const p=step.params||{};
        if(step.type==='skill'){
          const per=p.skill?.per_agent||{};
          const resolved=(aid&&per[aid])||p.skill?.default||'';
          const info=allSkills.find(s=>s.name===resolved);
          const missing=resolved&&allSkills.length&&(!info||(aid&&!list(info.agents).includes(aid)));
          const skLine=make('div','tools-inline-note','Скилл для '+agentName(aid||'агента')+': '+(resolved||'не задан')+(missing?' ⚠ не установлен у этого агента':''));
          if(missing)skLine.style.color='var(--danger)';
          card.append(skLine);
          if(p.instruction)card.append(make('pre','',renderSampleText(p.instruction)));
        }else if(step.type==='instruction'){
          card.append(make('pre','',renderSampleText(p.text||'')));
        }else if(step.type==='command'){
          card.append(make('code','',renderSampleText(p.command||'')));
        }else if(step.type==='gate'){
          card.append(make('div','',renderSampleText(p.question||'')));
        }else if(step.type==='checklist'){
          card.append(make('div','',list(p.items).map(it=>'☐ '+renderSampleText(typeof it==='string'?it:it.text)).join('\n')));
        }else if(step.type==='memory.recall'){
          card.append(make('div','tools-inline-note','Запрос: '+renderSampleText(p.query||'')));
        }else if(step.type==='memory.brief'){
          card.append(make('div','tools-inline-note','Вопрос: '+renderSampleText(p.question||'')));
        }else if(step.type==='memory.record'){
          card.append(make('div','tools-inline-note','Запишет '+String(p.kind||'decision')+': '+renderSampleText(p.title||'')));
          if(p.summary)card.append(make('pre','',renderSampleText(p.summary)));
        }else if(step.type==='compare'){
          card.append(make('div','tools-inline-note','Сравнит шаг «'+(p.step||'?')+'» с baseline'+(list(p.metrics).length?' ('+p.metrics.join(', ')+')':'')));
        }else if(step.type==='condition'){
          card.append(make('code','',p.expr||''));
        }
        previewList.append(card);
      });
    };
    agentSel.addEventListener('change',renderPreview);
    renderPreview();
    body.append(previewList);
    foot.append(toolButton('Закрыть',()=>dialog.close()));
    dialog.showModal();
  }
  function scenarioVersionDiff(beforeRec,afterRec,stepDiff=null){
    const lines=[];
    const beforeDef=beforeRec?.definition&&typeof beforeRec.definition==='object'?beforeRec.definition:null;
    const afterDef=afterRec?.definition&&typeof afterRec.definition==='object'?afterRec.definition:null;
    const stepCount=rec=>{
      if(!rec)return 0;
      if(typeof rec.steps==='number')return rec.steps;
      if(Array.isArray(rec.steps))return rec.steps.length;
      if(Array.isArray(rec.definition?.steps))return rec.definition.steps.length;
      return 0;
    };
    if(!beforeRec){
      lines.push('+ Создан сценарий ('+plural(stepCount(afterRec),['шаг','шага','шагов'])+')');
      return lines;
    }
    const bTitle=beforeDef?.title??beforeRec.title??'';
    const aTitle=afterDef?.title??afterRec?.title??'';
    if(bTitle!==aTitle)lines.push('~ Название: «'+bTitle+'» → «'+aTitle+'»');
    if(stepDiff&&typeof stepDiff==='object'){
      const fmtStep=x=>(x&&typeof x==='object')?((x.title?'«'+x.title+'» ':'')+'('+(x.id||'?')+')'):('('+String(x)+')');
      for(const item of list(stepDiff.added))lines.push('+ Добавлен шаг '+fmtStep(item));
      if(stepDiff.added_more)lines.push('+ … и ещё '+stepDiff.added_more+' добавленных');
      for(const item of list(stepDiff.changed))lines.push('~ Изменён шаг '+fmtStep(item));
      if(stepDiff.changed_more)lines.push('~ … и ещё '+stepDiff.changed_more+' изменённых');
      for(const item of list(stepDiff.removed))lines.push('- Удалён шаг '+fmtStep(item));
      if(stepDiff.removed_more)lines.push('- … и ещё '+stepDiff.removed_more+' удалённых');
    }else if(beforeDef&&afterDef){
      const bSteps=new Map(list(beforeDef.steps).map(s=>[s.id,s]));
      const aSteps=new Map(list(afterDef.steps).map(s=>[s.id,s]));
      for(const [id,st] of aSteps){
        if(!bSteps.has(id))lines.push('+ Добавлен шаг «'+(st.title||id)+'» ('+id+')');
        else if(JSON.stringify(bSteps.get(id))!==JSON.stringify(st))lines.push('~ Изменён шаг «'+(st.title||id)+'» ('+id+')');
      }
      for(const [id,st] of bSteps){
        if(!aSteps.has(id))lines.push('- Удалён шаг «'+(st.title||id)+'» ('+id+')');
      }
    }else if(stepCount(beforeRec)!==stepCount(afterRec)){
      lines.push('~ Число шагов: '+stepCount(beforeRec)+' → '+stepCount(afterRec));
    }
    const bInputs=Array.isArray(beforeRec.inputs)?beforeRec.inputs:Object.keys(beforeDef?.inputs||{});
    const aInputs=Array.isArray(afterRec?.inputs)?afterRec.inputs:Object.keys(afterDef?.inputs||{});
    if(JSON.stringify(bInputs)!==JSON.stringify(aInputs))lines.push('~ Входные параметры: ['+bInputs.join(', ')+'] → ['+aInputs.join(', ')+']');
    const bTags=list(beforeRec.tags||beforeDef?.tags);
    const aTags=list(afterRec?.tags||afterDef?.tags);
    if(JSON.stringify(bTags)!==JSON.stringify(aTags))lines.push('~ Теги: ['+bTags.join(', ')+'] → ['+aTags.join(', ')+']');
    if(beforeRec.sha256&&afterRec?.sha256&&beforeRec.sha256!==afterRec.sha256&&!lines.length){
      lines.push('~ Изменено определение (sha256 '+String(beforeRec.sha256).slice(0,8)+' → '+String(afterRec.sha256).slice(0,8)+')');
    }
    return lines.length?lines:['~ Метаданные / раскладка обновлены'];
  }
  function openScenarioHistoryDialog(ed){
    const sid=ed.original?.id||ed.draft?.id;
    const panel=ruleDialog('История версий и отличия',sid||'Сценарий');
    const {dialog,body,foot}=panel;
    const wantScope=ed.scope||'',wantProject=ed.project_id||'';
    const events=list(scenariosState.data?.log).filter(ev=>{
      if(ev.scenario_id!==sid||!['scenario_created','scenario_updated'].includes(ev.action))return false;
      const det=ev.detail||{};
      if(det.scope&&det.scope!==wantScope)return false;
      if((det.project_id||'')!==wantProject)return false;
      return true;
    });
    if(!events.length){
      body.append(make('p','tools-inline-note','В текущем журнале пока нет сохранённых версий этого сценария.'));
    }else{
      const box=make('div','scn-log-list');
      for(const ev of events){
        const det=ev.detail||{};
        const afterRec=det.after,beforeRec=det.before;
        const row=make('div','scn-log-row');
        const head=make('div','scn-run-top');
        const ver=afterRec?.version?('v'+afterRec.version):'';
        head.append(make('strong','',(SCN_ACTION_LABEL[ev.action]||ev.action)+(ver?' · '+ver:'')),make('span','tools-inline-note',ev.at?dateText(ev.at):''));
        row.append(head);
        const diffLines=scenarioVersionDiff(beforeRec,afterRec,det.step_diff||null);
        const diffBox=make('div','scn-diff-box');
        for(const dl of diffLines){
          const cls=dl.startsWith('+')?'add':dl.startsWith('-')?'del':'mod';
          diffBox.append(make('div','scn-diff-line '+cls,dl));
        }
        row.append(diffBox);
        box.append(row);
      }
      body.append(box);
    }
    foot.append(toolButton('Закрыть',()=>dialog.close()));
    dialog.showModal();
  }
  function renderScenarioCatalog(root){
    const stored=list(scenariosState.data?.scenarios).filter(scenarioMatchesFilter);
    const builtins=list(scenariosState.data?.builtin_templates).filter(scenarioMatchesFilter);
    for(const inv of list(scenariosState.data?.invalid_scenarios))toolWarning(root,'Повреждённое определение сценария: '+(inv.id||inv.key)+' — '+(inv.error||'некорректный JSON'));
    for(const uns of list(scenariosState.data?.unsupported_scenarios))toolWarning(root,'Сценарий «'+(uns.id||uns.key)+'» требует более новую версию формата Richi.');
    const bar=make('div','scn-catalog-bar');
    bar.append(make('p','','Собирайте повторяемые пайплайны для агентов на визуальном холсте, клонируйте готовые шаблоны или попросите агента в чате создать сценарий через richi scenario apply.'));
    const barActs=make('div','rule-actions');
    barActs.append(toolButton('✨ Собрать с агентом',openAgentAuthorDialog,'small'),keyed(toolButton('+ Новый сценарий',()=>openScenarioEditor(),'scn-btn-primary'),'scn-add'));
    bar.append(barActs);
    root.append(bar);
    if(scenariosState.scope!=='builtin'){
      toolSection(root,'Ваши сценарии',stored.length);
      if(!stored.length){
        const empty=make('div','tools-empty');
        empty.append(ruleMark('rule'),make('h3','',list(scenariosState.data?.scenarios).length?'Подходящих сценариев нет':'Своих сценариев пока нет'),make('p','',list(scenariosState.data?.scenarios).length?'Измените поиск или фильтр области.':'Создайте новый сценарий с нуля или откройте готовый шаблон «Глубокое ревью в 7 этапов» ниже.'));
        empty.append(toolButton('+ Создать сценарий',()=>openScenarioEditor(),'scn-btn-primary'));
        root.append(empty);
      }else{
        const grid=make('div','scn-cards-grid');stored.forEach(item=>grid.append(renderScenarioCard(item)));root.append(grid);
      }
    }
    if(builtins.length||scenariosState.scope===''||scenariosState.scope==='builtin'){
      toolSection(root,'Готовые шаблоны',builtins.length);
      if(builtins.length){
        const grid=make('div','scn-cards-grid');builtins.forEach(item=>grid.append(renderScenarioCard(item)));root.append(grid);
      }
    }
  }
  function pruneStepInvalidFields(ed,stepId){
    if(!ed||!(ed.invalidFields instanceof Map)||!stepId)return;
    const prefix=stepId+':';
    for(const k of [...ed.invalidFields.keys()])if(k.startsWith(prefix))ed.invalidFields.delete(k);
  }
  function renameStepInvalidFields(ed,oldId,newId){
    if(!ed||!(ed.invalidFields instanceof Map)||!oldId||!newId||oldId===newId)return;
    const prefix=oldId+':';
    for(const [k,v] of [...ed.invalidFields.entries()]){
      if(k.startsWith(prefix)){
        ed.invalidFields.delete(k);
        const rest=k.slice(prefix.length);
        ed.invalidFields.set(newId+':'+rest,Object.assign({},v,{stepId:newId}));
      }
    }
  }
  function renameStepId(draft,oldId,newId){
    if(!newId||oldId===newId||!SCN_STEP_ID_RE.test(newId))return false;
    if(list(draft.steps).some(s=>s.id===newId))return false;
    for(const step of list(draft.steps)){
      if(step.id===oldId)step.id=newId;
      if(Array.isArray(step.needs))step.needs=step.needs.map(n=>n===oldId?newId:n);
      if(step.type==='compare'&&step.params&&step.params.step===oldId)step.params.step=newId;
    }
    if(draft.layout?.positions&&draft.layout.positions[oldId]){
      draft.layout.positions[newId]=draft.layout.positions[oldId];
      delete draft.layout.positions[oldId];
    }
    if(scenariosState.editing?.draft===draft)renameStepInvalidFields(scenariosState.editing,oldId,newId);
    if(scenariosState.selectedStepId===oldId)scenariosState.selectedStepId=newId;
    return true;
  }
  function highlightScenarioNode(stepId,active,targetStepId=''){
    const root=$('scenarios-content');if(!root||!stepId)return;
    for(const n of root.querySelectorAll('.scn-node[data-step-id]')){
      if(n.dataset.stepId===stepId)n.classList.toggle('hi',Boolean(active));
    }
    for(const g of root.querySelectorAll('.scn-edge-group[data-from]')){
      const match=g.dataset.from===stepId&&(!targetStepId||g.dataset.to===targetStepId);
      if(match)g.classList.toggle('hi',Boolean(active));
    }
  }
  function renderScenarioCanvas(ed){
    const def=ed.draft,steps=list(def.steps),positions=ensureDraftPositions(def);
    const viewport=make('div','scn-canvas-viewport'),layer=make('div','scn-canvas-layer');
    const cam=scenariosState.camera||(scenariosState.camera={x:20,y:20,k:1});
    const svgEl=makeSvg('svg',{class:'scn-edges-svg',width:'2400',height:'1600'});
    svgEl.style.color='var(--edge)';
    const defsEl=makeSvg('defs'),marker=makeSvg('marker',{id:'scn-arrow',viewBox:'0 0 10 10',refX:'9',refY:'5',markerWidth:'7',markerHeight:'7',orient:'auto'});
    marker.append(makeSvg('path',{d:'M 0 1.5 L 9 5 L 0 8.5 z',fill:'currentColor'}));
    defsEl.append(marker);svgEl.append(defsEl);
    const edgesGroup=makeSvg('g');svgEl.append(edgesGroup);
    const previewPath=makeSvg('path',{class:'scn-edge-preview',d:''});svgEl.append(previewPath);
    layer.append(svgEl);

    const nodeEls=new Map(),zoomReadout=make('span','','100%');
    const applyCamera=()=>{
      cam.k=Math.min(1.8,Math.max(0.35,Number(cam.k)||1));
      layer.style.transform='translate('+Math.round(cam.x)+'px,'+Math.round(cam.y)+'px) scale('+cam.k.toFixed(3)+')';
      viewport.style.backgroundPosition=Math.round(cam.x)+'px '+Math.round(cam.y)+'px';
      zoomReadout.textContent=Math.round(cam.k*100)+'%';
    };
    const fitCamera=()=>{
      const vw=viewport.clientWidth||760,vh=viewport.clientHeight||560;
      if(!steps.length){cam.x=20;cam.y=20;cam.k=1;applyCamera();return;}
      let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
      for(const step of steps){
        const p=positions[step.id]||{x:SCN_PAD,y:SCN_PAD},el=nodeEls.get(step.id);
        const w=el?.offsetWidth||SCN_NODE_W,h=el?.offsetHeight||SCN_NODE_H;
        minX=Math.min(minX,p.x);minY=Math.min(minY,p.y);maxX=Math.max(maxX,p.x+w);maxY=Math.max(maxY,p.y+h);
      }
      const bw=Math.max(180,maxX-minX),bh=Math.max(120,maxY-minY);
      cam.k=Math.min(1,Math.max(0.42,Math.min((vw-72)/bw,(vh-72)/bh)));
      cam.x=Math.round((vw-bw*cam.k)/2-minX*cam.k);
      cam.y=Math.round((vh-bh*cam.k)/2-minY*cam.k);
      applyCamera();
    };
    const toWorld=(clientX,clientY)=>{
      const rect=viewport.getBoundingClientRect();
      return {x:(clientX-rect.left-cam.x)/cam.k,y:(clientY-rect.top-cam.y)/cam.k};
    };
    const portCoords=(stepId,side)=>{
      const p=positions[stepId]||{x:SCN_PAD,y:SCN_PAD},el=nodeEls.get(stepId);
      const w=el?el.offsetWidth||SCN_NODE_W:SCN_NODE_W,h=el?el.offsetHeight||SCN_NODE_H:SCN_NODE_H;
      return {x:side==='out'?p.x+w:p.x,y:p.y+h/2};
    };
    function redrawEdges(){
      edgesGroup.replaceChildren();
      let maxX=2000,maxY=1200;
      for(const step of steps){
        const p=positions[step.id]||{x:SCN_PAD,y:SCN_PAD};
        if(p.x+SCN_NODE_W+300>maxX)maxX=p.x+SCN_NODE_W+300;
        if(p.y+SCN_NODE_H+300>maxY)maxY=p.y+SCN_NODE_H+300;
        for(const dep of list(step.needs)){
          if(!positions[dep])continue;
          const a=portCoords(dep,'out'),b=portCoords(step.id,'in'),d=scenarioEdgePath(a.x,a.y,b.x,b.y);
          const g=makeSvg('g',{class:'scn-edge-group','data-from':dep,'data-to':step.id});
          const title=makeSvg('title');title.textContent=dep+' → '+step.id+' (нажмите, чтобы удалить связь)';
          const path=makeSvg('path',{class:'scn-edge-path',d,'marker-end':'url(#scn-arrow)'});
          const hit=makeSvg('path',{class:'scn-edge-hit',d});
          hit.addEventListener('click',event=>{
            event.stopPropagation();
            pushScenarioUndo(ed);
            step.needs=list(step.needs).filter(n=>n!==dep);scenariosState.lintResult=null;markScenarioDirty(ed);
            announce('Связь '+dep+' → '+step.id+' удалена');
            renderScenarios();
          });
          g.append(title,path,hit);edgesGroup.append(g);
        }
      }
      svgEl.setAttribute('width',String(maxX));svgEl.setAttribute('height',String(maxY));
    }

    let wireMenuEl=null;
    const closeWireMenu=()=>{if(wireMenuEl){wireMenuEl.remove();wireMenuEl=null;}};
    const openWireCreateMenu=(fromId,clientX,clientY,worldPt)=>{
      closeWireMenu();
      const rect=viewport.getBoundingClientRect();
      const menu=make('div','scn-wire-menu');
      const left=Math.max(10,Math.min(rect.width-264,clientX-rect.left));
      const top=Math.max(10,Math.min(rect.height-320,clientY-rect.top));
      menu.style.left=Math.round(left)+'px';menu.style.top=Math.round(top)+'px';
      menu.append(make('div','scn-wire-menu-head','Создать и подключить шаг'));
      const availIds=new Set(availableScenarioStepTypes().map(t=>t.id));
      const quickTypes=[
        ['instruction','Задание агенту','Свободная инструкция в чате'],
        ['skill','Скилл из профиля','Вызов установленного скилла'],
        ['command','Команда терминала','Тесты, линтер или сборка'],
        ['gate','Вопрос человеку','Пауза до ответа в чате'],
        ['checklist','Чек-лист','Список пунктов на проверку'],
        ['memory.recall','Поиск в памяти','Richi найдёт прошлые записи'],
        ['memory.brief','Сводка из памяти','Richi соберёт сводку по вопросу'],
        ['memory.record','Записать в память','Сохраняет итог в базу знаний Richi'],
        ['compare','Сравнить с baseline','Сравнивает метрики с эталонным запуском'],
        ['condition','Условие / ветвление','Вычисляет флаг для проверок if'],
      ].filter(([type])=>availIds.has(type));
      for(const [type,label,desc] of quickTypes){
        const btn=make('button');btn.type='button';
        btn.append(make('strong','',label),make('span','',desc));
        btn.addEventListener('click',e=>{
          e.stopPropagation();
          pushScenarioUndo(ed);
          const id=uniqueStepId(def.steps,type.replace('.','_'));
          const newStep={id,type,title:label,needs:[fromId],params:scenarioDefaultParams(type)};
          def.steps.push(newStep);
          positions[id]={x:Math.max(12,Math.round(worldPt.x)),y:Math.max(12,Math.round(worldPt.y-SCN_NODE_H/2))};
          scenariosState.selectedStepId=id;scenariosState.lintResult=null;markScenarioDirty(ed);
          closeWireMenu();announce('Создан шаг «'+label+'» после «'+fromId+'»');renderScenarios();
        });
        menu.append(btn);
      }
      viewport.append(menu);wireMenuEl=menu;
    };

    let wireState=null;
    const stopWire=()=>{
      if(!wireState)return;
      wireState.fromPort.classList.remove('active');
      for(const el of nodeEls.values())el.classList.remove('drop-target');
      previewPath.setAttribute('d','');
      wireState=null;
    };

    const lintByStep=scenariosState.lintResult?.byStep||null;
    steps.forEach((step,idx)=>{
      const p=positions[step.id]||{x:SCN_PAD,y:SCN_PAD},role=scenarioStepRole(step.type);
      const node=make('div','scn-node role-'+role+(scenariosState.selectedStepId===step.id?' selected':''));
      node.dataset.stepId=step.id;node.style.left=p.x+'px';node.style.top=p.y+'px';
      const head=make('div','scn-node-head');
      const hLeft=make('div','scn-node-head-left');
      hLeft.append(make('span','scn-node-num','#'+(idx+1)),make('span','scn-node-type',SCN_STEP_META[step.type]?.label||step.type));
      head.append(hLeft);
      const stepLint=lintByStep&&lintByStep[step.id];
      if(stepLint){
        const lb=make('span','scn-node-lint '+stepLint.severity,stepLint.severity==='err'?'✕':stepLint.severity==='warn'?'!':'✓');
        lb.title=stepLint.messages.length?stepLint.messages.join('\n'):'Проверка пройдена';
        head.append(lb);
      }else{
        head.append(make('span','scn-node-badge',SCN_ROLE_LABEL[role]||role));
      }
      const body=make('div','scn-node-body');
      body.append(make('div','scn-node-title',prettyStepLabel(step)));
      const badges=make('div','scn-node-badges');
      if(step.hints?.subagent)badges.append(make('span','scn-node-badge','субагент'));
      if(step.type==='skill'&&step.params?.skill){
        const rawSk=step.params.skill;
        const sk=typeof rawSk==='string'?rawSk:(rawSk.default||(rawSk.per_agent?'по агентам ('+Object.keys(rawSk.per_agent).length+')':''));
        if(sk)badges.append(make('span','scn-node-badge','скилл: '+cut(sk,22)));
      }
      if(step.type==='checklist'&&list(step.params?.items).length)badges.append(make('span','scn-node-badge','пунктов: '+step.params.items.length));
      if(step.type==='gate'&&list(step.params?.options).length)badges.append(make('span','scn-node-badge','вариантов: '+step.params.options.length));
      if(step.if)badges.append(make('span','scn-node-badge','условие'));
      if(step.matrix)badges.append(make('span','scn-node-badge','матрица'));
      if(badges.childElementCount)body.append(badges);

      const inPort=make('button','scn-port scn-port-in');inPort.type='button';inPort.title='Вход: отпустите сюда линию от предыдущего шага';inPort.setAttribute('aria-label','Входной порт шага '+step.id);
      const outPort=make('button','scn-port scn-port-out');outPort.type='button';outPort.title='Выход: потяните к шагу для связи или в пустое место, чтобы создать новый шаг';outPort.setAttribute('aria-label','Выходной порт шага '+step.id);

      body.addEventListener('click',()=>{closeWireMenu();if(scenariosState.selectedStepId!==step.id){scenariosState.selectedStepId=step.id;renderScenarios();}});

      // Drag node by header (zoom-aware)
      let nodeDrag=null;
      head.addEventListener('pointerdown',event=>{
        if(event.button!==0)return;
        event.stopPropagation();closeWireMenu();
        nodeDrag={pointerId:event.pointerId,startX:event.clientX,startY:event.clientY,origX:positions[step.id].x,origY:positions[step.id].y,moved:false,undoSaved:false};
        head.setPointerCapture(event.pointerId);node.classList.add('dragging');
      });
      head.addEventListener('pointermove',event=>{
        if(!nodeDrag||nodeDrag.pointerId!==event.pointerId)return;
        const dx=(event.clientX-nodeDrag.startX)/cam.k,dy=(event.clientY-nodeDrag.startY)/cam.k;
        if(Math.hypot(dx,dy)>3)nodeDrag.moved=true;
        if(!nodeDrag.moved)return;
        if(!nodeDrag.undoSaved){pushScenarioUndo(ed);nodeDrag.undoSaved=true;}
        const nx=Math.max(12,Math.round(nodeDrag.origX+dx)),ny=Math.max(12,Math.round(nodeDrag.origY+dy));
        positions[step.id]={x:nx,y:ny};node.style.left=nx+'px';node.style.top=ny+'px';
        redrawEdges();
      });
      const finishNodeDrag=event=>{
        if(!nodeDrag||nodeDrag.pointerId!==event.pointerId)return;
        const moved=nodeDrag.moved;nodeDrag=null;node.classList.remove('dragging');
        if(head.hasPointerCapture(event.pointerId))head.releasePointerCapture(event.pointerId);
        if(moved){markScenarioDirty(ed);}
        else if(scenariosState.selectedStepId!==step.id){scenariosState.selectedStepId=step.id;renderScenarios();}
      };
      head.addEventListener('pointerup',finishNodeDrag);head.addEventListener('pointercancel',finishNodeDrag);

      // Drag wire from output port to target node OR empty canvas (Wire-to-Create)
      outPort.addEventListener('pointerdown',event=>{
        if(event.button!==0)return;
        event.stopPropagation();closeWireMenu();
        const origin=portCoords(step.id,'out');
        wireState={pointerId:event.pointerId,fromId:step.id,fromPort:outPort,origin,startClientX:event.clientX,startClientY:event.clientY};
        outPort.classList.add('active');outPort.setPointerCapture(event.pointerId);
        previewPath.setAttribute('d',scenarioEdgePath(origin.x,origin.y,origin.x+24,origin.y));
      });
      outPort.addEventListener('pointermove',event=>{
        if(!wireState||wireState.pointerId!==event.pointerId)return;
        const pt=toWorld(event.clientX,event.clientY);
        previewPath.setAttribute('d',scenarioEdgePath(wireState.origin.x,wireState.origin.y,pt.x,pt.y));
        const hitEl=document.elementFromPoint(event.clientX,event.clientY)?.closest?.('.scn-node');
        for(const [sid,el] of nodeEls.entries()){
          el.classList.toggle('drop-target',Boolean(hitEl&&hitEl.dataset.stepId===sid&&sid!==wireState.fromId&&!scenarioWouldCycle(steps,wireState.fromId,sid)));
        }
      });
      const finishWire=event=>{
        if(!wireState||wireState.pointerId!==event.pointerId)return;
        const {fromId,startClientX,startClientY}=wireState;
        if(outPort.hasPointerCapture(event.pointerId))outPort.releasePointerCapture(event.pointerId);
        const hitEl=document.elementFromPoint(event.clientX,event.clientY)?.closest?.('.scn-node');
        const dist=Math.hypot(event.clientX-startClientX,event.clientY-startClientY);
        const worldPt=toWorld(event.clientX,event.clientY);
        stopWire();
        const targetId=hitEl?.dataset?.stepId;
        if(targetId&&targetId!==fromId){
          if(scenarioWouldCycle(steps,fromId,targetId)){
            scenariosState.saveError='Нельзя соединить «'+fromId+'» → «'+targetId+'»: получится цикл зависимостей.';
            renderScenarios();return;
          }
          const target=steps.find(s=>s.id===targetId);
          if(target){
            if(!Array.isArray(target.needs))target.needs=[];
            if(!target.needs.includes(fromId)){pushScenarioUndo(ed);target.needs.push(fromId);scenariosState.saveError='';scenariosState.lintResult=null;markScenarioDirty(ed);announce('Соединены шаги '+fromId+' → '+targetId);renderScenarios();}
          }
        }else if(!targetId&&dist>36){
          openWireCreateMenu(fromId,event.clientX,event.clientY,worldPt);
        }
      };
      const cancelWire=event=>{
        if(!wireState||wireState.pointerId!==event.pointerId)return;
        if(outPort.hasPointerCapture(event.pointerId))outPort.releasePointerCapture(event.pointerId);
        stopWire();
      };
      outPort.addEventListener('pointerup',finishWire);outPort.addEventListener('pointercancel',cancelWire);

      node.append(head,body,inPort,outPort);
      layer.append(node);nodeEls.set(step.id,node);
    });

    // Pan background & wheel zoom centered on cursor
    let panState=null;
    viewport.addEventListener('pointerdown',event=>{
      if(event.button!==0)return;
      if(event.target.closest('.scn-node,.scn-port,.scn-canvas-hud,.scn-wire-menu,.scn-edge-hit'))return;
      closeWireMenu();
      panState={pointerId:event.pointerId,startX:event.clientX,startY:event.clientY,origX:cam.x,origY:cam.y};
      viewport.classList.add('panning');viewport.setPointerCapture(event.pointerId);
    });
    viewport.addEventListener('pointermove',event=>{
      if(!panState||panState.pointerId!==event.pointerId)return;
      cam.x=panState.origX+(event.clientX-panState.startX);
      cam.y=panState.origY+(event.clientY-panState.startY);
      applyCamera();
    });
    const endPan=event=>{
      if(!panState||panState.pointerId!==event.pointerId)return;
      panState=null;viewport.classList.remove('panning');
      if(viewport.hasPointerCapture(event.pointerId))viewport.releasePointerCapture(event.pointerId);
    };
    viewport.addEventListener('pointerup',endPan);viewport.addEventListener('pointercancel',endPan);
    viewport.addEventListener('dblclick',event=>{
      if(event.target.closest('.scn-node,.scn-canvas-hud,.scn-wire-menu'))return;
      fitCamera();
    });
    viewport.addEventListener('wheel',event=>{
      event.preventDefault();closeWireMenu();
      const rect=viewport.getBoundingClientRect();
      const mx=event.clientX-rect.left,my=event.clientY-rect.top;
      const prevK=cam.k;
      const factor=event.deltaY<0?1.1:0.9;
      cam.k=Math.min(1.8,Math.max(0.35,prevK*factor));
      cam.x=mx-(mx-cam.x)*(cam.k/prevK);
      cam.y=my-(my-cam.y)*(cam.k/prevK);
      applyCamera();
    },{passive:false});

    // Floating Zoom / Pan / Fit HUD (Figma / Linear pattern)
    const hud=make('div','scn-canvas-hud');
    const zoomStep=mult=>{
      const vw=viewport.clientWidth||760,vh=viewport.clientHeight||560,prevK=cam.k;
      cam.k=Math.min(1.8,Math.max(0.35,prevK*mult));
      cam.x=vw/2-(vw/2-cam.x)*(cam.k/prevK);
      cam.y=vh/2-(vh/2-cam.y)*(cam.k/prevK);
      applyCamera();
    };
    const btnOut=toolButton('−',()=>zoomStep(0.85));btnOut.title='Уменьшить';
    const btnReset=make('button');btnReset.type='button';btnReset.title='Сбросить масштаб 100%';btnReset.append(zoomReadout);
    btnReset.addEventListener('click',()=>{cam.k=1;applyCamera();});
    const btnIn=toolButton('+',()=>zoomStep(1.18));btnIn.title='Увеличить';
    const btnFit=toolButton('Вписать',fitCamera);btnFit.title='Вписать весь пайплайн в экран';
    hud.append(btnOut,btnReset,btnIn,btnFit);

    viewport.append(layer,hud);
    requestAnimationFrame(()=>{
      redrawEdges();
      if(scenariosState.cameraFitNeeded){scenariosState.cameraFitNeeded=false;fitCamera();}
      else applyCamera();
    });
    return viewport;
  }
  function renderScenarioListView(ed){
    const def=ed.draft,steps=list(def.steps),box=make('div','scn-list-view');
    let dragIndex=-1;
    steps.forEach((step,index)=>{
      const row=make('div','scn-list-row'+(scenariosState.selectedStepId===step.id?' selected':''));
      row.draggable=true;
      const handle=make('span','scn-drag-handle','⋮⋮');handle.title='Перетащите для изменения порядка';
      const info=make('div');
      const stepById=new Map(steps.map(s=>[s.id,s]));
      const needsNames=list(step.needs).map(n=>cut(prettyStepLabel(stepById.get(n)||{id:n}),24));
      info.append(make('div','scn-node-title',(/^\d+\.\s/.test(prettyStepLabel(step))?'':(index+1)+'. ')+prettyStepLabel(step)));
      info.append(make('div','scn-node-id',(SCN_STEP_META[step.type]?.label||step.type)+(step.hints?.subagent?' · субагент':'')+(needsNames.length?' · после: '+needsNames.join(', '):' · стартовый шаг')));
      const controls=make('div','rule-actions');
      const up=toolButton('↑',e=>{e.stopPropagation();if(index>0){pushScenarioUndo(ed);const [m]=def.steps.splice(index,1);def.steps.splice(index-1,0,m);markScenarioDirty(ed);renderScenarios();}},'small');up.disabled=index===0;up.title='Поднять выше';
      const dn=toolButton('↓',e=>{e.stopPropagation();if(index<steps.length-1){pushScenarioUndo(ed);const [m]=def.steps.splice(index,1);def.steps.splice(index+1,0,m);markScenarioDirty(ed);renderScenarios();}},'small');dn.disabled=index===steps.length-1;dn.title='Опустить ниже';
      controls.append(up,dn);
      row.append(handle,info,controls);
      row.addEventListener('click',()=>{scenariosState.selectedStepId=step.id;renderScenarios();});
      row.addEventListener('dragstart',e=>{dragIndex=index;e.dataTransfer.effectAllowed='move';try{e.dataTransfer.setData('text/plain',step.id);}catch{}});
      row.addEventListener('dragover',e=>{e.preventDefault();row.classList.add('drag-over');});
      row.addEventListener('dragleave',()=>row.classList.remove('drag-over'));
      row.addEventListener('drop',e=>{
        e.preventDefault();row.classList.remove('drag-over');
        if(dragIndex<0||dragIndex===index)return;
        const [moved]=def.steps.splice(dragIndex,1);def.steps.splice(index,0,moved);markScenarioDirty(ed);dragIndex=-1;renderScenarios();
      });
      box.append(row);
    });
    if(!steps.length)box.append(make('div','tools-inline-note','В пайплайне пока нет шагов. Нажмите «+ Шаг» выше.'));
    return box;
  }
  const SCN_STEP_GUIDE={
    instruction:{
      who:'Выполняет агент в чате',
      what:'Свободное задание агенту. Агент сам видит текущую ветку, дифф и файлы репозитория — просто опишите, что сделать на этом этапе.',
      examples:[
        ['Self-review diff','Проведи глубокий self-review текущих изменений в ветке: проверь логику, граничные случаи, обработку ошибок и тесты.'],
        ['Независимое ревью','Проведи независимое ревью изменений свежим взглядом, не опираясь на рассуждения автора. Найди скрытые баги и риски.'],
        ['Итоговый отчёт','Собери результаты предыдущих шагов в короткий приоритизированный план: что чиним сразу, что выносим отдельно.'],
      ],
    },
    skill:{
      who:'Выполняет агент в чате (через скилл)',
      what:'Агент вызывает конкретный установленный скилл из вашего профиля (например, ревью, тестирование, описание PR). Найдите нужный скилл поиском ниже.',
      examples:[
        ['Проверить текущую ветку','Примени скилл к текущим изменениям в ветке и выпиши конкретные замечания.'],
      ],
    },
    command:{
      who:'Выполняет агент в терминале',
      what:'Агент запускает консольную команду (тесты, линтер, сборку) и проверяет код возврата. Можно указать ожидаемый exit code.',
      examples:[
        ['Тесты Python','pytest -q'],
        ['Тесты JS/TS','npm test'],
        ['Статус git','git status -s'],
      ],
    },
    gate:{
      who:'Ждёт ответа человека в чате',
      what:'Пайплайн ставится на паузу и задаёт вам вопрос прямо в чате. Следующие шаги запустятся только после вашего ответа.',
      examples:[
        ['Подтверждение правок','Принимаем найденные замечания и переходим к исправлению?'],
        ['Готовность к мержу','Все проверки пройдены. Можно открывать/обновлять PR?'],
      ],
    },
    checklist:{
      who:'Ждёт отметки человека в чате',
      what:'Контрольный список пунктов, которые человек подтверждает перед продолжением (например, чек-лист перед релизом или мержем). Добавляйте пункты кнопкой «+ Добавить пункт».',
      examples:[],
    },
    'memory.recall':{
      who:'Выполняет Richi автоматически (до агента)',
      what:'Движок Richi сам ищет подходящие решения, факты и прошлые задачи в вашей локальной памяти и передаёт найденное следующим шагам — агент не тратит токены на повторный поиск.',
      examples:[
        ['Архитектура и ограничения','архитектурные решения ограничения контракты'],
        ['Прошлые баги','известные баги регрессии инциденты'],
        ['Правила релиза','релиз деплой миграции'],
      ],
    },
    'memory.brief':{
      who:'Выполняет Richi автоматически (до агента)',
      what:'Движок Richi сам собирает компактную сводку из вашей базы знаний по вопросу и подтягивает итоги последних запусков этого сценария.',
      examples:[
        ['Правила проекта','Какие архитектурные решения, ограничения и договорённости действуют в этом проекте?'],
        ['Риски изменений','Какие прошлые баги, зависимости и риски связаны с этой областью кода?'],
      ],
    },
    'memory.record':{
      who:'Выполняет Richi автоматически (память процесса)',
      what:'Сохраняет подтверждённый итог, решение или находку в долгосрочную память Richi со ссылкой на этот запуск, чтобы следующие запуски стали умнее.',
      examples:[
        ['Итог ревью','Зафиксировать принятые решения и найденные риски'],
      ],
    },
    compare:{
      who:'Выполняет Richi автоматически (сравнение с эталоном)',
      what:'Сравнивает числовые метрики и списки выбранного шага с закреплённым baseline или последним успешным запуском этого же сценария.',
      examples:[],
    },
    condition:{
      who:'Выполняет Richi автоматически (развилка)',
      what:'Проверяет правило «да / нет» (например, какой вариант ответа выбрал человек на предыдущем вопросе) и решает, идти ли по ветке дальше.',
      examples:[],
    },
  };
  function stepSlugFromTitle(title,steps,currentId){
    const raw=typeof ruleSlug==='function'?ruleSlug(title):String(title||'').toLowerCase().replace(/[^a-z0-9]+/g,'_');
    const base=(raw.replace(/-/g,'_').replace(/^[^a-z]+/,'').slice(0,32))||'step';
    const used=new Set(list(steps).filter(s=>s.id!==currentId).map(s=>s.id));
    if(!used.has(base))return base;
    for(let i=2;i<200;i++)if(!used.has(base+'_'+i))return base+'_'+i;
    return currentId;
  }
  function renderSearchableSkillPicker(currentSkill,onPick,agentId=''){
    const wrap=make('div','scn-skill-picker');
    const selectedBox=make('div','scn-skill-selected');
    const selectedName=make('strong');
    const clearBtn=toolButton('Сбросить',()=>{chosen='';onPick('');updateView();},'small');
    selectedBox.append(selectedName,clearBtn);
    const searchInput=make('input');searchInput.type='search';searchInput.placeholder=agentId?'Поиск скилла для '+agentName(agentId)+'…':'Поиск скилла по названию или описанию…';searchInput.autocomplete='off';searchInput.spellcheck=false;
    searchInput.setAttribute('aria-label',agentId?'Поиск скилла для '+agentName(agentId):'Поиск скилла');
    const metaRow=make('div','rule-field-hint');
    const listBox=make('div','scn-skill-list');
    let chosen=currentSkill||'';

    function updateView(){
      selectedBox.hidden=!chosen;
      const all=list(rulesState.skills?.skills);
      const hasForAgent=!agentId||!all.length||all.some(sk=>sk.name===chosen&&list(sk.agents).includes(agentId));
      selectedName.textContent='Выбран скилл: '+chosen+(chosen&&agentId&&!hasForAgent?' (не найден у '+agentName(agentId)+')':'');
      const q=normalized(searchInput.value);
      const matched=all.filter(sk=>{
        if(!q)return true;
        const hay=normalized(sk.name+' '+(sk.description||'')+' '+list(sk.agents).join(' '));
        return q.split(/\s+/).filter(Boolean).every(w=>hay.includes(w));
      }).sort((a,b)=>{
        if(agentId){
          const aHas=list(a.agents).includes(agentId)?0:1;
          const bHas=list(b.agents).includes(agentId)?0:1;
          if(aHas!==bHas)return aHas-bHas;
        }
        return a.name.localeCompare(b.name);
      });
      listBox.replaceChildren();
      if(rulesState.skillsLoading&&!all.length){
        metaRow.textContent='Загружаем каталог установленных скиллов…';
        listBox.append(make('div','tools-inline-note','Загрузка…'));
        return;
      }
      const agentCount=agentId?all.filter(sk=>list(sk.agents).includes(agentId)).length:all.length;
      metaRow.textContent=all.length
        ?(q?'Найдено '+matched.length+' из '+all.length+' скиллов':(agentId?'У '+agentName(agentId)+' установлено '+agentCount+' из '+all.length+' скиллов — начните печатать для поиска':'Всего доступно '+all.length+' скиллов — начните печатать, чтобы отфильтровать'))
        :'Каталог скиллов пуст — введите имя скилла вручную в поле поиска выше.';
      for(const sk of matched.slice(0,40)){
        const btn=make('button','scn-skill-item');btn.type='button';
        btn.setAttribute('aria-pressed',String(chosen===sk.name));
        const head=make('div','scn-skill-item-head');
        const onThisAgent=!agentId||list(sk.agents).includes(agentId);
        head.append(make('strong','',(chosen===sk.name?'✓ ':'')+sk.name),make('span','scn-node-badge',(onThisAgent?'':'нет у '+agentName(agentId)+' · ')+(list(sk.agents).map(agentName).join(', ')||'скилл')));
        btn.append(head);
        if(sk.description)btn.append(make('div','scn-skill-item-desc',cut(sk.description,120)));
        btn.addEventListener('click',()=>{chosen=sk.name;onPick(chosen);updateView();});
        listBox.append(btn);
      }
      if(matched.length>40)listBox.append(make('div','tools-inline-note','Показаны первые 40 из '+matched.length+'. Уточните поиск.'));
      const rawQuery=searchInput.value.trim();
      if(rawQuery&&!all.some(sk=>sk.name===rawQuery)){
        const customBtn=toolButton('Использовать скилл вручную: «'+rawQuery+'»',()=>{chosen=rawQuery;onPick(chosen);updateView();},'small');
        customBtn.style.margin='4px';
        listBox.append(customBtn);
      }
      if(!listBox.childElementCount)listBox.append(make('div','tools-inline-note','Ничего не найдено. Введите точное имя скилла, чтобы указать его вручную.'));
    }
    searchInput.addEventListener('input',updateView);
    wrap.addEventListener('catalogchange',updateView);
    wrap.append(selectedBox,searchInput,metaRow,listBox);
    updateView();
    return wrap;
  }
  function renderConditionHelper(def,steps,currentStep,currentExpr,onExpr){
    const wrap=make('div');wrap.style.display='grid';wrap.style.gap='8px';
    const ancestors=[...scenarioAncestors(steps,currentStep.id)];
    const stepById=new Map(steps.map(s=>[s.id,s]));
    const sources=[];
    for(const aid of ancestors){
      const anc=stepById.get(aid);if(!anc)continue;
      if(anc.type==='gate'&&!anc.matrix)sources.push({path:'steps.'+aid+'.answer',type:'string',label:'Ответ на «'+prettyStepLabel(anc)+'»',options:list(anc.params?.options)});
      if(anc.type==='condition')sources.push({path:'steps.'+aid+'.data.value',type:'boolean',label:'Результат условия «'+prettyStepLabel(anc)+'»',options:['true','false']});
      sources.push({path:'steps.'+aid+'.status',type:'string',label:'Статус шага «'+prettyStepLabel(anc)+'»',options:['done','skipped','failed']});
    }
    for(const [name,spec] of Object.entries(def.inputs||{})){
      if(spec?.type==='secret')continue;
      const t=spec?.type||'string';
      sources.push({path:'inputs.'+name,type:t,label:'Параметр «'+(spec?.title||name)+'»',options:t==='boolean'?['true','false']:list(spec?.options)});
    }
    const row=make('div','scn-cond-builder');
    const leftSel=make('select');leftSel.add(new Option('— выберите, что проверять —',''));
    for(const src of sources)leftSel.add(new Option(src.label,src.path));
    const opSel=make('select');
    const rightIn=make('input');rightIn.type='text';rightIn.placeholder='Значение (напр. Подтвердить)';
    const rightList=make('datalist');rightList.id='scn-cond-opts-'+(++ruleFieldCount);rightIn.setAttribute('list',rightList.id);
    const syncOperatorsAndOptions=()=>{
      const found=sources.find(s=>s.path===leftSel.value);
      const t=found?.type||'string';
      opSel.replaceChildren();
      if(t==='multiselect'){
        opSel.add(new Option('содержит (in)','in'));opSel.add(new Option('не содержит (not in)','not in'));
      }else if(t==='number'){
        opSel.add(new Option('равно (==)','=='));opSel.add(new Option('не равно (!=)','!='));
        opSel.add(new Option('больше (>)','>'));opSel.add(new Option('меньше (<)','<'));
        opSel.add(new Option('>=','>='));opSel.add(new Option('<=','<='));
      }else{
        opSel.add(new Option('равно (==)','=='));opSel.add(new Option('не равно (!=)','!='));
      }
      rightList.replaceChildren();
      for(const opt of list(found?.options))rightList.append(new Option(opt));
      rightIn.placeholder=t==='boolean'?'true или false':(t==='number'?'Число (напр. 5)':'Значение (напр. Подтвердить)');
    };
    syncOperatorsAndOptions();
    const rawIn=make('input');rawIn.type='text';rawIn.style.fontFamily='ui-monospace,SFMono-Regular,monospace';rawIn.value=currentExpr||'';rawIn.placeholder="Например: steps.human_confirm.answer == 'Подтвердить'";
    const applyBuilder=()=>{
      if(!leftSel.value||!rightIn.value.trim())return;
      const found=sources.find(s=>s.path===leftSel.value),t=found?.type||'string',val=rightIn.value.trim();
      const quoteStr=s=>"'"+s.replace(/\\/g,'\\\\').replace(/'/g,"\\'")+"'";
      let built='';
      if(t==='boolean'){
        const low=val.toLowerCase();
        const lit=(low==='true'||low==='да'||low==='1')?'true':'false';
        built=leftSel.value+' '+opSel.value+' '+lit;
      }else if(t==='number'){
        const num=Number(val);
        if(!Number.isFinite(num))return;
        built=leftSel.value+' '+opSel.value+' '+String(num);
      }else if(t==='multiselect'){
        built=quoteStr(val)+' '+opSel.value+' '+leftSel.value;
      }else{
        built=leftSel.value+' '+opSel.value+' '+quoteStr(val);
      }
      rawIn.value=built;onExpr(built);
    };
    leftSel.addEventListener('change',()=>{syncOperatorsAndOptions();const found=sources.find(s=>s.path===leftSel.value);if(found?.options?.length)rightIn.value=found.options[0];else rightIn.value='';applyBuilder();});
    opSel.addEventListener('change',applyBuilder);
    rightIn.addEventListener('input',applyBuilder);
    rawIn.addEventListener('input',()=>{onExpr(rawIn.value);});
    if(sources.length){
      row.append(leftSel,opSel,rightIn);
      wrap.append(make('span','rule-field-hint','Конструктор условия: выберите предыдущий шаг или параметр — типизированная формула соберётся сама.'),row,rightList);
    }else{
      wrap.append(make('span','rule-field-hint','Чтобы проверять результат предыдущего шага (например, ответ на «Вопрос человеку»), сначала соедините этот шаг стрелкой с предыдущим.'));
    }
    wrap.append(ruleField('Формула условия (можно править вручную)',rawIn));
    return wrap;
  }
  function renderStepInspector(ed){
    const box=make('aside','scn-inspector'),def=ed.draft,steps=list(def.steps);
    const step=steps.find(s=>s.id===scenariosState.selectedStepId)||steps[0]||null;
    if(!step){
      box.append(make('h3','','Настройка шага'),make('p','tools-inline-note','Нажмите на любой шаг на графе или добавьте новый кнопкой «+ Шаг», чтобы настроить его.'));
      return box;
    }
    scenariosState.selectedStepId=step.id;
    const stepIndex=steps.indexOf(step),stepById=new Map(steps.map(s=>[s.id,s])),stepNumById=new Map(steps.map((s,i)=>[s.id,i+1]));
    const ancestors=scenarioAncestors(steps,step.id);
    const role=scenarioStepRole(step.type),guide=SCN_STEP_GUIDE[step.type]||SCN_STEP_GUIDE.instruction;
    const headRow=make('div','scn-run-top');
    const titleWrap=make('div','scn-node-head-left');
    titleWrap.append(make('span','scn-node-num','#'+(stepIndex+1)),make('h3','',prettyStepLabel(step)));
    headRow.append(titleWrap,make('code','scn-card-id',step.id));
    box.append(headRow);

    const typeSel=make('select');
    const availTypes=availableScenarioStepTypes();
    for(const item of availTypes)typeSel.add(new Option(item.label,item.id));
    if(!availTypes.some(t=>t.id===step.type)){
      const known=SCN_STEP_META[step.type];
      typeSel.add(new Option(known?known.label:(step.type+' (неподдерживаемый тип)'),step.type));
    }
    typeSel.value=step.type;
    typeSel.addEventListener('change',()=>{
      pushScenarioUndo(ed);
      pruneStepInvalidFields(ed,step.id);
      step.type=typeSel.value;step.params=scenarioDefaultParams(step.type);
      const nextRole=scenarioStepRole(step.type);
      if(nextRole!=='agent'){delete step.executor;delete step.outputs;delete step.checks;delete step.retry;delete step.hints;delete step.continue_on_failure;}
      if(nextRole==='richi'){delete step.matrix;delete step.matrix_mode;}
      scenariosState.lintResult=null;markScenarioDirty(ed);renderScenarios();
    });
    box.append(ruleField('Тип шага',typeSel));

    // Plain-Russian callout explaining what this step type does and who executes it
    const callout=make('div','scn-type-callout');
    callout.append(make('strong','',guide.who),make('span','',guide.what));
    box.append(callout);

    const bindTextUndo=el=>{
      let captured=false;
      el.addEventListener('focus',()=>{captured=false;});
      el.addEventListener('beforeinput',()=>{if(!captured){pushScenarioUndo(ed);captured=true;}});
    };

    const titleInput=make('input');titleInput.type='text';titleInput.value=step.title||'';titleInput.placeholder='Например: 1. Self-review или Проверка безопасности';
    bindTextUndo(titleInput);
    titleInput.addEventListener('input',()=>{
      step.title=titleInput.value;markScenarioDirty(ed);
      const labelText=prettyStepLabel(step);
      const nodeEl=$('scenarios-content')?.querySelector('.scn-node[data-step-id="'+step.id+'"] .scn-node-title');
      if(nodeEl)nodeEl.textContent=labelText;
      const inspH3=box.querySelector('h3');if(inspH3)inspH3.textContent=labelText;
    });
    box.append(ruleField('Название шага',titleInput));

    if(role==='agent'){
      const subWrap=make('label','check-label'),subCb=make('input');
      subCb.type='checkbox';subCb.checked=Boolean(step.hints?.subagent);
      subCb.addEventListener('change',()=>{
        pushScenarioUndo(ed);
        if(!step.hints)step.hints={};
        if(subCb.checked)step.hints.subagent=true;else delete step.hints.subagent;
        if(!Object.keys(step.hints).length)delete step.hints;
        markScenarioDirty(ed);renderScenarios();
      });
      subWrap.append(subCb,document.createTextNode('Запускать в отдельном независимом агенте (субагент)'));
      box.append(subWrap);
    }

    // Rich Inline Pill Prompt Editor (Notion / Dify / Linear UX: users NEVER see or type raw {{steps.x.summary}}!)
    const FIELD_LABELS={summary:'Итог',answer:'Ответ',data:'Данные',items:'Элементы',status:'Статус',exit_code:'Код выхода'};
    const CTX_LABELS={'project.path':'Папка проекта','now.date':'Текущая дата','now.utc':'Время UTC','run.id':'ID запуска','agent.id':'ID агента'};
    const stepPrimaryRef=src=>{
      if(src.matrix&&(src.type==='gate'||src.type==='checklist'))return {fld:'items',tag:'Ответы матрицы',noun:'ответы'};
      if(src.type==='gate')return {fld:'answer',tag:'Ответ человека',noun:'ответ'};
      if(src.matrix)return {fld:'data',tag:'Данные матрицы',noun:'данные'};
      return {fld:'summary',tag:'Итог шага',noun:'итог'};
    };
    const buildMentionItems=(opts={})=>{
      const items=[],wrap=expr=>'{{'+expr+(opts.requireShell?' | shell':'')+'}}';
      const directSet=new Set(list(step.needs));
      const orderedAnc=[...list(step.needs).filter(id=>ancestors.has(id)),...[...ancestors].filter(id=>!directSet.has(id))];
      for(const anc of orderedAnc){
        const src=stepById.get(anc);if(!src)continue;
        const num=stepNumById.get(anc),ref=stepPrimaryRef(src);
        items.push({group:'Предыдущие шаги',label:'#'+num+' '+cut(prettyStepLabel(src),32),tag:ref.tag,token:wrap('steps.'+anc+'.'+ref.fld),stepId:anc,search:normalized(num+' '+prettyStepLabel(src)+' '+anc+' '+ref.tag)});
      }
      for(const [name,spec] of Object.entries(def.inputs||{})){
        if(opts.forbidSecret&&spec?.type==='secret')continue;
        items.push({group:'Параметры запуска',label:spec?.title||name,tag:'Параметр',token:wrap('inputs.'+name),stepId:'',search:normalized((spec?.title||'')+' '+name+' параметр')});
      }
      items.push(
        {group:'Контекст',label:'Папка проекта',tag:'Контекст',token:wrap('project.path'),stepId:'',search:'папка проекта path project'},
        {group:'Контекст',label:'Текущая дата',tag:'Контекст',token:wrap('now.date'),stepId:'',search:'текущая дата date now'},
      );
      return items;
    };
    const buildLivePreview=(targetEl,opts={})=>{
      const boxEl=make('div','scn-live-preview');boxEl.hidden=true;
      const update=()=>{
        const parts=scenarioParseTokens(targetEl.value);
        if(!parts.some(pt=>pt.type==='token')){boxEl.hidden=true;return;}
        boxEl.hidden=false;boxEl.replaceChildren();
        boxEl.append(make('span','scn-live-preview-label','Подстановки'));
        const content=make('div');
        for(const pt of parts){
          if(pt.type==='text'){content.append(document.createTextNode(pt.value));continue;}
          const pill=make('button','scn-var-pill');pill.type='button';pill.title=pt.raw;
          const badShell=opts.requireShell&&!/(^|\|\s*)shell\s*$/.test(pt.filter);
          if(pt.root==='steps'){
            const src=stepById.get(pt.target),num=stepNumById.get(pt.target);
            const ok=Boolean(src&&ancestors.has(pt.target))&&!badShell;
            if(!ok)pill.classList.add('bad');else pill.classList.add('role-'+scenarioStepRole(src.type));
            pill.textContent=src?('← #'+num+' '+cut(prettyStepLabel(src),22)+' · '+(FIELD_LABELS[pt.field]||pt.field||'данные')):('⚠ шаг «'+pt.target+'»');
          }else if(pt.root==='inputs'){
            const spec=def.inputs&&def.inputs[pt.target];
            if(!spec||(opts.forbidSecret&&spec?.type==='secret')||badShell)pill.classList.add('bad');
            pill.textContent=spec?('Параметр · '+(spec.title||pt.target)):('⚠ параметр «'+pt.target+'»');
          }else{
            if(badShell)pill.classList.add('bad');
            pill.textContent=CTX_LABELS[pt.path]||pt.path;
          }
          content.append(pill);
        }
        boxEl.append(content);
      };
      targetEl.addEventListener('input',update);update();
      return boxEl;
    };
    const createRichPromptEditor=(initialValue,opts={},onChange)=>{
      const wrap=make('div','scn-rich-wrap');
      const editor=make('div','scn-rich-editor'+(opts.mono?' mono':''));
      editor.contentEditable='true';editor.spellcheck=!opts.mono;
      editor.setAttribute('role','textbox');editor.setAttribute('aria-multiline','true');
      editor.dataset.placeholder=opts.placeholder||'';
      const listeners=[];
      let savedRange=null,mentionEl=null,mentionState=null,undoCaptured=false;

      const makeInlinePill=pt=>{
        const pill=make('span','scn-inline-pill');
        pill.contentEditable='false';pill.dataset.token=pt.raw;
        const badShell=opts.requireShell&&!/(^|\|\s*)shell\s*$/.test(pt.filter);
        let text='',srcStep=null;
        if(pt.root==='steps'){
          srcStep=stepById.get(pt.target)||null;
          const num=stepNumById.get(pt.target);
          const ok=Boolean(srcStep&&ancestors.has(pt.target))&&!badShell;
          if(!ok)pill.classList.add('bad');else pill.classList.add('role-'+scenarioStepRole(srcStep.type));
          const fld=FIELD_LABELS[pt.field]||pt.field||'данные';
          text=srcStep
            ?('← #'+num+' '+cut(prettyStepLabel(srcStep),24)+' · '+fld+(ancestors.has(pt.target)?'':' ⚠ не связан')+(badShell?' ⚠ | shell':''))
            :('⚠ неизвестный шаг «'+pt.target+'»');
          pill.title=srcStep?('Подстановка: '+pt.raw+' (Cmd/Ctrl+клик или двойной клик — перейти к шагу)'):('Неизвестный шаг: '+pt.raw);
        }else if(pt.root==='inputs'){
          const spec=def.inputs&&def.inputs[pt.target];
          const badSec=opts.forbidSecret&&spec?.type==='secret';
          if(!spec||badSec||badShell)pill.classList.add('bad');
          text=spec?('Параметр · '+(spec.title||pt.target)+(badSec?' ⚠ секрет':'')):('⚠ параметр «'+pt.target+'»');
          pill.title='Параметр запуска: '+pt.raw;
        }else{
          if(badShell)pill.classList.add('bad');
          text=(CTX_LABELS[pt.path]||pt.path)+(badShell?' ⚠ | shell':'');
          pill.title='Контекст: '+pt.raw;
        }
        const lbl=make('span','',text);
        if(srcStep){
          pill.addEventListener('mouseenter',()=>highlightScenarioNode(srcStep.id,true,step.id));
          pill.addEventListener('mouseleave',()=>highlightScenarioNode(srcStep.id,false,step.id));
          const jumpToSrc=e=>{e.preventDefault();e.stopPropagation();highlightScenarioNode(srcStep.id,false,step.id);scenariosState.selectedStepId=srcStep.id;renderScenarios();};
          lbl.addEventListener('click',e=>{if(e.metaKey||e.ctrlKey)jumpToSrc(e);});
          lbl.addEventListener('dblclick',jumpToSrc);
        }
        const xBtn=make('button','scn-inline-pill-x','×');xBtn.type='button';xBtn.title='Убрать подстановку';xBtn.setAttribute('aria-label','Убрать подстановку');
        xBtn.addEventListener('mousedown',e=>{e.preventDefault();e.stopPropagation();});
        xBtn.addEventListener('click',e=>{
          e.preventDefault();e.stopPropagation();
          pushScenarioUndo(ed);
          if(srcStep)highlightScenarioNode(srcStep.id,false,step.id);
          pill.remove();emitChange();editor.focus();
        });
        pill.append(lbl,xBtn);
        return pill;
      };
      const buildNodesFromText=text=>{
        const frag=document.createDocumentFragment();
        for(const pt of scenarioParseTokens(text)){
          if(pt.type==='token'){frag.append(makeInlinePill(pt));continue;}
          const lines=pt.value.split('\n');
          lines.forEach((ln,i)=>{
            if(i>0)frag.append(make('br'));
            if(ln)frag.append(document.createTextNode(ln));
          });
        }
        return frag;
      };
      const serializeNode=node=>{
        let out='';
        for(const ch of node.childNodes){
          if(ch.nodeType===3){out+=ch.nodeValue.replace(/\u200B/g,'');}
          else if(ch.nodeType===1){
            if(ch.dataset&&ch.dataset.token){out+=ch.dataset.token;}
            else if(ch.tagName==='BR'){if(!ch.dataset.sentinel)out+='\n';}
            else{
              const inner=serializeNode(ch);
              if(out&&!out.endsWith('\n')&&(ch.tagName==='DIV'||ch.tagName==='P'))out+='\n';
              out+=inner;
            }
          }
        }
        return out;
      };
      const getValue=()=>serializeNode(editor);
      const syncEmpty=()=>{editor.classList.toggle('is-empty',!getValue().length);};
      const emitChange=()=>{
        const val=getValue();syncEmpty();
        onChange(val);markScenarioDirty(ed);
        listeners.forEach(fn=>fn(val));
      };
      const setValue=(text,silent=false)=>{
        editor.replaceChildren(buildNodesFromText(text));
        syncEmpty();
        if(!silent){onChange(text);markScenarioDirty(ed);listeners.forEach(fn=>fn(text));}
      };

      const closeMention=()=>{if(mentionEl){mentionEl.remove();mentionEl=null;}mentionState=null;};
      const saveSelection=()=>{
        const sel=window.getSelection();
        if(sel&&sel.rangeCount&&editor.contains(sel.anchorNode))savedRange=sel.getRangeAt(0).cloneRange();
      };
      const placeCaretAfter=node=>{
        const sel=window.getSelection();if(!sel)return;
        const r=document.createRange();r.setStartAfter(node);r.collapse(true);
        sel.removeAllRanges();sel.addRange(r);savedRange=r.cloneRange();
      };
      const insertPillToken=(token,replaceRange=null)=>{
        pushScenarioUndo(ed);
        editor.focus();
        const sel=window.getSelection();
        let range=replaceRange||savedRange;
        if(!range||!editor.contains(range.startContainer)){
          range=document.createRange();range.selectNodeContents(editor);range.collapse(false);
        }
        range.deleteContents();
        const pt=scenarioParseTokens(token).find(x=>x.type==='token');
        if(pt){
          const pill=makeInlinePill(pt),sp=document.createTextNode(' ');
          range.insertNode(sp);range.insertNode(pill);
          placeCaretAfter(sp);
        }
        closeMention();emitChange();
      };
      const openMentionMenu=(query='',replaceRange=null)=>{
        const all=buildMentionItems(opts);
        const q=normalized(query);
        const matched=q?all.filter(it=>it.search.includes(q)):all;
        closeMention();
        const menu=make('div','scn-mention-menu');
        mentionState={items:matched,activeIndex:0,replaceRange};
        if(!matched.length){
          menu.append(make('div','scn-mention-head','Ничего не найдено — соедините этот шаг стрелкой с нужным шагом'));
        }else{
          let lastGroup='';
          matched.forEach((it,idx)=>{
            if(it.group!==lastGroup){lastGroup=it.group;menu.append(make('div','scn-mention-head',it.group));}
            const btn=make('button','scn-mention-item'+(idx===0?' active':''));btn.type='button';
            btn.append(make('span','',it.label),make('small','',it.tag));
            btn.addEventListener('mousedown',e=>{e.preventDefault();});
            btn.addEventListener('mouseenter',()=>{
              mentionState.activeIndex=idx;
              menu.querySelectorAll('.scn-mention-item').forEach((b,i)=>b.classList.toggle('active',i===idx));
              if(it.stepId)highlightScenarioNode(it.stepId,true,step.id);
            });
            btn.addEventListener('mouseleave',()=>{if(it.stepId)highlightScenarioNode(it.stepId,false,step.id);});
            btn.addEventListener('click',()=>{if(it.stepId)highlightScenarioNode(it.stepId,false,step.id);insertPillToken(it.token,replaceRange);});
            menu.append(btn);
          });
        }
        wrap.append(menu);mentionEl=menu;
      };
      const checkMentionTrigger=()=>{
        const sel=window.getSelection();
        if(!sel||!sel.isCollapsed||!sel.rangeCount||!editor.contains(sel.anchorNode)){closeMention();return;}
        const node=sel.anchorNode,offset=sel.anchorOffset;
        if(node.nodeType!==3){closeMention();return;}
        const before=node.nodeValue.slice(0,offset);
        const m=before.match(/(?:^|[\s([{"'])@([^\s@]{0,24})$/);
        if(!m){closeMention();return;}
        const triggerStart=offset-1-m[1].length;
        const r=document.createRange();r.setStart(node,triggerStart);r.setEnd(node,offset);
        openMentionMenu(m[1],r);
      };

      editor.addEventListener('focus',()=>{undoCaptured=false;});
      editor.addEventListener('beforeinput',()=>{if(!undoCaptured){pushScenarioUndo(ed);undoCaptured=true;}});
      editor.addEventListener('keyup',()=>{saveSelection();});
      editor.addEventListener('mouseup',saveSelection);
      editor.addEventListener('input',()=>{saveSelection();emitChange();checkMentionTrigger();});
      editor.addEventListener('keydown',e=>{
        if(mentionState&&mentionEl){
          const btns=[...mentionEl.querySelectorAll('.scn-mention-item')];
          if(e.key==='ArrowDown'&&btns.length){
            e.preventDefault();mentionState.activeIndex=(mentionState.activeIndex+1)%btns.length;
            btns.forEach((b,i)=>b.classList.toggle('active',i===mentionState.activeIndex));btns[mentionState.activeIndex]?.scrollIntoView({block:'nearest'});return;
          }
          if(e.key==='ArrowUp'&&btns.length){
            e.preventDefault();mentionState.activeIndex=(mentionState.activeIndex-1+btns.length)%btns.length;
            btns.forEach((b,i)=>b.classList.toggle('active',i===mentionState.activeIndex));btns[mentionState.activeIndex]?.scrollIntoView({block:'nearest'});return;
          }
          if((e.key==='Enter'||e.key==='Tab')&&mentionState.items[mentionState.activeIndex]){
            e.preventDefault();const picked=mentionState.items[mentionState.activeIndex];
            insertPillToken(picked.token,mentionState.replaceRange);return;
          }
          if(e.key==='Escape'){e.preventDefault();closeMention();return;}
        }
        if(e.key==='Enter'&&!mentionEl){
          if(opts.singleLine){e.preventDefault();return;}
          if(!e.shiftKey){
            e.preventDefault();
            const sel=window.getSelection();if(!sel||!sel.rangeCount)return;
            const r=sel.getRangeAt(0);r.deleteContents();
            const br=make('br');r.insertNode(br);
            if(!br.nextSibling){const sent=make('br');sent.dataset.sentinel='1';br.after(sent);}
            placeCaretAfter(br);emitChange();
          }
        }
      });
      editor.addEventListener('paste',e=>{
        e.preventDefault();
        let plain=e.clipboardData?.getData('text/plain')||'';if(!plain)return;
        if(opts.singleLine)plain=plain.replace(/[\r\n]+/g,' ').trim();
        pushScenarioUndo(ed);
        const sel=window.getSelection();if(!sel||!sel.rangeCount)return;
        const r=sel.getRangeAt(0);r.deleteContents();
        const frag=buildNodesFromText(plain),last=frag.lastChild;
        r.insertNode(frag);if(last)placeCaretAfter(last);
        emitChange();
      });
      editor.addEventListener('blur',()=>{setTimeout(()=>{if(!wrap.contains(document.activeElement))closeMention();},160);});

      const bar=make('div','scn-rich-bar');
      bar.append(make('span','',ancestors.size?'Наберите @ или нажмите кнопку справа, чтобы сослаться на шаг':'Наберите @ чтобы вставить контекст (папку проекта, дату)'));
      const insertBtn=toolButton('+ Вставить шаг / данные ▾',e=>{
        e.stopPropagation();
        if(mentionEl){closeMention();return;}
        openMentionMenu('',savedRange);
      },'scn-example-btn');
      insertBtn.addEventListener('mousedown',e=>{e.preventDefault();saveSelection();});
      bar.append(insertBtn);
      wrap.append(editor,bar);
      setValue(initialValue||'',true);

      return {
        el:wrap,
        getValue,
        setValue,
        toggleToken(token){
          pushScenarioUndo(ed);
          const res=scenarioToggleToken(getValue(),token);
          setValue(res.value);
          return res.active;
        },
        onInput(fn){listeners.push(fn);},
        focus(){editor.focus();},
      };
    };

    let activePrompt=null;
    const incomingSlot=make('div');
    box.append(incomingSlot);

    const renderExampleButtons=(target,examples)=>{
      if(!list(examples).length)return null;
      const row=make('div','scn-examples');
      row.append(make('span','rule-field-hint','Примеры:'));
      for(const [label,text] of examples){
        row.append(toolButton(label,()=>{
          pushScenarioUndo(ed);
          if(typeof target.setValue==='function'){target.setValue(text);target.focus();}
          else{target.value=text;target.dispatchEvent(new Event('input'));target.focus();}
        },'scn-example-btn'));
      }
      return row;
    };

    if(!step.params||typeof step.params!=='object')step.params=SCN_STEP_META[step.type]?scenarioDefaultParams(step.type):{};
    const invalidFields=ed.invalidFields instanceof Map?ed.invalidFields:(ed.invalidFields=new Map());
    const bindBoundedInt=(inp,errEl,fieldKey,min,max,onSet,onClear)=>{
      const fullKey=step.id+':'+fieldKey;
      const savedBad=invalidFields.get(fullKey);
      if(savedBad){inp.value=savedBad.raw;errEl.textContent=savedBad.error;}
      inp.addEventListener('input',()=>{
        const raw=inp.value.trim(),v=scnInt(raw);
        if(raw===''){onClear();errEl.textContent='';invalidFields.delete(fullKey);}
        else if(v===null||v<min||v>max){const msg='Введите целое число от '+min+' до '+max;errEl.textContent=msg;invalidFields.set(fullKey,{stepId:step.id,fieldKey,label:fieldKey,raw:inp.value,error:msg});}
        else{onSet(v);errEl.textContent='';invalidFields.delete(fullKey);}
        markScenarioDirty(ed);
      });
    };
    const p=step.params;
    if(step.type==='instruction'){
      const rich=createRichPromptEditor(p.text||'',{placeholder:'Напишите простым языком, что должен сделать агент на этом шаге…'},val=>{p.text=val;});
      activePrompt=rich;
      const f=ruleField('Что сделать агенту',rich.el);
      const ex=renderExampleButtons(rich,guide.examples);if(ex)f.append(ex);
      box.append(f);
    }else if(step.type==='skill'){
      if(!p.skill||typeof p.skill!=='object'||Array.isArray(p.skill))p.skill={default:typeof p.skill==='string'?p.skill:''};
      const picker=renderSearchableSkillPicker(typeof p.skill.default==='string'?p.skill.default:'',val=>{
        pushScenarioUndo(ed);
        if(val)p.skill.default=val;else delete p.skill.default;
        markScenarioDirty(ed);renderScenarios();
      });
      box.append(ruleField('Общий скилл для всех агентов (с поиском)',picker));
      const perAgentMap=(p.skill.per_agent&&typeof p.skill.per_agent==='object'&&!Array.isArray(p.skill.per_agent))?p.skill.per_agent:{};
      const agentIds=[...new Set([...knownAgentIds(),...Object.keys(perAgentMap)])];
      const paCount=Object.keys(perAgentMap).length;
      const paFold=make('details','tools-fold');
      if(paCount)paFold.open=true;
      paFold.append(make('summary','',paCount?'Особые скиллы по агентам ('+paCount+')':'Особые скиллы по агентам (опционально)'));
      const paInner=make('div');paInner.style.display='grid';paInner.style.gap='8px';paInner.style.marginTop='8px';
      paInner.append(make('div','tools-inline-note','Если у конкретного агента этот скилл называется иначе — выберите для него отдельное имя. Пустое значение использует общий скилл выше.'));
      for(const aid of agentIds){
        const curVal=typeof perAgentMap[aid]==='string'?perAgentMap[aid]:'';
        const rowFold=make('details','scn-mini-fold');
        rowFold.append(make('summary','',agentName(aid)+': '+(curVal?curVal:(p.skill.default?'как общий ('+p.skill.default+')':'не задан'))));
        const aPicker=renderSearchableSkillPicker(curVal,val=>{
          pushScenarioUndo(ed);
          if(!p.skill.per_agent||typeof p.skill.per_agent!=='object')p.skill.per_agent={};
          if(val)p.skill.per_agent[aid]=val;
          else{delete p.skill.per_agent[aid];if(!Object.keys(p.skill.per_agent).length)delete p.skill.per_agent;}
          markScenarioDirty(ed);renderScenarios();
        },aid);
        rowFold.append(aPicker);
        paInner.append(rowFold);
      }
      if(paCount){
        paInner.append(toolButton('Сбросить все per-agent переопределения',()=>{pushScenarioUndo(ed);delete p.skill.per_agent;markScenarioDirty(ed);renderScenarios();},'small'));
      }
      paFold.append(paInner);
      box.append(paFold);
      const rich=createRichPromptEditor(p.instruction||'',{placeholder:'Дополнительные указания к запуску скилла (необязательно)…'},val=>{p.instruction=val;});
      activePrompt=rich;
      const f=ruleField('Дополнительная инструкция к скиллу',rich.el);
      const ex=renderExampleButtons(rich,guide.examples);if(ex)f.append(ex);
      box.append(f);
    }else if(step.type==='command'){
      const rich=createRichPromptEditor(p.command||'',{mono:true,singleLine:true,requireShell:true,placeholder:'Например: pytest -q или npm test (одна строка)'},val=>{p.command=val;});
      activePrompt=rich;
      const f=ruleField('Консольная команда',rich.el,'Одна строка (без переносов). Все вставленные плашки автоматически экранируются через | shell.');
      const ex=renderExampleButtons(rich,guide.examples);if(ex)f.append(ex);
      box.append(f);
      const cwd=make('input');cwd.type='text';cwd.value=p.cwd||'';cwd.placeholder='По умолчанию — корень проекта';
      bindTextUndo(cwd);
      cwd.addEventListener('input',()=>{p.cwd=cwd.value;markScenarioDirty(ed);});
      box.append(ruleField('Папка запуска (необязательно)',cwd));
      const row=make('div','rule-field-row');
      const expErr=make('div','rule-field-hint');expErr.style.color='var(--danger)';
      const exp=make('input');exp.type='text';exp.value=p.expect_exit!==undefined?String(p.expect_exit):'';exp.placeholder='0 (успех)';
      bindBoundedInt(exp,expErr,'Код выхода',0,255,v=>{p.expect_exit=v;},()=>{delete p.expect_exit;});
      const tailErr=make('div','rule-field-hint');tailErr.style.color='var(--danger)';
      const tail=make('input');tail.type='text';tail.value=p.tail_lines!==undefined?String(p.tail_lines):'';tail.placeholder='200';
      bindBoundedInt(tail,tailErr,'Строки лога',0,500,v=>{p.tail_lines=v;},()=>{delete p.tail_lines;});
      const expF=ruleField('Ожидаемый код выхода',exp);expF.append(expErr);
      const tailF=ruleField('Сколько последних строк лога сохранить',tail);tailF.append(tailErr);
      row.append(expF,tailF);
      box.append(row);
    }else if(step.type==='gate'){
      const rich=createRichPromptEditor(p.question||'',{forbidSecret:true,placeholder:'Например: Принимаем изменения и переходим к финальному шагу?'},val=>{p.question=val;});
      activePrompt=rich;
      const f=ruleField('Текст вопроса пользователю',rich.el);
      const ex=renderExampleButtons(rich,guide.examples);if(ex)f.append(ex);
      box.append(f);
      // Interactive gate options builder (do NOT mutate p.options on read!)
      const opts=Array.isArray(p.options)?p.options:[];
      const optWrap=make('div','rule-field');
      optWrap.append(make('span','field-label','Варианты ответа (необязательно, 2–10 вариантов)'));
      const optList=make('div','scn-item-list');
      opts.forEach((optVal,idx)=>{
        const row=make('div','scn-item-row');row.style.gridTemplateColumns='minmax(0,1fr) auto';
        const inp=make('input');inp.type='text';inp.value=optVal;inp.placeholder='Вариант '+(idx+1);
        inp.addEventListener('input',()=>{p.options[idx]=inp.value;markScenarioDirty(ed);});
        const del=toolButton('×',()=>{pushScenarioUndo(ed);p.options.splice(idx,1);if(!p.options.length)delete p.options;markScenarioDirty(ed);renderScenarios();},'small danger');del.title='Удалить вариант';
        row.append(inp,del);optList.append(row);
      });
      const addOpt=toolButton('+ Добавить вариант ответа',()=>{
        pushScenarioUndo(ed);
        if(!Array.isArray(p.options))p.options=[];
        p.options.push(p.options.length?'Вариант '+(p.options.length+1):'Подтвердить');
        if(p.options.length===1)p.options.push('Доработать');
        markScenarioDirty(ed);renderScenarios();
      },'small');
      optWrap.append(optList,addOpt,make('span','rule-field-hint','Если вариантов нет — человек сможет ввести любой свободный ответ в чате.'));
      box.append(optWrap);
      const allowWrap=make('label','check-label'),cb=make('input');cb.type='checkbox';cb.checked=p.allow_comment!==false;
      cb.addEventListener('change',()=>{pushScenarioUndo(ed);p.allow_comment=cb.checked;markScenarioDirty(ed);});
      allowWrap.append(cb,document.createTextNode('Разрешить добавить комментарий к ответу'));box.append(allowWrap);
    }else if(step.type==='checklist'){
      const rawItems=Array.isArray(p.items)?p.items:[];
      const chkWrap=make('div','rule-field');
      chkWrap.append(make('span','field-label','Пункты чек-листа'));
      const itemList=make('div','scn-item-list');
      rawItems.forEach((rawItem,idx)=>{
        const textVal=typeof rawItem==='string'?rawItem:String(rawItem?.text||'');
        const reqVal=typeof rawItem==='string'?false:Boolean(rawItem?.required);
        const ensureObj=()=>{if(!Array.isArray(p.items))p.items=[];if(typeof p.items[idx]==='string')p.items[idx]={text:p.items[idx],required:false};return p.items[idx];};
        const row=make('div','scn-item-row');
        const inp=make('input');inp.type='text';inp.value=textVal;inp.placeholder='Пункт '+(idx+1)+': что нужно проверить…';
        inp.addEventListener('input',()=>{ensureObj().text=inp.value;markScenarioDirty(ed);});
        const reqLbl=make('label','check-label'),reqCb=make('input');reqCb.type='checkbox';reqCb.checked=reqVal;
        reqCb.addEventListener('change',()=>{pushScenarioUndo(ed);ensureObj().required=reqCb.checked;markScenarioDirty(ed);});
        reqLbl.append(reqCb,document.createTextNode('обяз.'));
        const acts=make('div','scn-item-actions');
        const up=toolButton('↑',()=>{if(idx>0){pushScenarioUndo(ed);const [m]=p.items.splice(idx,1);p.items.splice(idx-1,0,m);markScenarioDirty(ed);renderScenarios();}},'small');up.disabled=idx===0;up.title='Выше';
        const dn=toolButton('↓',()=>{if(idx<p.items.length-1){pushScenarioUndo(ed);const [m]=p.items.splice(idx,1);p.items.splice(idx+1,0,m);markScenarioDirty(ed);renderScenarios();}},'small');dn.disabled=idx===p.items.length-1;dn.title='Ниже';
        const del=toolButton('×',()=>{pushScenarioUndo(ed);p.items.splice(idx,1);markScenarioDirty(ed);renderScenarios();},'small danger');del.title='Удалить пункт';
        acts.append(up,dn,del);
        row.append(inp,reqLbl,acts);
        itemList.append(row);
      });
      if(!rawItems.length)itemList.append(make('div','tools-inline-note','Список пока пуст — нажмите кнопку ниже, чтобы добавить первый пункт.'));
      const addItem=toolButton('+ Добавить пункт',()=>{
        pushScenarioUndo(ed);
        if(!Array.isArray(p.items))p.items=[];
        p.items.push({text:'',required:true});markScenarioDirty(ed);renderScenarios();
        requestAnimationFrame(()=>{const inputs=$('scenarios-content')?.querySelectorAll('.scn-item-list input[type=text]');inputs?.[inputs.length-1]?.focus();});
      },'small primary');
      chkWrap.append(itemList,addItem);
      box.append(chkWrap);
    }else if(step.type==='memory.recall'){
      const q=make('input');q.type='text';q.value=p.query||'';q.placeholder='Например: архитектурные решения или прошлые баги';
      bindTextUndo(q);
      q.addEventListener('input',()=>{p.query=q.value;markScenarioDirty(ed);});
      const f=ruleField('Что искать в памяти Richi',q,'Напишите ключевые слова — Richi сам найдёт подходящие записи и передаст агенту.');
      f.append(buildLivePreview(q,{forbidSecret:true}));
      const ex=renderExampleButtons(q,guide.examples);if(ex)f.append(ex);
      box.append(f);
      const limErr=make('div','rule-field-hint');limErr.style.color='var(--danger)';
      const lim=make('input');lim.type='text';lim.value=p.limit!==undefined?String(p.limit):'10';
      bindBoundedInt(lim,limErr,'Максимум записей',1,50,v=>{p.limit=v;},()=>{delete p.limit;});
      const limF=ruleField('Максимум записей (1–50)',lim);limF.append(limErr);box.append(limF);
      const kindsWrap=make('div','rule-field');kindsWrap.append(make('span','field-label','Искать только среди типов (необязательно)'));
      const kg=make('div','scn-needs-grid');
      const kindNames={task:'Задачи',decision:'Решения',fact:'Факты',note:'Заметки',investigation:'Разборы',experiment:'Эксперименты',procedure:'Процедуры'};
      for(const k of SCN_ENTRY_KINDS){
        const lbl=make('label','scn-needs-item'),cb=make('input');cb.type='checkbox';cb.checked=list(p.kinds).includes(k);
        cb.addEventListener('change',()=>{
          pushScenarioUndo(ed);
          const cur=new Set(list(p.kinds));if(cb.checked)cur.add(k);else cur.delete(k);p.kinds=[...cur];markScenarioDirty(ed);
        });
        lbl.append(cb,document.createTextNode(kindNames[k]||k));kg.append(lbl);
      }
      kindsWrap.append(kg);box.append(kindsWrap);
    }else if(step.type==='memory.brief'){
      const q=make('input');q.type='text';q.value=p.question||'';q.placeholder='Например: Какие ограничения действуют в этом проекте?';
      bindTextUndo(q);
      q.addEventListener('input',()=>{p.question=q.value;markScenarioDirty(ed);});
      const f=ruleField('Вопрос к базе знаний Richi',q,'Richi автоматически соберёт сводку по этому вопросу перед работой агента.');
      f.append(buildLivePreview(q,{forbidSecret:true}));
      const ex=renderExampleButtons(q,guide.examples);if(ex)f.append(ex);
      box.append(f);
      const limErr=make('div','rule-field-hint');limErr.style.color='var(--danger)';
      const lim=make('input');lim.type='text';lim.value=p.limit!==undefined?String(p.limit):'10';
      bindBoundedInt(lim,limErr,'Максимум записей',1,50,v=>{p.limit=v;},()=>{delete p.limit;});
      const limF=ruleField('Максимум записей (1–50)',lim);limF.append(limErr);box.append(limF);
      const pastLbl=make('label','check-label'),pastCb=make('input');pastCb.type='checkbox';pastCb.checked=p.include_past_runs!==false;
      pastCb.addEventListener('change',()=>{pushScenarioUndo(ed);p.include_past_runs=pastCb.checked;markScenarioDirty(ed);});
      pastLbl.append(pastCb,document.createTextNode('Включать итоги последних 3 запусков этого сценария'));
      box.append(pastLbl);
      const learnLbl=make('label','check-label'),learnCb=make('input');learnCb.type='checkbox';learnCb.checked=Boolean(p.include_learnings);
      learnCb.addEventListener('change',()=>{pushScenarioUndo(ed);p.include_learnings=learnCb.checked;markScenarioDirty(ed);});
      learnLbl.append(learnCb,document.createTextNode('Подмешивать подтверждённые FP-паттерны проекта (include_learnings)'));
      box.append(learnLbl);
    }else if(step.type==='memory.record'){
      const kindSel=make('select');
      for(const k of SCN_ENTRY_KINDS)kindSel.add(new Option(kindLabel(k,true)+' ('+k+')',k));
      kindSel.value=SCN_ENTRY_KINDS.includes(p.kind)?p.kind:'decision';
      kindSel.addEventListener('change',()=>{pushScenarioUndo(ed);p.kind=kindSel.value;markScenarioDirty(ed);});
      const ksSel=make('select');
      ksSel.add(new Option('Подтверждено (confirmed)','confirmed'));
      ksSel.add(new Option('Гипотеза (hypothesis)','hypothesis'));
      ksSel.value=p.knowledge_state==='hypothesis'?'hypothesis':'confirmed';
      ksSel.addEventListener('change',()=>{pushScenarioUndo(ed);p.knowledge_state=ksSel.value;markScenarioDirty(ed);});
      const topRow=make('div','rule-field-row');
      topRow.append(ruleField('Вид записи',kindSel),ruleField('Статус знания',ksSel));
      box.append(topRow);
      const titleRich=createRichPromptEditor(p.title||'',{singleLine:true,forbidSecret:true,placeholder:'Заголовок записи, например: Итоги ревью {{run.id}}'},val=>{p.title=val;});
      box.append(ruleField('Заголовок записи в память',titleRich.el));
      const sumRich=createRichPromptEditor(p.summary||'',{forbidSecret:true,placeholder:'Что сохранить в память? Введите @ чтобы вставить итоги предыдущих шагов…'},val=>{p.summary=val;});
      activePrompt=sumRich;
      box.append(ruleField('Текст записи (что запомнить)',sumRich.el));
      const eidInp=make('input');eidInp.type='text';eidInp.value=p.entry_id||'';eidInp.placeholder='Автоматически (или существующий ID, чтобы дописать в задачу)';
      bindTextUndo(eidInp);
      eidInp.addEventListener('input',()=>{const v=eidInp.value.trim();if(v)p.entry_id=v;else delete p.entry_id;markScenarioDirty(ed);});
      box.append(ruleField('ID записи (необязательно)',eidInp));
      const tagsInp=make('input');tagsInp.type='text';tagsInp.value=Array.isArray(p.tags)?p.tags.join(', '):'';tagsInp.placeholder='perf, review, architecture';
      bindTextUndo(tagsInp);
      tagsInp.addEventListener('input',()=>{const arr=tagsInp.value.split(',').map(s=>s.trim()).filter(Boolean).slice(0,10);if(arr.length)p.tags=arr;else delete p.tags;markScenarioDirty(ed);});
      box.append(ruleField('Теги через запятую (необязательно)',tagsInp));
    }else if(step.type==='compare'){
      const ancIds=[...ancestors];
      const targetSel=make('select');
      targetSel.add(new Option('— выберите шаг-предок —',''));
      for(const aid of ancIds){
        const aStep=stepById.get(aid);
        if(aStep)targetSel.add(new Option('#'+stepNumById.get(aid)+' '+prettyStepLabel(aStep)+' ('+aid+')',aid));
      }
      if(p.step&&!ancIds.includes(p.step))targetSel.add(new Option(p.step+' (не подключён в needs!)',p.step));
      targetSel.value=p.step||'';
      targetSel.addEventListener('change',()=>{pushScenarioUndo(ed);p.step=targetSel.value;markScenarioDirty(ed);renderScenarios();});
      box.append(ruleField('Какой шаг сравнивать с прошлым запуском',targetSel,'Выберите шаг-предок, который возвращает числовые метрики или списки в data.'));
      const metricsInp=make('input');metricsInp.type='text';metricsInp.value=Array.isArray(p.metrics)?p.metrics.join(', '):'';metricsInp.placeholder='Например: p95_ms, error_rate, findings (пусто = все поля data)';
      bindTextUndo(metricsInp);
      metricsInp.addEventListener('input',()=>{const arr=metricsInp.value.split(',').map(s=>s.trim()).filter(Boolean).slice(0,20);if(arr.length)p.metrics=arr;else delete p.metrics;markScenarioDirty(ed);});
      box.append(ruleField('Ключи метрик в data (через запятую)',metricsInp));
      const baseInp=make('input');baseInp.type='text';baseInp.value=p.baseline_run||'';baseInp.placeholder='По умолчанию — закреплённый baseline или последний успешный запуск';
      bindTextUndo(baseInp);
      baseInp.addEventListener('input',()=>{const v=baseInp.value.trim();if(v)p.baseline_run=v;else delete p.baseline_run;markScenarioDirty(ed);});
      box.append(ruleField('Явный baseline run_id (необязательно)',baseInp));
    }else if(step.type==='condition'){
      box.append(renderConditionHelper(def,steps,step,p.expr||'',val=>{p.expr=val;markScenarioDirty(ed);}));
    }else if(!SCN_STEP_META[step.type]){
      toolWarning(box,'Неизвестный тип шага «'+step.type+'». Исходные параметры сохранены без изменений; выберите поддерживаемый тип или отредактируйте JSON.');
    }

    // Populate n8n-style Incoming Data pane (declarative toggle: prevents accidental duplicate spamming!)
    const directNeeds=list(step.needs).filter(id=>stepById.has(id));
    if(directNeeds.length){
      const inc=make('div','scn-incoming');
      inc.append(make('div','scn-incoming-head','Входящие данные от предыдущих шагов'));
      const toggleUpdaters=[];
      for(const nid of directNeeds){
        const src=stepById.get(nid),num=stepNumById.get(nid);
        const row=make('div','scn-incoming-row');
        const info=make('div','scn-node-head-left');
        const jumpBtn=make('button','scn-var-pill role-'+scenarioStepRole(src.type));jumpBtn.type='button';
        jumpBtn.textContent='#'+num+' '+cut(prettyStepLabel(src),24);
        jumpBtn.title='Перейти к шагу '+src.id;
        jumpBtn.addEventListener('click',()=>{highlightScenarioNode(src.id,false,step.id);scenariosState.selectedStepId=src.id;renderScenarios();});
        info.append(jumpBtn);
        row.append(info);
        if(activePrompt){
          const ref=stepPrimaryRef(src);
          const token='{{steps.'+src.id+'.'+ref.fld+(step.type==='command'?' | shell':'')+'}}';
          const toggleBtn=make('button','scn-example-btn scn-incoming-btn');toggleBtn.type='button';
          const syncBtn=()=>{
            const has=scenarioHasToken(activePrompt.getValue(),token);
            toggleBtn.classList.toggle('active',has);
            toggleBtn.textContent=has?'✓ В тексте · убрать':'+ Вставить '+ref.noun;
            toggleBtn.title=has?'Уже добавлено плашкой в текст — нажмите, чтобы убрать':'Вставить плашку «#'+num+' '+prettyStepLabel(src)+'» в текст задания';
          };
          toggleBtn.addEventListener('click',()=>{
            activePrompt.toggleToken(token);
            syncBtn();
          });
          syncBtn();toggleUpdaters.push(syncBtn);
          row.append(toggleBtn);
        }
        row.addEventListener('mouseenter',()=>highlightScenarioNode(src.id,true,step.id));
        row.addEventListener('mouseleave',()=>highlightScenarioNode(src.id,false,step.id));
        inc.append(row);
      }
      if(activePrompt&&toggleUpdaters.length){
        activePrompt.onInput(()=>toggleUpdaters.forEach(fn=>fn()));
      }
      incomingSlot.append(inc);
    }

    // Clear, clickable step dependencies (human titles + separate downstream list so nothing looks broken!)
    const needsWrap=make('div','rule-field');
    needsWrap.append(make('span','field-label','После каких шагов запускается этот шаг'));
    const others=steps.filter(s=>s.id!==step.id);
    const selectable=others.filter(other=>list(step.needs).includes(other.id)||!scenarioWouldCycle(steps,other.id,step.id));
    const downstream=others.filter(other=>!selectable.includes(other));
    const needsGrid=make('div','scn-needs-grid');
    if(!others.length){
      needsGrid.append(make('span','tools-inline-note','Пока в сценарии один шаг — добавьте ещё шаги, чтобы соединять их.'));
    }else if(!selectable.length){
      needsGrid.append(make('span','tools-inline-note','Это стартовый шаг — он запускается первым, а все остальные шаги уже идут после него.'));
    }else{
      for(const other of selectable){
        const checked=list(step.needs).includes(other.id);
        const lbl=make('label','scn-needs-item'),cb=make('input');
        cb.type='checkbox';cb.checked=checked;
        cb.addEventListener('change',()=>{
          pushScenarioUndo(ed);
          if(!Array.isArray(step.needs))step.needs=[];
          if(cb.checked){if(!step.needs.includes(other.id))step.needs.push(other.id);}
          else step.needs=step.needs.filter(n=>n!==other.id);
          markScenarioDirty(ed);renderScenarios();
        });
        lbl.append(cb,document.createTextNode(cut(prettyStepLabel(other),32)));
        needsGrid.append(lbl);
      }
    }
    needsWrap.append(needsGrid);
    if(downstream.length){
      const dsRow=make('div','scn-downstream');
      dsRow.append(make('span','','Дальше после этого шага идут:'));
      for(const d of downstream){
        dsRow.append(toolButton(cut(prettyStepLabel(d),28)+' →',()=>{scenariosState.selectedStepId=d.id;renderScenarios();},'scn-downstream-chip'));
      }
      needsWrap.append(dsRow);
    }
    needsWrap.append(make('span','rule-field-hint','Связи также можно тянуть мышкой на графе: от правого кружка шага к левому кружку следующего (клик по стрелке удаляет связь).'));
    box.append(needsWrap);

    // Advanced step settings (progressive disclosure: technical ID, if condition, retries, matrix, checks)
    const adv=make('details','tools-fold');adv.open=scenariosState.advOpen;adv.addEventListener('toggle',()=>{scenariosState.advOpen=adv.open;});adv.append(make('summary','','Расширенные настройки (условие пропуска, повторы, матрица, ID)'));
    const advBody=make('div');advBody.style.display='grid';advBody.style.gap='10px';advBody.style.marginTop='10px';
    advBody.append(make('div','rule-field-hint','Условие запуска (необязательно): если условие не выполнится, этот шаг автоматически пропустится.'));
    advBody.append(renderConditionHelper(def,steps,step,step.if||'',val=>{step.if=val;markScenarioDirty(ed);}));
    const jsonField=(label,hint,prop,placeholder)=>{
      const ta=make('textarea');ta.style.minHeight='64px';ta.style.fontFamily='ui-monospace,SFMono-Regular,monospace';ta.style.fontSize='11px';
      const fullKey=step.id+':'+prop;
      const savedBad=invalidFields.get(fullKey);
      ta.value=savedBad?savedBad.raw:(step[prop]?JSON.stringify(step[prop],null,2):'');ta.placeholder=placeholder;
      const err=make('div','rule-field-hint');err.style.color='var(--danger)';
      if(savedBad)err.textContent=savedBad.error;
      const validateJson=(recordError)=>{
        const raw=ta.value.trim();
        if(!raw){delete step[prop];err.textContent='';invalidFields.delete(fullKey);markScenarioDirty(ed);return;}
        try{step[prop]=JSON.parse(raw);err.textContent='';invalidFields.delete(fullKey);markScenarioDirty(ed);}
        catch(e){if(recordError){const msg='Ошибка JSON: '+e.message;err.textContent=msg;invalidFields.set(fullKey,{stepId:step.id,fieldKey:prop,label:prop,raw:ta.value,error:msg});}}
      };
      ta.addEventListener('change',()=>validateJson(true));
      ta.addEventListener('input',()=>{if(invalidFields.has(fullKey))validateJson(false);});
      const f=ruleField(label,ta,hint);f.append(err);advBody.append(f);
    };
    if(role==='agent'){
      const cofWrap=make('label','check-label'),cof=make('input');cof.type='checkbox';cof.checked=Boolean(step.continue_on_failure);
      cof.addEventListener('change',()=>{step.continue_on_failure=cof.checked;markScenarioDirty(ed);});
      cofWrap.append(cof,document.createTextNode('Не останавливать весь сценарий, если этот шаг упал с ошибкой'));
      advBody.append(cofWrap);
      const agSel=make('select');agSel.add(new Option('Любой активный агент',''));
      for(const aid of knownAgentIds())agSel.add(new Option(agentName(aid)+' ('+aid+')',aid));
      if(step.executor?.agent&&![...agSel.options].some(o=>o.value===step.executor.agent))agSel.add(new Option(step.executor.agent,step.executor.agent));
      agSel.value=step.executor?.agent||'';
      agSel.addEventListener('change',()=>{step.executor=agSel.value?{agent:agSel.value}:undefined;markScenarioDirty(ed);});
      advBody.append(ruleField('Закрепить конкретного агента',agSel,'Оставьте «Любой», чтобы шаг выполнял агент, в котором запущен чат.'));
      const retErr=make('div','rule-field-hint');retErr.style.color='var(--danger)';
      const ret=make('input');ret.type='text';ret.value=step.retry?.max?String(step.retry.max):'';ret.placeholder='0 (без повторов)';
      bindBoundedInt(ret,retErr,'Повторы',0,5,v=>{if(v>0)step.retry={max:v};else delete step.retry;},()=>{delete step.retry;});
      const retF=ruleField('Повторы при неудаче (0–5)',ret);retF.append(retErr);advBody.append(retF);
      jsonField('Матрица повторения шага (matrix)','Запускает копию этого шага для каждого значения из списка (например, параллельно по разным углам ревью).','matrix','{"angle": ["безопасность", "тесты"]}');
      jsonField('Ожидаемые выходные поля (outputs)','Структура данных, которую агент обязан вернуть в отчёте шага.','outputs','{"data": {"findings": {"type": "list", "required": true}}}');
      const quickChecks=make('div');quickChecks.style.display='grid';quickChecks.style.gap='6px';
      const toggleStepCheck=(kind,enabled,factory)=>{
        pushScenarioUndo(ed);
        invalidFields.delete(step.id+':checks');
        const cur=Array.isArray(step.checks)?step.checks.filter(c=>c&&c.kind!==kind):[];
        if(enabled)cur.push(factory());
        if(cur.length)step.checks=cur;else delete step.checks;
        markScenarioDirty(ed);renderScenarios();
      };
      const hasNoDirty=Array.isArray(step.checks)&&step.checks.some(c=>c&&c.kind==='no_dirty_git');
      const ndLbl=make('label','check-label'),ndCb=make('input');ndCb.type='checkbox';ndCb.checked=hasNoDirty;
      ndCb.addEventListener('change',()=>toggleStepCheck('no_dirty_git',ndCb.checked,()=>({kind:'no_dirty_git',mode:'unchanged'})));
      ndLbl.append(ndCb,document.createTextNode('Read-only guard (no_dirty_git): запретить изменение файлов репозитория на этом шаге'));
      const hasGrounded=Array.isArray(step.checks)&&step.checks.some(c=>c&&c.kind==='findings_grounded');
      const fgLbl=make('label','check-label'),fgCb=make('input');fgCb.type='checkbox';fgCb.checked=hasGrounded;
      fgCb.addEventListener('change',()=>toggleStepCheck('findings_grounded',fgCb.checked,()=>({kind:'findings_grounded',path:'findings'})));
      fgLbl.append(fgCb,document.createTextNode('Заземлить находки (findings_grounded): проверять реальный файл, строку и evidence'));
      quickChecks.append(ndLbl,fgLbl);
      advBody.append(quickChecks);
      jsonField('Автопроверки отчёта (checks)','Проверки, которые Richi проверит перед принятием отчёта шага (no_dirty_git, findings_grounded, file_exists, evidence_git).','checks','[{"kind": "findings_grounded", "path": "findings"}]');
    }else if(role==='human'){
      jsonField('Матрица (matrix)','Создаёт отдельный вопрос/чек-лист для каждого значения из списка.','matrix','{"item": ["a", "b"]}');
    }
    const idInput=make('input');idInput.type='text';idInput.value=step.id;idInput.spellcheck=false;idInput.autocomplete='off';
    const idError=make('div','rule-field-hint');idError.style.color='var(--danger)';
    idInput.addEventListener('change',()=>{
      const next=idInput.value.trim();
      if(next===step.id){idError.textContent='';return;}
      if(!SCN_STEP_ID_RE.test(next)||list(def.steps).some(s=>s.id===next)){idError.textContent='Нужны строчные латинские буквы, цифры, «_» или «-», и ID не должен повторяться.';idInput.value=step.id;return;}
      pushScenarioUndo(ed);
      renameStepId(def,step.id,next);
      markScenarioDirty(ed);renderScenarios();
    });
    const idField=ruleField('Технический ID шага',idInput,'Генерируется автоматически. Нужен, только если вы пишете формулы {{steps.<id>.*}} вручную.');idField.append(idError);
    advBody.append(idField);
    adv.append(advBody);box.append(adv);

    // Step footer actions
    const foot=make('div','rule-actions');
    foot.append(toolButton('Дублировать шаг',()=>{
      pushScenarioUndo(ed);
      const copy=scenarioClone(step);
      copy.id=uniqueStepId(def.steps,step.id);
      copy.title=(copy.title||step.id)+' (копия)';
      def.steps.push(copy);
      const pos=ensureDraftPositions(def)[step.id]||{x:SCN_PAD,y:SCN_PAD};
      def.layout.positions[copy.id]={x:pos.x+40,y:pos.y+SCN_NODE_H+24};
      scenariosState.selectedStepId=copy.id;markScenarioDirty(ed);renderScenarios();
    },'small'));
    foot.append(toolButton('Удалить шаг',()=>{
      if(def.steps.length<=1){scenariosState.saveError='В сценарии должен остаться хотя бы один шаг.';renderScenarios();return;}
      pushScenarioUndo(ed);
      pruneStepInvalidFields(ed,step.id);
      def.steps=def.steps.filter(s=>s.id!==step.id);
      for(const other of def.steps){
        if(Array.isArray(other.needs))other.needs=other.needs.filter(n=>n!==step.id);
        if(other.type==='compare'&&other.params&&other.params.step===step.id)other.params.step='';
      }
      if(def.layout?.positions)delete def.layout.positions[step.id];
      scenariosState.selectedStepId=def.steps[0]?.id||'';markScenarioDirty(ed);renderScenarios();
    },'small danger'));
    box.append(foot);
    return box;
  }
  function openScenarioJsonDialog(ed){
    const def=ed.draft;
    const panel=ruleDialog('{ } Код сценария (JSON)',def.id||'Черновик');
    const {dialog,body,foot}=panel;
    body.append(make('p','rule-field-hint','Техническое описание сценария в формате JSON — удобно для экспорта, передачи агенту в чат или массовой правки. В обычной работе на холсте открывать его не обязательно.'));
    const ta=make('textarea','scn-json-modal-ta');
    ta.spellcheck=false;ta.value=JSON.stringify(scenarioNormalizeDraft(def),null,2);
    const copyRow=make('div','rule-actions');
    const copyBtn=toolButton('Скопировать JSON',async()=>{
      try{await navigator.clipboard.writeText(ta.value);copyBtn.textContent='Скопировано ✓';setTimeout(()=>copyBtn.textContent='Скопировать JSON',1500);}
      catch{copyBtn.textContent='Выделите текст';}
    },'small');
    copyRow.append(copyBtn);
    const jErr=make('div','rule-field-hint');jErr.style.color='var(--danger)';
    body.append(copyRow,ta,jErr);
    foot.append(toolButton('Закрыть',()=>dialog.close()));
    foot.append(toolButton('Применить на холст',()=>{
      try{
        const parsed=JSON.parse(ta.value);
        if(!parsed||typeof parsed!=='object'||Array.isArray(parsed)||!Array.isArray(parsed.steps)||!parsed.steps.every(s=>s&&typeof s==='object'&&!Array.isArray(s))){
          throw new Error('Ожидается объект определения со списком объектов steps.');
        }
        const rawIds=new Set(parsed.steps.map(s=>String(s?.id||'').trim()).filter(Boolean));
        for(const rs of parsed.steps){
          if(Array.isArray(rs.needs)){
            for(const nd of rs.needs){
              const n=String(nd||'').trim();
              if(n&&!rawIds.has(n))throw new Error('Шаг «'+(rs.id||'?')+'» ссылается на несуществующий шаг «'+n+'» в needs.');
            }
          }
        }
        const normalizedDef=scenarioNormalizeDraft(parsed);
        if(ed.mode==='update')normalizedDef.id=ed.original.id;
        ensureDraftPositions(normalizedDef);
        pushScenarioUndo(ed);
        ed.draft=normalizedDef;
        if(ed.invalidFields)ed.invalidFields.clear();
        if(!list(ed.draft.steps).some(s=>s.id===scenariosState.selectedStepId))scenariosState.selectedStepId=ed.draft.steps[0]?.id||'';
        markScenarioDirty(ed);dialog.close();announce('JSON применён на холст');renderScenarios();
      }catch(e){jErr.textContent='Ошибка JSON: '+e.message;}
    },'scn-btn-primary'));
    dialog.showModal();
  }
  function renderScenarioEditor(root){
    const ed=scenariosState.editing,def=ed.draft;
    const wrap=make('div','scn-editor');
    const head=make('div','scn-editor-bar');
    const left=make('div','scn-editor-bar-left');
    left.append(toolButton('← Все сценарии',closeScenarioEditor,'scn-back-btn'));
    left.append(make('span','scn-editor-title',def.title||def.id||'Новый сценарий'));
    const statusPill=make('span','scn-status-pill '+(ed.dirty?'dirty':'clean'),ed.dirty?'● Не сохранено':(ed.mode==='update'?'✓ Сохранён'+(ed.original?.version?' · v'+ed.original.version:''):'Новый черновик'));
    left.append(statusPill);
    const right=make('div','scn-editor-bar-right');
    const undoBtn=toolButton('↶ Отменить',undoScenarioEdit,'small');
    undoBtn.dataset.scnUndo='1';undoBtn.disabled=!ed.undoStack.length;undoBtn.title='Отменить последнее изменение (Cmd/Ctrl+Z)';
    right.append(undoBtn);
    right.append(toolButton('✓ Проверить',()=>{
      const skillNames=rulesState.skills?list(rulesState.skills.skills).map(s=>s.name):null;
      scenariosState.lintResult=scenarioLintDraft(def,skillNames);
      announce(scenariosState.lintResult.ok?'Проверка пайплайна: ошибок не найдено':'Проверка пайплайна: найдены замечания ('+scenariosState.lintResult.issues.length+')');
      renderScenarios();
    },'small'));
    const previewBtn=toolButton('👁 Предпросмотр',()=>openScenarioPreviewDialog(ed),'small');
    previewBtn.title='Посмотреть итоговые промпты и выбранные скиллы глазами конкретного агента';
    right.append(previewBtn);
    if(ed.mode==='update'&&ed.original){
      const histBtn=toolButton('🕒 Версии',()=>openScenarioHistoryDialog(ed),'small');
      histBtn.title='История изменений сценария, diff по шагам и возврат к прошлой версии в черновик';
      right.append(histBtn);
    }
    const jsonBtn=toolButton('{ } Код JSON',()=>openScenarioJsonDialog(ed),'small');
    jsonBtn.title='Открыть исходный код сценария в JSON для экспорта или ручной правки';
    right.append(jsonBtn);
    if(ed.mode==='update'&&ed.original){
      const runNowBtn=toolButton('Запустить в чате',()=>openScenarioRunModal(ed.original),'small');
      runNowBtn.dataset.scnRun='1';runNowBtn.hidden=ed.dirty;
      right.append(runNowBtn);
    }
    const saveLabel=scenariosState.busy?'Сохраняем…':(rulesState.editing?(ed.mode==='add'?'Сохранить как новый':'Сохранить сценарий'):'Показать команду сохранения');
    const saveBtn=toolButton(saveLabel,saveScenarioDraft,'scn-btn-primary');
    saveBtn.disabled=scenariosState.busy;
    right.append(saveBtn);
    head.append(left,right);wrap.append(head);

    // Explicit Unsaved Changes Banner (updated live in-place without losing input focus!)
    const dirtyBar=make('div','scn-dirty-bar');dirtyBar.hidden=!ed.dirty;
    const dText=make('div','scn-dirty-bar-text');
    dText.append(make('span','scn-dirty-bar-dot'),make('span','','Есть несохранённые изменения — они пока только в этом окне и не записаны в базу.'));
    const dActs=make('div','scn-dirty-bar-actions');
    const dUndo=toolButton('↶ Отменить шаг',undoScenarioEdit,'small');dUndo.dataset.scnUndo='1';dUndo.disabled=!ed.undoStack.length;
    const dReset=toolButton('Сбросить до исходного',resetScenarioDraft,'small danger');dReset.dataset.scnReset='1';dReset.disabled=!ed.dirty;
    const dSave=toolButton(rulesState.editing?'Сохранить сценарий':'Показать команду сохранения',saveScenarioDraft,'small scn-btn-primary');dSave.disabled=scenariosState.busy;
    dActs.append(dUndo,dReset,dSave);
    dirtyBar.append(dText,dActs);wrap.append(dirtyBar);

    if(scenariosState.lintResult){
      const lr=scenariosState.lintResult;
      const errCount=lr.issues.filter(i=>i.severity==='err').length;
      const warnCount=lr.issues.filter(i=>i.severity==='warn').length;
      const sev=errCount?'err':(warnCount?'warn':'ok');
      const lBar=make('div','scn-lint-bar '+sev);
      const lTop=make('div','scn-run-top');
      lTop.append(make('strong','',sev==='ok'
        ?'✓ Пайплайн проверен — битых ссылок, циклов и ошибок параметров не найдено.'
        :'Проверка пайплайна: '+(errCount?errCount+' ошибок':'')+(errCount&&warnCount?' · ':'')+(warnCount?warnCount+' предупреждений':'')));
      lTop.append(toolButton('Скрыть',()=>{scenariosState.lintResult=null;renderScenarios();},'small'));
      lBar.append(lTop);
      if(lr.issues.length){
        const lList=make('div','scn-lint-list');
        const stepNumMap=new Map(list(def.steps).map((s,i)=>[s.id,i+1]));
        const stepObjMap=new Map(list(def.steps).map(s=>[s.id,s]));
        for(const issue of lr.issues){
          const item=make('button','scn-lint-item');item.type='button';
          item.append(make('span','scn-node-lint '+issue.severity,issue.severity==='err'?'✕':'!'));
          const prefix=issue.stepId&&stepObjMap.has(issue.stepId)?('#'+stepNumMap.get(issue.stepId)+' '+cut(prettyStepLabel(stepObjMap.get(issue.stepId)),24)+': '):'';
          item.append(make('span','',prefix+issue.message));
          if(issue.stepId){
            item.addEventListener('mouseenter',()=>highlightScenarioNode(issue.stepId,true));
            item.addEventListener('mouseleave',()=>highlightScenarioNode(issue.stepId,false));
            item.addEventListener('click',()=>{scenariosState.selectedStepId=issue.stepId;renderScenarios();});
          }
          lList.append(item);
        }
        lBar.append(lList);
      }
      wrap.append(lBar);
    }

    if(scenariosState.saveError){
      const err=make('div','rule-error',scenariosState.saveError);err.setAttribute('role','alert');wrap.append(err);
    }
    if(scenariosState.commandText){
      const showCmd=ruleCommandBox(wrap,scenarioCommandHint('Команда сохраняет сценарий'));
      showCmd(scenariosState.commandText);
    }

    // Metadata & inputs card
    const metaCard=make('div','scn-meta-card'),metaGrid=make('div','scn-meta-grid');
    const titleInp=make('input');titleInp.type='text';titleInp.value=def.title||'';titleInp.placeholder='Например: Глубокое ревью перед мержем';
    const idInp=make('input');idInp.type='text';idInp.value=def.id||'';idInp.disabled=ed.mode==='update';idInp.spellcheck=false;idInp.placeholder='deep-review-custom';
    titleInp.addEventListener('input',()=>{
      def.title=titleInp.value;markScenarioDirty(ed);
      const tEl=$('scenarios-content')?.querySelector('.scn-editor-title');
      if(tEl)tEl.textContent=def.title||def.id||'Новый сценарий';
      if(ed.mode==='add'&&!ed.idTouched&&typeof ruleSlug==='function'){
        const slug=ruleSlug(titleInp.value);
        if(slug){def.id=uniqueScenarioId(slug);idInp.value=def.id;}
      }
    });
    idInp.addEventListener('input',()=>{ed.idTouched=true;def.id=idInp.value.trim();markScenarioDirty(ed);});
    metaGrid.append(ruleField('Название сценария',titleInp));
    const scopeSel=make('select');scopeSel.disabled=ed.mode==='update';
    scopeSel.add(new Option('Весь workspace (глобальный)','global'));
    for(const [pid,pname] of scenarioProjectNames())scopeSel.add(new Option('Только проект · '+pname,'project:'+pid));
    scopeSel.value=ed.scope==='project'&&ed.project_id?'project:'+ed.project_id:'global';
    scopeSel.addEventListener('change',()=>{
      pushScenarioUndo(ed);
      if(scopeSel.value.startsWith('project:')){ed.scope='project';ed.project_id=scopeSel.value.slice(8);}
      else{ed.scope='global';ed.project_id='';}
      markScenarioDirty(ed);
    });
    metaGrid.append(ruleField('Где доступен',scopeSel));
    metaGrid.append(ruleField('ID для вызова в чате',idInp));
    metaCard.append(metaGrid);

    const metaFold=make('details','tools-fold');metaFold.open=scenariosState.metaOpen;metaFold.addEventListener('toggle',()=>{scenariosState.metaOpen=metaFold.open;});
    const inputCount=Object.keys(def.inputs||{}).length;
    metaFold.append(make('summary','','Описание, теги и дополнительные параметры запуска'+(inputCount?' ('+inputCount+')':'')));
    const foldBody=make('div');foldBody.style.display='grid';foldBody.style.gap='10px';foldBody.style.marginTop='10px';
    const descRow=make('div','scn-meta-grid');
    const descInp=make('input');descInp.type='text';descInp.value=def.description||'';descInp.placeholder='Зачем нужен этот сценарий';
    descInp.addEventListener('input',()=>{def.description=descInp.value;markScenarioDirty(ed);});
    const whenInp=make('input');whenInp.type='text';whenInp.value=def.when_to_use||'';whenInp.placeholder='Когда агент может предложить этот сценарий сам';
    whenInp.addEventListener('input',()=>{def.when_to_use=whenInp.value;markScenarioDirty(ed);});
    const runTitleInp=make('input');runTitleInp.type='text';runTitleInp.value=def.run_title||'';runTitleInp.placeholder='Например: Ревью ({{now.date}})';
    runTitleInp.addEventListener('input',()=>{def.run_title=runTitleInp.value;markScenarioDirty(ed);});
    const tagsInp=make('input');tagsInp.type='text';tagsInp.value=list(def.tags).join(', ');tagsInp.placeholder='review, security, deploy';
    tagsInp.addEventListener('input',()=>{def.tags=tagsInp.value.split(',').map(v=>v.trim()).filter(Boolean);markScenarioDirty(ed);});
    descRow.append(ruleField('Описание',descInp),ruleField('Когда предлагать агенту',whenInp),ruleField('Заголовок запуска в истории',runTitleInp),ruleField('Теги',tagsInp));
    foldBody.append(descRow);

    // Inputs editor
    const inputsBox=make('div','scn-inputs-list');
    const inpEntries=Object.entries(def.inputs||{});
    for(const [name,spec] of inpEntries){
      const row=make('div','scn-input-row');
      const nameIn=make('input');nameIn.type='text';nameIn.value=name;nameIn.placeholder='имя (a-z_)';nameIn.title='Имя параметра';
      nameIn.addEventListener('change',()=>{
        const next=nameIn.value.trim();if(!next||next===name||!SCN_INPUT_NAME_RE.test(next)||def.inputs[next]){nameIn.value=name;return;}
        const rebuilt={};for(const [k,v] of Object.entries(def.inputs))rebuilt[k===name?next:k]=v;def.inputs=rebuilt;markScenarioDirty(ed);renderScenarios();
      });
      const typeIn=make('select');SCN_INPUT_TYPES.forEach(t=>typeIn.add(new Option(t,t)));typeIn.value=spec.type||'string';
      typeIn.addEventListener('change',()=>{
        spec.type=typeIn.value;delete spec.default;
        if(spec.type==='select'||spec.type==='multiselect'){if(!list(spec.options).length)spec.options=['вариант1','вариант2'];}
        else delete spec.options;
        markScenarioDirty(ed);renderScenarios();
      });
      const titleIn=make('input');titleIn.type='text';titleIn.value=spec.title||'';titleIn.placeholder='Подпись поля';
      titleIn.addEventListener('input',()=>{spec.title=titleIn.value;markScenarioDirty(ed);});
      const extraIn=make('input');extraIn.type='text';
      if(spec.type==='select'||spec.type==='multiselect'){
        extraIn.value=list(spec.options).join(', ');extraIn.placeholder='Варианты через запятую';
        extraIn.addEventListener('input',()=>{
          const opts=[...new Set(extraIn.value.split(',').map(v=>v.trim()).filter(Boolean))];
          spec.options=opts;
          if(spec.type==='select'&&spec.default!==undefined&&!opts.includes(spec.default))delete spec.default;
          if(spec.type==='multiselect'&&Array.isArray(spec.default)){
            const kept=spec.default.filter(v=>opts.includes(v));
            if(kept.length)spec.default=kept;else delete spec.default;
          }
          markScenarioDirty(ed);
        });
      }else if(spec.type==='secret'){
        extraIn.value='';extraIn.disabled=true;extraIn.placeholder='Секрет без значения по умолчанию';
      }else{
        extraIn.value=spec.default!==undefined?String(spec.default):'';extraIn.placeholder='По умолчанию';
        extraIn.addEventListener('input',()=>{
          const v=extraIn.value;
          if(v.trim()===''){delete spec.default;}
          else if(spec.type==='boolean'){spec.default=v.trim().toLowerCase()==='true';}
          else if(spec.type==='number'){const n=Number(v.trim());if(Number.isFinite(n))spec.default=n;else delete spec.default;}
          else spec.default=v;
          markScenarioDirty(ed);
        });
      }
      const reqLbl=make('label','check-label'),reqCb=make('input');reqCb.type='checkbox';reqCb.checked=Boolean(spec.required);
      reqCb.addEventListener('change',()=>{spec.required=reqCb.checked;markScenarioDirty(ed);});
      reqLbl.append(reqCb,document.createTextNode('обяз.'));
      const delBtn=toolButton('×',()=>{delete def.inputs[name];markScenarioDirty(ed);renderScenarios();},'small danger');delBtn.title='Удалить параметр';
      row.append(nameIn,typeIn,titleIn,extraIn,reqLbl,delBtn);
      inputsBox.append(row);
    }
    foldBody.append(inputsBox);
    const addInpRow=make('div');
    addInpRow.append(toolButton('+ Добавить входной параметр',()=>{
      if(!def.inputs)def.inputs={};
      let n='param',i=1;while(def.inputs[n])n='param_'+(++i);
      def.inputs[n]={type:'string',title:'',required:false};markScenarioDirty(ed);scenariosState.metaOpen=true;renderScenarios();
    },'small'));
    foldBody.append(addInpRow);
    metaFold.append(foldBody);metaCard.append(metaFold);
    wrap.append(metaCard);

    // Main 2-column visual pipeline builder
    const builder=make('div','scn-builder');
    const stage=make('div','scn-stage');
    const stageHead=make('div','scn-stage-head'),sLeft=make('div','scn-stage-head-left'),sRight=make('div','scn-stage-head-right');
    const gBtn=toolButton('Граф (drag & wire)',()=>{scenariosState.viewMode='graph';renderScenarios();},'small'+(scenariosState.viewMode==='graph'?' primary':''));
    const lBtn=toolButton('Список шагов',()=>{scenariosState.viewMode='list';renderScenarios();},'small'+(scenariosState.viewMode==='list'?' primary':''));
    sLeft.append(gBtn,lBtn);
    const addStepSel=make('select');addStepSel.style.height='30px';addStepSel.style.fontSize='12px';addStepSel.style.borderRadius='8px';addStepSel.style.border='1px solid var(--line)';addStepSel.style.background='var(--panel)';addStepSel.style.color='var(--ink)';addStepSel.style.padding='0 8px';
    availableScenarioStepTypes().forEach(t=>addStepSel.add(new Option(t.label,t.id)));
    const addStepBtn=toolButton('+ Шаг',()=>{
      pushScenarioUndo(ed);
      const type=addStepSel.value||'instruction';
      const id=uniqueStepId(def.steps,type.replace('.','_'));
      const sel=def.steps.find(s=>s.id===scenariosState.selectedStepId)||def.steps[def.steps.length-1];
      const step={id,type,title:SCN_STEP_META[type]?.label||'Новый шаг',needs:sel?[sel.id]:[],params:scenarioDefaultParams(type)};
      def.steps.push(step);
      const posMap=ensureDraftPositions(def);
      const anchor=sel&&posMap[sel.id]?posMap[sel.id]:{x:SCN_PAD,y:SCN_PAD};
      posMap[id]={x:anchor.x+SCN_NODE_W+SCN_COL_GAP,y:anchor.y};
      scenariosState.selectedStepId=id;markScenarioDirty(ed);renderScenarios();
    },'small primary');
    const layoutBtn=toolButton('Авто-раскладка',()=>{
      pushScenarioUndo(ed);
      if(!def.layout)def.layout={};
      def.layout.positions=scenarioAutoLayout(def.steps);scenariosState.cameraFitNeeded=true;markScenarioDirty(ed);renderScenarios();
    },'small');
    sRight.append(addStepSel,addStepBtn,layoutBtn);
    stageHead.append(sLeft,sRight);stage.append(stageHead);
    if(scenariosState.viewMode==='graph')stage.append(renderScenarioCanvas(ed));
    else stage.append(renderScenarioListView(ed));
    const stageFoot=make('div','scn-stage-head');
    stageFoot.append(make('span','rule-field-hint',scenariosState.viewMode==='graph'
      ?'Колесо мыши — масштаб · перетащите фон — панорама · правый порт в пустое место — создать связанный шаг · клик по линии — удалить связь.'
      :'Здесь меняется только порядок отображения в списке. Реальный порядок выполнения задаётся связями «После каких шагов» (needs) на графе.'));
    stage.append(stageFoot);

    builder.append(stage,renderStepInspector(ed));
    wrap.append(builder);
    root.append(wrap);
  }
  function renderScenarioRuns(root){
    const schemaVersion=Number(scenariosState.data?.schema_version)||0;
    const capScenarios=scenariosState.data?.platform?.capabilities?.scenarios;
    if(capScenarios===false||(capScenarios===undefined&&schemaVersion<4)){
      toolWarning(root,'В этом workspace схема хранилища v'+schemaVersion+'. Определения и конструктор уже работают, а таблица запусков появится после миграции таблиц сценариев (команда richi migrate после резервной копии).');
      return;
    }
    const allRuns=list(scenariosState.data?.runs);
    const waitingQueue=list(scenariosState.data?.waiting_queue);
    if(waitingQueue.length){
      const qBox=make('div','scn-wait-queue');
      const qHead=make('div','scn-run-top');
      qHead.append(make('strong','','⏳ Ждёт вашего ответа ('+waitingQueue.length+')'),make('span','tools-inline-note','Единая очередь всех активных шлюзов во всех проектах'));
      qBox.append(qHead);
      const qGrid=make('div','scn-wait-list');
      for(const wq of waitingQueue.slice(0,8)){
        const item=make('div','scn-wait-item');
        const top=make('div','scn-run-top');
        top.append(make('strong','',wq.scenario_title+' · '+wq.title),make('code','',wq.run_id));
        item.append(top);
        if(wq.question)item.append(make('div','tools-inline-note',cut(wq.question,180)));
        const defaultAns=list(wq.options)[0]||(wq.type==='checklist'?'done':'ok');
        const phrase='Ответь на шаг '+wq.step_key+' запуска '+wq.run_id+': '+defaultAns;
        const acts=make('div','rule-actions');
        const cp=toolButton('Скопировать ответ («'+defaultAns+'»)',async()=>{
          try{await navigator.clipboard.writeText(phrase);cp.textContent='Скопировано ✓';setTimeout(()=>cp.textContent='Скопировать ответ («'+defaultAns+'»)',1400);}catch{}
        },'small scn-btn-primary');
        const openRun=toolButton('Открыть запуск',()=>{
          scenariosState.runFilter='all';
          if(scenariosState.scope){scenariosState.scope='';const pf=$('project-filter');if(pf&&state.mode==='scenarios')pf.value='';}
          if(scenariosState.query){scenariosState.query='';const sr=$('search');if(sr&&state.mode==='scenarios')sr.value='';}
          loadScenarioRun(wq.run_id);
        },'small');
        acts.append(cp,openRun);
        item.append(acts);
        qGrid.append(item);
      }
      qBox.append(qGrid);
      root.append(qBox);
    }
    const stats=scenariosState.data?.stats;
    if(stats&&stats.total_runs>0){
      const strip=make('div','scn-stats-strip');
      const kpi=(val,lbl)=>{const c=make('div','scn-stat-card');c.append(make('strong','',val),make('span','',lbl));return c;};
      strip.append(
        kpi(String(stats.total_runs),'Всего запусков'),
        kpi(stats.completion_rate!==null?Math.round(stats.completion_rate*100)+'%':'—','Доведены до конца'),
        kpi(stats.first_pass_rate!==null?Math.round(stats.first_pass_rate*100)+'%':'—','С 1-го раза без отклонений'),
        kpi(String(stats.by_status?.failed||0),'С ошибкой')
      );
      const worst=list(stats.per_step).find(s=>(s.failed||0)+(s.rejections||0)>0);
      if(worst)strip.append(kpi(worst.step_id,'Узкое место ('+worst.failed+' ош. / '+worst.rejections+' откл.)'));
      root.append(strip);
    }
    const isBlockedOnlyRun=run=>run.status==='waiting'&&(run.step_counts?.blocked||0)>0&&!(run.step_counts?.waiting_human||0);
    const runDisplayStatus=run=>isBlockedOnlyRun(run)?'blocked':run.status;
    const runDisplayLabel=run=>isBlockedOnlyRun(run)?'Заблокирован (ошибка)':(SCN_STATUS_LABEL[run.status]||run.status);
    const filters=[['all','Все'],['active','Активные'],['waiting','Ждут ответа'],['done','Завершённые'],['failed','Ошибка / блок / отменены']];
    const bar=make('div','scn-subtabs');
    for(const [key,label] of filters){
      const btn=toolButton(label,()=>{scenariosState.runFilter=key;renderScenarios();},'scn-subtab');
      btn.setAttribute('aria-pressed',String(scenariosState.runFilter===key));bar.append(btn);
    }
    root.append(bar);
    const q=normalized(scenariosState.query);
    const filtered=allRuns.filter(run=>{
      if(scenariosState.scope==='global'&&run.scope!=='global')return false;
      if(scenariosState.scope==='builtin'&&run.scope!=='builtin')return false;
      if(scenariosState.scope.startsWith('project:')&&run.project_id!==scenariosState.scope.slice(8))return false;
      if(scenariosState.runFilter==='active'&&!['running','waiting','paused'].includes(run.status))return false;
      if(scenariosState.runFilter==='waiting'&&(!['running','waiting'].includes(run.status)||!(run.step_counts?.waiting_human||0)))return false;
      if(scenariosState.runFilter==='done'&&run.status!=='done')return false;
      if(scenariosState.runFilter==='failed'&&!['failed','cancelled'].includes(run.status)&&!isBlockedOnlyRun(run))return false;
      if(!q)return true;
      return [run.id,run.scenario_id,run.status,run.project_id,run.created_by_agent,run.task_entry_id].map(normalized).join(' ').includes(q);
    });
    if(!filtered.length){
      const empty=make('div','tools-empty');
      empty.append(ruleMark('rule'),make('h3','',allRuns.length?'Подходящих запусков нет':'Запущенных сценариев пока нет'),make('p','',allRuns.length?'Измените фильтр статуса или поиск.':'Запустите сценарий из чата агента командой richi scenario run <id> — он появится здесь и как задача «Запущенные сценарии» на карте.'));
      root.append(empty);return;
    }
    const inWaiting=waitingQueue.some(w=>w.run_id===scenariosState.selectedRunId);
    if(!filtered.some(r=>r.id===scenariosState.selectedRunId)&&!(inWaiting&&(scenariosState.runDetailLoading||scenariosState.runDetailError||scenariosState.runDetail?.run?.run_id===scenariosState.selectedRunId))){
      scenariosState.selectedRunId=filtered[0].id;
      const targetId=scenariosState.selectedRunId;
      if(scenariosState.runDetail?.run?.run_id!==targetId&&scenariosState.runDetailLoadingId!==targetId){
        queueMicrotask(()=>{if(scenariosState.selectedRunId===targetId&&scenariosState.runDetail?.run?.run_id!==targetId&&scenariosState.runDetailLoadingId!==targetId)loadScenarioRun(targetId);});
      }
    }
    const layout=make('div','scn-runs-layout'),col=make('div','scn-runs-list');
    for(const run of filtered){
      const btn=toolButton('',()=>{loadScenarioRun(run.id);},'scn-run-card');
      btn.setAttribute('aria-pressed',String(run.id===scenariosState.selectedRunId));
      const top=make('div','scn-run-top');
      const runTitleWrap=make('span');
      runTitleWrap.append(make('strong','',run.scenario_id+' · v'+run.scenario_version+(run.is_baseline?' ★':'')));
      if(run.run_kind&&run.run_kind!=='prod')runTitleWrap.append(document.createTextNode(' '),make('span','scn-pill-badge '+(run.run_kind==='eval'?'tpl':'count'),run.run_kind));
      top.append(runTitleWrap,make('span','scn-status st-'+runDisplayStatus(run),runDisplayLabel(run)));
      const counts=run.step_counts||{},total=counts.total||0,done=(counts.done||0)+(counts.skipped||0);
      const prog=make('div','scn-progress'),fill=make('i');fill.style.width=(total?Math.round(done*100/total):0)+'%';prog.append(fill);
      const meta=make('div','rule-meta');
      meta.append(make('code','',run.id),make('span','',done+'/'+total+' шагов'));
      if(counts.blocked)meta.append(make('span','','блок: '+counts.blocked));
      if(counts.failed)meta.append(make('span','','ошибок: '+counts.failed));
      if(run.updated_at)meta.append(make('span','',dateText(run.updated_at)));
      btn.append(top,prog,meta);col.append(btn);
    }
    if(scenariosState.data?.runs_more){
      col.append(make('div','tools-inline-note','Показаны последние '+allRuns.length+' из '+(scenariosState.data.runs_total||allRuns.length)+' запусков. До 200 последних запусков доступно через richi scenario runs --limit 200 (используйте фильтры --active, --scenario, --project).'));
    }
    const detail=make('div','scn-run-detail');
    if(scenariosState.runDetailLoading&&scenariosState.runDetail?.run?.run_id!==scenariosState.selectedRunId){
      detail.append(make('p','tools-inline-note','Загружаем шаги и журнал запуска…'));
    }else if(scenariosState.runDetailError){
      detail.append(make('div','rule-error',scenariosState.runDetailError),toolButton('Повторить',()=>loadScenarioRun(scenariosState.selectedRunId),'small'));
    }else if(scenariosState.runDetail?.run?.run_id===scenariosState.selectedRunId){
      const r=scenariosState.runDetail.run;
      const waitingSteps=list(r.steps).filter(s=>s.status==='waiting_human');
      const blockedSteps=list(r.steps).filter(s=>s.status==='blocked');
      const detailBlockedOnly=r.status==='waiting'&&blockedSteps.length>0&&!waitingSteps.length;
      const head=make('div','scn-run-top');
      const titles=make('div');
      titles.append(make('div','tools-eyebrow','Запуск сценария · '+r.scenario_id+' v'+r.scenario_version),make('h3','',r.scenario_title||r.scenario_id));
      head.append(titles,make('span','scn-status st-'+(detailBlockedOnly?'blocked':r.status),detailBlockedOnly?'Заблокирован (ошибка)':(SCN_STATUS_LABEL[r.status]||r.status)));
      detail.append(head);

      const meta=make('div','rule-meta');
      const scopeLabel=r.scope==='builtin'?('Встроенный шаблон'+(r.project_id?' · проект: '+r.project_id:'')):(r.scope==='project'?'Проект: '+r.project_id:'Глобальный');
      meta.append(make('code','',r.run_id),make('span','','rev '+r.revision),make('span','',scopeLabel));
      if(r.run_kind&&r.run_kind!=='prod')meta.append(make('span','scn-pill-badge '+(r.run_kind==='eval'?'tpl':'count'),r.run_kind));
      if(r.is_baseline)meta.append(make('span','scn-node-badge','★ Эталон (baseline)'));
      if(r.created_by_agent)meta.append(make('span','','Агент: '+r.created_by_agent));
      if(r.updated_at)meta.append(make('span','','Снимок от '+dateText(r.updated_at)));
      detail.append(meta);

      const actRow=make('div','rule-actions');
      const jumpNote=make('div','tools-inline-note');jumpNote.hidden=true;
      if(r.task_entry_id){
        const nodeId='entry:'+r.task_entry_id;
        const jumpBtn=toolButton('Открыть задачу на карте',async()=>{
          if(state.nodeMap.has(nodeId)){focusNode(nodeId);return;}
          let changed=false;
          if(!$('hypotheses').checked){$('hypotheses').checked=true;changed=true;}
          if(r.status==='cancelled'&&!$('superseded').checked){$('superseded').checked=true;changed=true;}
          if(changed){await loadGraph();}
          if(state.nodeMap.has(nodeId)){focusNode(nodeId);}
          else{scenariosState.outcome='Задача «'+r.task_entry_id+'» не найдена в текущем снимке карты.';renderScenarios();}
        },'small primary');
        actRow.append(jumpBtn);
      }
      actRow.append(toolButton('↻ Обновить снимок',()=>loadScenarioRun(r.run_id),'small'));
      detail.append(actRow,jumpNote);

      const inputPairs=r.inputs&&typeof r.inputs==='object'?Object.entries(r.inputs):[];
      if(inputPairs.length){
        const inpBox=make('div','rule-meta');
        inpBox.append(make('strong','','Параметры:'));
        for(const [k,v] of inputPairs)inpBox.append(make('code','',k+'='+(typeof v==='object'?JSON.stringify(v):String(v))));
        detail.append(inpBox);
      }

      // Waiting gates (strictly read-only in UI: answered in chat via phrase or in terminal via CLI)
      for(const ws of waitingSteps){
        const gateBox=make('div','tools-warning');
        gateBox.append(make('strong','','Ожидает ответа человека: '+(ws.title||ws.key)+' ('+ws.key+')'));
        if(ws.gate?.question)gateBox.append(make('p','',ws.gate.question));
        if(list(ws.checklist?.items).length)gateBox.append(make('div','tools-inline-note','Чек-лист: '+ws.checklist.items.map(it=>typeof it==='string'?it:(it?.text||'')).filter(Boolean).join(' · ')));
        const opts=list(ws.gate?.options);
        let chosenVal=opts[0]||(ws.type==='checklist'?'done':'ok'),commentVal='';
        if(opts.length){
          const optRow=make('div','scn-chips');
          opts.forEach(opt=>{
            const b=toolButton(opt,()=>{chosenVal=opt;optRow.querySelectorAll('button').forEach(x=>x.classList.toggle('primary',x===b));syncAnswerBoxes();},'small'+(opt===chosenVal?' primary':''));
            optRow.append(b);
          });
          gateBox.append(optRow);
        }
        const chatPre=make('pre','');
        const copyPhrase=toolButton('Скопировать фразу для чата',async()=>{
          try{await navigator.clipboard.writeText(chatPre.textContent);copyPhrase.textContent='Скопировано ✓';setTimeout(()=>copyPhrase.textContent='Скопировать фразу для чата',1500);}
          catch{copyPhrase.textContent='Выделите текст';}
        },'small scn-btn-primary');
        const chatWrap=make('div','scn-chat-prompt');
        chatWrap.append(make('div','field-label','Ответьте агенту в чате:'),chatPre,copyPhrase);
        gateBox.append(chatWrap);
        const cliFold=make('details','tools-fold');cliFold.append(make('summary','','Команда для терминала (CLI)'));
        const cliInner=make('div');cliInner.style.marginTop='8px';
        const showAnswerCmd=ruleCommandBox(cliInner,scenarioCommandHint('Команда записывает ответ на шаг'));
        cliFold.append(cliInner);gateBox.append(cliFold);
        const syncAnswerBoxes=()=>{
          chatPre.textContent='Ответь на шаг '+ws.key+' запуска '+r.run_id+': '+chosenVal+(commentVal?' ('+commentVal+')':'');
          showAnswerCmd(scenarioAnswerCommand(r.run_id,ws.key,chosenVal,commentVal,state.mapStore));
        };
        syncAnswerBoxes();
        detail.append(gateBox);
      }

      // Live 2D Execution Graph Overlay (status-colored nodes + click-to-scroll to step detail)
      const renderRunBlueprintGraph=(runObj,container)=>{
        const defSteps=list(runObj.definition_steps).length
          ?list(runObj.definition_steps)
          :[...new Map(list(runObj.steps).map(s=>[s.id,{id:s.id,title:s.title||s.id,type:s.type,needs:[]}])).values()];
        if(!defSteps.length)return null;
        const autoPos=scenarioAutoLayout(defSteps),savedPos=runObj.layout?.positions||{};
        const pos={};
        for(const ds of defSteps){
          const sp=savedPos[ds.id];
          pos[ds.id]=sp&&Number.isFinite(Number(sp.x))&&Number.isFinite(Number(sp.y))
            ?{x:Math.round(Number(sp.x)),y:Math.round(Number(sp.y))}
            :(autoPos[ds.id]||{x:SCN_PAD,y:SCN_PAD});
        }
        const byStepId=new Map();
        for(const rs of list(runObj.steps)){
          if(!byStepId.has(rs.id))byStepId.set(rs.id,[]);
          byStepId.get(rs.id).push(rs);
        }
        const rank={failed:6,blocked:5,waiting_human:4,claimed:3,running:3,ready:2,done:1,skipped:0,pending:-1};
        const aggStatus=rows=>{
          if(!rows.length)return 'pending';
          return rows.reduce((best,cur)=>(rank[cur.status]??-1)>(rank[best]??-1)?cur.status:best,rows[0].status);
        };
        const graphBox=make('div','scn-run-graph');
        let maxX=520,maxY=260;
        for(const ds of defSteps){
          const p=pos[ds.id];
          if(p.x+240>maxX)maxX=p.x+240;
          if(p.y+110>maxY)maxY=p.y+110;
        }
        const surface=make('div');surface.style.position='relative';surface.style.width=maxX+'px';surface.style.height=maxY+'px';
        const svgEl=makeSvg('svg',{class:'scn-edges-svg',width:String(maxX),height:String(maxY)});
        svgEl.style.color='var(--edge)';
        const defsEl=makeSvg('defs'),mk=makeSvg('marker',{id:'scn-run-arrow',viewBox:'0 0 10 10',refX:'9',refY:'5',markerWidth:'6',markerHeight:'6',orient:'auto'});
        mk.append(makeSvg('path',{d:'M 0 1.5 L 9 5 L 0 8.5 z',fill:'currentColor'}));
        defsEl.append(mk);svgEl.append(defsEl);
        for(const ds of defSteps){
          const b=pos[ds.id];
          for(const dep of list(ds.needs)){
            const a=pos[dep];if(!a)continue;
            const d=scenarioEdgePath(a.x+210,a.y+36,b.x,b.y+36);
            svgEl.append(makeSvg('path',{class:'scn-edge-path',d,'marker-end':'url(#scn-run-arrow)'}));
          }
        }
        surface.append(svgEl);
        defSteps.forEach((ds,idx)=>{
          const p=pos[ds.id],rows=byStepId.get(ds.id)||[],st=aggStatus(rows);
          const node=make('div','scn-run-node st-'+st);
          node.style.left=p.x+'px';node.style.top=p.y+'px';
          const top=make('div','scn-run-top');
          const numWrap=make('span','scn-node-head-left');
          numWrap.append(make('span','scn-node-num','#'+(idx+1)));
          const stText=rows.length>1?rows.filter(x=>x.status==='done'||x.status==='skipped').length+'/'+rows.length:(SCN_STATUS_LABEL[st]||st);
          top.append(numWrap,make('span','scn-status st-'+st,stText));
          node.append(top,make('div','scn-node-title',cut(prettyStepLabel(ds),34)));
          node.addEventListener('click',()=>{
            const target=container.querySelector('.scn-step-row[data-step-id="'+ds.id+'"]');
            if(target){target.scrollIntoView({behavior:'smooth',block:'nearest'});target.style.outline='2px solid var(--accent)';setTimeout(()=>{target.style.outline='';},1200);}
          });
          surface.append(node);
        });
        graphBox.append(surface);
        return graphBox;
      };
      const renderRunTimeline=runObj=>{
        const stepsList=list(runObj.steps);
        const times=[];
        for(const s of stepsList){
          const a=s.started_at?Date.parse(s.started_at):NaN;
          const b=s.finished_at?Date.parse(s.finished_at):(s.status==='claimed'||s.status==='waiting_human'?Date.now():NaN);
          if(Number.isFinite(a))times.push(a);
          if(Number.isFinite(b))times.push(b);
        }
        const minT=times.length?Math.min(...times):0;
        const maxT=times.length?Math.max(...times):0;
        const span=Math.max(1000,maxT-minT);
        const fmtDur=ms=>{
          if(!Number.isFinite(ms)||ms<0)return '—';
          if(ms<1000)return Math.round(ms)+' мс';
          const sec=Math.round(ms/1000);
          return sec<60?(sec+' с'):(Math.floor(sec/60)+' м '+(sec%60)+' с');
        };
        const box=make('div','scn-timeline');
        for(const s of stepsList){
          const row=make('div','scn-tl-row');
          const lbl=make('div','scn-tl-label');
          lbl.append(make('span','',cut(s.title||s.key,28)),make('code','',s.key));
          const track=make('div','scn-tl-track');
          const a=s.started_at?Date.parse(s.started_at):NaN;
          const b=s.finished_at?Date.parse(s.finished_at):(Number.isFinite(a)&&['claimed','waiting_human'].includes(s.status)?Date.now():a);
          let durTxt='—';
          if(Number.isFinite(a)&&Number.isFinite(b)&&times.length){
            const leftPct=Math.max(0,Math.min(96,((a-minT)/span)*100));
            const widthPct=Math.max(3,Math.min(100-leftPct,((Math.max(b-a,200))/span)*100));
            const bar=make('div','scn-tl-bar st-'+s.status);
            bar.style.left=leftPct+'%';bar.style.width=widthPct+'%';
            track.append(bar);
            durTxt=fmtDur(b-a);
          }
          row.append(lbl,track,make('span','scn-tl-dur',durTxt));
          box.append(row);
        }
        return box;
      };
      const runGraph=renderRunBlueprintGraph(r,detail);
      if(runGraph||list(r.steps).length){
        const visMode=scenariosState.runVisualMode||'graph';
        const visHead=make('div','scn-run-top');
        visHead.append(make('strong','',visMode==='timeline'?'Таймлайн выполнения':'Карта выполнения'));
        const toggle=make('div','scn-subtabs');toggle.style.margin='0';
        for(const [k,lbl] of [['graph','Граф'],['timeline','Таймлайн (водопад)']]){
          const b=toolButton(lbl,()=>{scenariosState.runVisualMode=k;renderScenarios();},'scn-subtab');
          b.setAttribute('aria-pressed',String(visMode===k));toggle.append(b);
        }
        visHead.append(toggle);
        detail.append(visHead);
        detail.append(visMode==='timeline'?renderRunTimeline(r):(runGraph||renderRunTimeline(r)));
      }

      toolSection(detail,'Шаги выполнения',list(r.steps).length);
      for(const step of list(r.steps)){
        const row=make('div','scn-step-row st-'+step.status);
        row.dataset.stepId=step.id;
        const sTop=make('div','scn-run-top');
        const matrixTag=step.matrix&&typeof step.matrix==='object'&&Object.keys(step.matrix).length
          ?' ['+Object.entries(step.matrix).map(([k,v])=>k+'='+String(v)).join(', ')+']':'';
        sTop.append(make('strong','',(step.title||step.id)+matrixTag+' '),make('span','scn-status st-'+step.status,SCN_STATUS_LABEL[step.status]||step.status));
        const sMeta=make('div','rule-meta');
        sMeta.append(make('code','',step.key),make('span','',SCN_STEP_META[step.type]?.label||step.type),make('span','',SCN_ROLE_LABEL[step.executor?.role]||step.executor?.role||''));
        if(step.attempts)sMeta.append(make('span','','попыток: '+step.attempts));
        if(step.rejections)sMeta.append(make('span','','отклонений отчёта: '+step.rejections));
        if(step.claimed_by_chat)sMeta.append(make('span','','чат: '+cut(step.claimed_by_chat,36)));
        row.append(sTop,sMeta);
        if(step.summary)row.append(make('div','',step.summary));
        if(list(step.artifacts).length){
          const artRow=make('div','rule-meta');
          artRow.append(make('span','tools-inline-note','Артефакты:'));
          for(const art of list(step.artifacts)){
            const uri=typeof art==='string'?art:(art&&art.uri?String(art.uri):'');
            if(!uri)continue;
            const lbl=typeof art==='object'&&art&&art.title?(String(art.title)+' ('+uri+')'):uri;
            artRow.append(make('code','',lbl));
          }
          row.append(artRow);
        }
        if(step.answer!==undefined&&String(step.answer)!==String(step.summary||''))row.append(make('div','tools-inline-note','Ответ: '+String(step.answer)));
        if(step.reason)row.append(make('div','tools-inline-note','Причина: '+step.reason));
        detail.append(row);
      }

      const runEvents=list(scenariosState.runDetail.log);
      if(runEvents.length){
        const evFold=make('details','tools-fold');evFold.append(make('summary','','События запуска ('+runEvents.length+(scenariosState.runDetail.log_more?'+':'')+')'));
        const evList=make('div','scn-log-list');evList.style.marginTop='10px';
        for(const ev of runEvents)evList.append(renderScenarioLogRow(ev));
        if(scenariosState.runDetail.log_more)evList.append(make('div','tools-inline-note','Показаны последние '+runEvents.length+' событий запуска. До 200 событий доступно через richi scenario log --run '+r.run_id+' --entries 200.'));
        evFold.append(evList);detail.append(evFold);
      }
    }
    layout.append(col,detail);root.append(layout);
  }
  function renderScenarioLogRow(ev){
    const row=make('div','scn-log-row'),head=make('div','scn-log-head');
    const left=make('div','rule-meta');
    left.append(make('strong','',SCN_ACTION_LABEL[ev.action]||ev.action||'событие'));
    if(ev.scenario_id)left.append(make('code','',ev.scenario_id));
    if(ev.run_id)left.append(make('code','',ev.run_id));
    if(ev.step_key)left.append(make('code','',ev.step_key));
    head.append(left,make('span','tools-inline-note',ev.at?dateText(ev.at):''));
    row.append(head);
    const meta=[];
    if(ev.by)meta.push('кто: '+ev.by);
    if(ev.agent)meta.push('агент: '+ev.agent);
    if(meta.length)row.append(make('div','tools-inline-note',meta.join(' · ')));
    if(ev.detail&&typeof ev.detail==='object'&&Object.keys(ev.detail).length){
      const fold=make('details');fold.append(make('summary','','Детали'),make('pre','',JSON.stringify(ev.detail,null,2)));
      row.append(fold);
    }
    return row;
  }
  function renderScenarioLog(root){
    const entries=list(scenariosState.data?.log);
    const bar=make('div','scn-subtabs');
    for(const [key,label] of [['all','Все события'],['defs','Определения'],['runs','Запуски']]){
      const btn=toolButton(label,()=>{scenariosState.logFilter=key;renderScenarios();},'scn-subtab');
      btn.setAttribute('aria-pressed',String(scenariosState.logFilter===key));bar.append(btn);
    }
    root.append(bar);
    const q=normalized(scenariosState.query);
    const shown=entries.filter(ev=>{
      const isRun=Boolean(ev.run_id);
      if(scenariosState.logFilter==='defs'&&isRun)return false;
      if(scenariosState.logFilter==='runs'&&!isRun)return false;
      if(!q)return true;
      return [ev.action,ev.scenario_id,ev.run_id,ev.step_key,ev.by,ev.agent,JSON.stringify(ev.detail||{})].map(normalized).join(' ').includes(q);
    });
    if(!shown.length){
      const empty=make('div','tools-empty');
      empty.append(ruleMark('rule'),make('h3','',entries.length?'Подходящих событий нет':'Журнал сценариев пока пуст'),make('p','','Здесь будут отображаться создания и изменения сценариев, а также все события запусков.'));
      root.append(empty);return;
    }
    const listBox=make('div','scn-log-list');
    shown.forEach(ev=>listBox.append(renderScenarioLogRow(ev)));
    root.append(listBox);
    if(scenariosState.data?.log_more)root.append(make('p','tools-inline-note','Показаны последние '+entries.length+' событий. До 200 последних событий доступно через richi scenario log --entries 200.'));
  }
  function openRememberFpDialog(finding){
    const panel=ruleDialog('Запомнить FP-паттерн в памяти Richi',(finding.file||'')+(finding.line?':'+finding.line:'')+' · '+finding.finding_id);
    const {dialog,body,foot}=panel;
    body.append(make('p','tools-inline-note','Карта не записывает правила напрямую в базу знаний (entries). Задайте область файлов и условие безопасности — Richi соберёт точную CLI-команду для сохранения подтверждённого факта.'));
    const scopeIn=make('input');scopeIn.type='text';scopeIn.value=finding.file||'';scopeIn.placeholder='src/auth.py';
    const condIn=make('textarea');condIn.rows=3;condIn.placeholder='Например: Gateway проверяет HMAC-подпись до вызова verify_token';
    const outPre=make('pre','rule-command');
    const buildCmd=()=>{
      const sp=scopeIn.value.trim()||(finding.file||'src');
      const cd=condIn.value.trim()||'<условие безопасности (минимум 10 символов)>';
      const base=['richi',...list(state.mapStore).map(scnShellQuote),'scenario','feedback',scnShellQuote(finding.run_id),
        '--finding='+scnShellQuote(finding.finding_id),'--verdict','false_positive','--remember-pattern',
        '--scope-path='+scnShellQuote(sp),'--safety-condition='+scnShellQuote(cd)];
      outPre.textContent=base.join(' ');
    };
    scopeIn.addEventListener('input',buildCmd);condIn.addEventListener('input',buildCmd);buildCmd();
    body.append(
      ruleField('Область файлов (--scope-path)',scopeIn,'Относительный путь файла или директории, к которой относится правило.'),
      ruleField('Условие безопасности (--safety-condition)',condIn,'Объясните инвариант, который ревьюер должен перепроверить перед флагом (10–500 символов).'),
      outPre
    );
    const copyBtn=toolButton('Скопировать команду',async()=>{
      try{await navigator.clipboard.writeText(outPre.textContent);copyBtn.textContent='Скопировано';setTimeout(()=>copyBtn.textContent='Скопировать команду',1600);}
      catch{copyBtn.textContent='Выделите текст';}
    },'small primary');
    foot.append(copyBtn,toolButton('Закрыть',()=>dialog.close(),'small'));
    dialog.showModal();condIn.focus();
  }
  function openFixHandoffDialog(findingsList){
    const panel=ruleDialog('Отправить баги в работу агенту',plural(findingsList.length,['находка','находки','находок'])+' готовы для исправления');
    const {dialog,body,foot}=panel;
    const lines=['Исправь следующие подтверждённые проблемы из Review Hub (Richi):',''];
    findingsList.forEach((f,i)=>{
      const loc=(f.file||'(без файла)')+(f.line?':'+f.line:'');
      lines.push((i+1)+'. ['+f.severity+'] '+loc+' — '+f.title+' (run '+f.run_id+', '+f.finding_id+')');
      if(f.evidence)lines.push('   Доказательство: '+f.evidence);
    });
    lines.push('','После исправления и прогона тестов отметь находки как исправленные через richi scenario feedback.');
    const pre=make('pre','rule-command',lines.join('\n'));
    body.append(make('p','tools-inline-note','Скопируйте промпт в чат агента — агент получит точные файлы, строки и доказательства из Review Hub.'),pre);
    const copyBtn=toolButton('Скопировать промпт для чата',async()=>{
      try{await navigator.clipboard.writeText(pre.textContent);copyBtn.textContent='Скопировано';setTimeout(()=>copyBtn.textContent='Скопировать промпт для чата',1600);}
      catch{copyBtn.textContent='Выделите текст';}
    },'small primary');
    foot.append(copyBtn,toolButton('Закрыть',()=>dialog.close(),'small'));
    dialog.showModal();copyBtn.focus();
  }
  function buildBatchFeedbackCommands(cart){
    const byRun=new Map();
    for(const item of cart){
      if(!byRun.has(item.run_id))byRun.set(item.run_id,[]);
      const rec={finding_id:item.finding_id};
      if(item.verdict)rec.verdict=item.verdict;
      if(item.resolution)rec.resolution=item.resolution;
      byRun.get(item.run_id).push(rec);
    }
    const cmds=[];
    for(const [rid,items] of byRun.entries()){
      const prefix=['richi',...list(state.mapStore).map(scnShellQuote),'scenario','feedback',scnShellQuote(rid),'--batch','-'].join(' ');
      cmds.push(prefix+" <<'EOF'\n"+JSON.stringify({items},null,2)+"\nEOF");
    }
    return cmds.join('\n\n');
  }
  function renderReviewFpPatterns(root,data){
    const fps=list(data?.fp_patterns);
    if(!fps.length)return;
    const fpFold=make('details','tools-fold');fpFold.style.marginTop='14px';
    fpFold.append(make('summary','','Паттерны ложных срабатываний в памяти ('+fps.length+')'));
    const fpList=make('div','scn-fp-list');
    for(const pat of fps){
      const row=make('div','scn-fp-item'),rh=make('div','scn-finding-head');
      rh.append(make('strong','',pat.title||pat.entry_id),make('span','scn-pill-badge '+(pat.stale?'tpl':'project'),pat.stale?'Устарел (>90д)':'Актуален'));
      row.append(rh);
      if(list(pat.scope_paths).length)row.append(make('div','tools-inline-note','Область: '+pat.scope_paths.join(', ')));
      if(pat.safety_condition)row.append(make('div','','Условие безопасности: '+pat.safety_condition));
      fpList.append(row);
    }
    fpFold.append(fpList);root.append(fpFold);
  }
  function renderScenarioReviewHub(root){
    if(scenariosState.reviewLoading&&!scenariosState.reviewData){
      const box=make('div','tools-empty');
      box.append(ruleMark('rule'),make('h3','','Загружаем Review Hub'),make('p','','Собираем находки ревью, консенсус шагов, обратную связь и FP-паттерны.'));
      root.append(box);return;
    }
    if(scenariosState.reviewError&&!scenariosState.reviewData){
      const box=make('div','tools-empty');
      box.append(ruleMark('rule'),make('h3','','Не удалось загрузить Review Hub'),make('p','',scenariosState.reviewError),toolButton('Повторить',()=>loadScenarioReview(scenariosState.reviewProject),'primary'));
      root.append(box);return;
    }
    const data=scenariosState.reviewData;
    if(!data){loadScenarioReview(scenariosState.reviewProject);return;}
    if(scenariosState.reviewError)toolWarning(root,'Не удалось обновить Review Hub: '+scenariosState.reviewError);

    // Filter bar: project + refresh
    const topBar=make('div','scn-rev-filters');
    const projSel=make('select');projSel.add(new Option('Все проекты',''));
    for(const p of list(data.projects))projSel.add(new Option(p.name||p.id,p.id));
    projSel.value=scenariosState.reviewProject||'';
    projSel.addEventListener('change',()=>loadScenarioReview(projSel.value));
    topBar.append(projSel,toolButton('Обновить',()=>loadScenarioReview(scenariosState.reviewProject),'small'));
    root.append(topBar);

    if(!data.runs_analyzed){
      const hero=make('div','scn-rev-hero');
      hero.append(
        make('div','tools-eyebrow','Доменный подраздел · Review Hub'),
        make('h3','','Здесь соберутся все находки, консенсус и метрики ревью'),
        make('p','','Запустите любой сценарий ревью (с проверкой findings_grounded или тегом review), чтобы получать заземлённые находки с привязкой к файлам и строкам, отсеивать шум через disprove и обучать память Richi на ложных срабатываниях.')
      );
      const ctas=make('div','scn-rev-actions');
      const storedDefs=list(scenariosState.data?.scenarios);
      const builtinDefs=list(scenariosState.data?.builtin_templates);
      const resolveTemplate=tid=>{
        if(scenariosState.reviewProject){
          const proj=storedDefs.find(x=>x.id===tid&&x.scope==='project'&&x.project_id===scenariosState.reviewProject);
          if(proj)return proj;
        }
        const glob=storedDefs.find(x=>x.id===tid&&x.scope==='global');
        if(glob)return glob;
        return builtinDefs.find(x=>x.id===tid)||storedDefs.find(x=>x.id===tid)||null;
      };
      list(data.empty_cta_templates).forEach((tid,idx)=>{
        const found=resolveTemplate(tid);
        if(found){
          ctas.append(toolButton('Запустить: '+(found.definition?.title||tid),()=>openScenarioRunModal(found),'small '+(idx===0?'scn-btn-primary':'')));
        }
      });
      hero.append(ctas);
      root.append(hero);
      renderReviewFpPatterns(root,data);
      return;
    }

    // 5 Actionable KPI cards (NO live Recall/F1 in prod UI; color only when labeled >= 15)
    const k=data.kpis||{},sm=k.signal_mix||{},ob=k.open_blockers||{},dy=k.disprove_yield||{},fp=k.fp_health||{};
    const strip=make('div','scn-stats-strip');
    const sigCard=make('div','scn-stat-card');
    if(sm.sufficient_sample&&sm.actionable_share!==null){
      sigCard.classList.add(sm.actionable_share>=0.7?'tone-good':(sm.actionable_share>=0.45?'tone-warn':'tone-bad'));
    }
    const sigVal=sm.labeled_total?((sm.actionable_bugs+sm.valid_nits)+'/'+sm.labeled_total+' ('+Math.round((sm.actionable_share||0)*100)+'%)'):'—';
    const sigSub='размечено '+sm.labeled_total+'/'+sm.total_findings+(sm.sufficient_sample?' · сигнал/размечено':' · мало данных (< 15)');
    sigCard.append(make('strong','',sigVal),make('span','',sigSub));

    const blkCard=make('div','scn-stat-card'+(ob.p0>0?' tone-bad':(ob.p1>0?' tone-warn':'')));
    blkCard.append(make('strong','',String(ob.total||0)),make('span','','открытых P0/P1 ('+(ob.p0||0)+' P0 · '+(ob.p1||0)+' P1'+(ob.likely_fixed?(' · ~'+ob.likely_fixed+' испр.'):'')+')'));

    const dispCard=make('div','scn-stat-card');
    const dispVal=dy.candidates?(dy.disproved+'/'+dy.candidates+' ('+Math.round((dy.disprove_rate||0)*100)+'%)'):'—';
    dispCard.append(make('strong','',dispVal),make('span','','отсеяно disprove-критиком'));

    const fpCard=make('div','scn-stat-card'+(fp.stale>0?' tone-warn':''));
    fpCard.append(make('strong','',(fp.fresh||0)+'/'+(fp.total||0)),make('span','','FP-паттернов актуально'+(fp.stale?(' · '+fp.stale+' устар. >90д'):'')));

    const runsCard=make('div','scn-stat-card');
    runsCard.append(make('strong','',String(data.runs_analyzed||0)),make('span','','prod-запусков ревью'));
    strip.append(sigCard,blkCard,dispCard,fpCard,runsCard);
    root.append(strip);

    // Step signal breakdown table (collapsible)
    if(list(k.step_signal_table).length){
      const fold=make('details','tools-fold');
      fold.append(make('summary','','Сигнал по шагам ревью ('+k.step_signal_table.length+')'));
      const tbl=make('table','scn-rev-table');
      const thead=make('thead'),hr=make('tr');
      for(const h of ['Шаг','Всего','Баги','Замечания','FP','Без оценки','Доля сигнала'])hr.append(make('th','',h));
      thead.append(hr);tbl.append(thead);
      const tbody=make('tbody');
      for(const row of k.step_signal_table){
        const tr=make('tr');
        const rate=row.signal_rate===null?'—':(Math.round(row.signal_rate*100)+'%');
        const stepLabel=row.scenario_id?(row.scenario_id+' / '+row.step_id):row.step_id;
        for(const val of [stepLabel,row.total,row.actionable,row.valid_nit,row.false_positive,row.unlabeled,rate]){
          tr.append(make('td','',String(val)));
        }
        tbody.append(tr);
      }
      tbl.append(tbody);fold.append(tbl);root.append(fold);
    }

    // Read-only Batch Triage Cart
    if(!rulesState.editing&&scenariosState.triageCart.length){
      const cartBar=make('div','scn-rev-cart');
      cartBar.append(make('strong','','Корзина триажа (read-only): '+plural(scenariosState.triageCart.length,['пометка','пометки','пометок'])));
      const cBtns=make('div','scn-triage-btns');
      const copyBatch=toolButton('Скопировать batch CLI',async()=>{
        const cmd=buildBatchFeedbackCommands(scenariosState.triageCart);
        try{await navigator.clipboard.writeText(cmd);copyBatch.textContent='Скопировано';setTimeout(()=>copyBatch.textContent='Скопировать batch CLI',1800);}
        catch{copyBatch.textContent='Ошибка буфера';}
      },'small primary');
      cBtns.append(copyBatch,toolButton('Очистить',()=>{scenariosState.triageCart=[];renderScenarios();},'small'));
      cartBar.append(cBtns);root.append(cartBar);
    }

    // Inbox filters + Review -> Fix Handoff button (overlay read-only cart without mutating server snapshot)
    const cartMap=rulesState.editing?new Map():new Map(scenariosState.triageCart.map(c=>[c.run_id+'::'+c.finding_id,c]));
    const allFindings=list(data.findings).map(raw=>{
      const c=cartMap.get(raw.run_id+'::'+raw.finding_id);
      if(!c)return raw;
      return Object.assign({},raw,
        c.verdict!==undefined?{verdict:c.verdict}:{},
        c.resolution!==undefined?{resolution:c.resolution}:{}
      );
    });
    const fBar=make('div','scn-rev-filters');
    const stTabs=make('div','scn-subtabs');stTabs.style.margin='0';
    for(const [val,lbl] of [['open','Открытые'],['all','Все'],['fp','Ложные (FP)'],['disproved','Опровергнутые']]){
      const b=toolButton(lbl,()=>{scenariosState.reviewStatus=val;renderScenarios();},'scn-subtab');
      b.setAttribute('aria-pressed',String(scenariosState.reviewStatus===val));stTabs.append(b);
    }
    const sevSel=make('select');
    for(const [val,lbl] of [['all','Любая важность'],['P0','Только P0'],['P1','Только P1'],['P2','Только P2'],['nit','Только nit']]){
      sevSel.add(new Option(lbl,val));
    }
    sevSel.value=scenariosState.reviewSeverity||'all';
    sevSel.addEventListener('change',()=>{scenariosState.reviewSeverity=sevSel.value;renderScenarios();});

    const shown=allFindings.filter(f=>{
      if(scenariosState.reviewSeverity!=='all'&&f.severity!==scenariosState.reviewSeverity)return false;
      if(scenariosState.reviewStatus==='open')return f.status!=='disproved'&&f.resolution==='open'&&f.verdict!=='false_positive';
      if(scenariosState.reviewStatus==='fp')return f.verdict==='false_positive';
      if(scenariosState.reviewStatus==='disproved')return f.status==='disproved';
      return true;
    });
    const selectedItems=shown.filter(f=>scenariosState.reviewSelected.has(f.run_id+'::'+f.finding_id));
    const handoffTargets=selectedItems.length?selectedItems:shown.filter(f=>f.status!=='disproved'&&f.resolution==='open'&&f.verdict!=='false_positive'&&(f.severity==='P0'||f.severity==='P1'));
    const handoffBtn=toolButton(
      'Отправить '+handoffTargets.length+' в работу',
      ()=>openFixHandoffDialog(handoffTargets),
      'small scn-btn-primary'
    );
    handoffBtn.disabled=!handoffTargets.length;
    fBar.append(stTabs,sevSel,handoffBtn);
    root.append(fBar);

    const inbox=make('div','scn-rev-inbox');
    if(!shown.length){
      const em=make('div','tools-empty');
      em.append(ruleMark('rule'),make('h3','','Подходящих находок нет'),make('p','','Смените фильтр важности или статуса, чтобы увидеть остальные находки.'));
      inbox.append(em);
    }
    const applyTriage=async(finding,patch)=>{
      const key=finding.run_id+'::'+finding.finding_id;
      if(!rulesState.editing){
        const idx=scenariosState.triageCart.findIndex(c=>c.run_id===finding.run_id&&c.finding_id===finding.finding_id);
        const merged=Object.assign(idx>=0?scenariosState.triageCart[idx]:{run_id:finding.run_id,finding_id:finding.finding_id},patch);
        if(idx>=0)scenariosState.triageCart[idx]=merged;else scenariosState.triageCart.push(merged);
        announce('Добавлено в корзину триажа');renderScenarios();return;
      }
      if(scenariosState.triageBusy.has(key))return;
      scenariosState.triageBusy.add(key);
      const raw=list(scenariosState.reviewData?.findings).find(x=>x.run_id===finding.run_id&&x.finding_id===finding.finding_id)||finding;
      const prevV=raw.verdict,prevR=raw.resolution;
      if(patch.verdict!==undefined)raw.verdict=patch.verdict;
      if(patch.resolution!==undefined)raw.resolution=patch.resolution;
      scenariosState.reviewError='';
      renderScenarios();
      try{
        await sendScenarioFeedback(finding.run_id,[Object.assign({finding_id:finding.finding_id},patch)]);
        announce('Оценка сохранена');
        scenariosState.triageReloadPending=true;
      }catch(err){
        const curRaw=list(scenariosState.reviewData?.findings).find(x=>x.run_id===finding.run_id&&x.finding_id===finding.finding_id)||raw;
        curRaw.verdict=prevV;curRaw.resolution=prevR;
        const keyMsg=editKeyProblem(err);
        scenariosState.reviewError=keyMsg||changeFailure(err)||err.message||'Не удалось сохранить оценку.';
      }finally{
        scenariosState.triageBusy.delete(key);
        if(scenariosState.triageBusy.size===0&&scenariosState.triageReloadPending){
          scenariosState.triageReloadPending=false;
          loadScenarioReview(scenariosState.reviewProject,{preserveError:Boolean(scenariosState.reviewError)});
        }else{
          renderScenarios();
        }
      }
    };

    for(const f of shown){
      const key=f.run_id+'::'+f.finding_id;
      const isBusy=scenariosState.triageBusy.has(key);
      const card=make('article','scn-finding-card sev-'+(f.severity||'P2')+(f.resolution!=='open'||f.verdict==='false_positive'||f.status==='disproved'?' is-resolved':''));
      const head=make('div','scn-finding-head'),left=make('div','scn-finding-left');
      const cb=make('input');cb.type='checkbox';cb.checked=scenariosState.reviewSelected.has(key);
      cb.addEventListener('change',()=>{if(cb.checked)scenariosState.reviewSelected.add(key);else scenariosState.reviewSelected.delete(key);renderScenarios();});
      left.append(cb,make('span','scn-sev-pill sev-'+(f.severity||'P2'),f.severity||'P2'));
      const loc=(f.file||'(без файла)')+(f.line?(':'+f.line+(f.end_line&&f.end_line!==f.line?'-'+f.end_line:'')):'');
      const fileBtn=make('button','scn-file-btn',loc);fileBtn.type='button';fileBtn.title='Скопировать путь и строку';
      fileBtn.addEventListener('click',async()=>{
        try{await navigator.clipboard.writeText(loc);fileBtn.textContent='Скопировано: '+loc;setTimeout(()=>{fileBtn.textContent=loc;},1400);}catch{}
      });
      left.append(fileBtn,make('strong','',f.title||'(без заголовка)'));
      const badges=make('div','scn-triage-btns');
      if(f.consensus_ratio)badges.append(make('span','scn-pill-badge project','Консенсус '+f.consensus_ratio.votes+'/'+f.consensus_ratio.total));
      if(list(f.corroborated_by).length>1)badges.append(make('span','scn-pill-badge global','Подтверждено: '+f.corroborated_by.join(', ')));
      if(f.likely_fixed)badges.append(make('span','scn-pill-badge tpl','Вероятно исправлено'));
      if(f.status==='disproved')badges.append(make('span','scn-pill-badge count','Опровергнуто'));
      head.append(left,badges);
      card.append(head);

      const meta=make('div','rule-meta');
      meta.append(make('code','',f.finding_id),make('span','',f.scenario_title||f.scenario_id),make('code','',f.run_id));
      if(f.severity_votes&&Object.keys(f.severity_votes).length>1){
        meta.append(make('span','','голоса: '+Object.entries(f.severity_votes).map(([sv,cnt])=>sv+'×'+cnt).join(', ')));
      }
      card.append(meta);
      if(f.evidence)card.append(make('pre','scn-finding-ev',f.evidence));

      const tBar=make('div','scn-triage-bar'),tBtns=make('div','scn-triage-btns');
      const mkBtn=(lbl,pressed,fn)=>{const b=make('button','scn-triage-btn',lbl);b.type='button';b.disabled=isBusy;b.setAttribute('aria-pressed',String(Boolean(pressed)));b.addEventListener('click',fn);return b;};
      const reopenIfWontFix=f.resolution==='wont_fix'?{resolution:'open'}:{};
      tBtns.append(
        mkBtn('Баг',f.verdict==='actionable_bug',()=>applyTriage(f,Object.assign({verdict:'actionable_bug'},reopenIfWontFix))),
        mkBtn('Замечание',f.verdict==='valid_nit',()=>applyTriage(f,Object.assign({verdict:'valid_nit'},reopenIfWontFix))),
        mkBtn('Ложное (FP)',f.verdict==='false_positive',()=>applyTriage(f,{verdict:'false_positive',resolution:'wont_fix'})),
        mkBtn(f.resolution==='fixed'?'Исправлено ✓':'Исправлено',f.resolution==='fixed',()=>applyTriage(f,{resolution:f.resolution==='fixed'?'open':'fixed'}))
      );
      tBar.append(tBtns);
      if(f.verdict==='false_positive'){
        tBar.append(toolButton(f.pattern_entry_id?'FP-паттерн сохранён ('+f.pattern_entry_id+')':'Запомнить FP-паттерн (CLI)',()=>openRememberFpDialog(f),'small'));
      }
      card.append(tBar);
      inbox.append(card);
    }
    root.append(inbox);
    renderReviewFpPatterns(root,data);
  }
  function renderScenarios(){
    const root=$('scenarios-content');root.replaceChildren();
    $('scenarios-panel').setAttribute('aria-busy',String(scenariosState.loading));
    if(state.mode==='scenarios'){
      $('data-note').textContent=scenariosNote();
      const tb=document.querySelector('.toolbar');if(tb)tb.hidden=Boolean(scenariosState.editing);
    }
    const heading=make('div','tools-heading rules-heading'),intro=make('div'),count=make('div','tools-total');
    intro.append(make('div','tools-eyebrow','Исполняемые пайплайны'),make('h2','','Сценарии'),make('p','tools-lead','Визуальный конструктор пайплайнов (drag-and-drop шагов и соединение портов needs), мониторинг запусков и общий журнал событий.'));
    const totalDefs=list(scenariosState.data?.scenarios).length+list(scenariosState.data?.builtin_templates).length;
    count.append(make('strong','',String(totalDefs)),make('span','',plural(totalDefs,['сценарий','сценария','сценариев'])));
    heading.append(intro,count);root.append(heading);
    if(!scenariosState.data){
      const failed=Boolean(scenariosState.error)&&!scenariosState.loading,box=make('div','tools-empty');
      box.append(ruleMark('rule'),make('h3','',failed?'Не удалось загрузить сценарии':'Загружаем сценарии'),make('p','',failed?scenariosState.error:'Читаем определения, встроенные шаблоны, запуски и журнал.'));
      if(failed)box.append(toolButton('Повторить загрузку',()=>loadScenarios(),'primary'));
      root.append(box);return;
    }
    if(scenariosState.error)toolWarning(root,'Не удалось обновить сценарии: '+scenariosState.error+' Показан предыдущий снимок.');
    renderRuleMode(root,'Изменения сценариев сохраняются прямо в памяти workspace.');
    if(scenariosState.outcome)toolWarning(root,scenariosState.outcome);

    // Sub-tabs bar (4 clean L2 tabs driven by platform.domain_views: Review, Runs, Scenarios, Log)
    const subtabs=make('div','scn-subtabs');
    const runsList=list(scenariosState.data.runs);
    const runsTotal=scenariosState.data.runs_total||runsList.length;
    const waitingCount=scenariosState.data.runs_waiting!==undefined?scenariosState.data.runs_waiting:runsList.filter(r=>r.run_kind!=='eval'&&(r.status==='running'||r.status==='waiting')&&(r.step_counts?.waiting_human||0)>0).length;
    const domainViews=list(scenariosState.data.platform?.domain_views).filter(v=>v&&v.available!==false);
    const tabs=[];
    for(const dv of domainViews){
      if(dv.id==='review'){
        const blk=scenariosState.reviewData?.kpis?.open_blockers?.total||0;
        tabs.push(['review',(dv.title||'Ревью')+(blk?' ('+blk+')':'')]);
      }
    }
    tabs.push(
      ['runs','Запуски ('+runsTotal+(waitingCount?' · '+waitingCount+' ждут':'')+')'],
      ['definitions','Сценарии ('+list(scenariosState.data.scenarios).length+' + '+list(scenariosState.data.builtin_templates).length+')'],
      ['log','Журнал ('+list(scenariosState.data.log).length+(scenariosState.data.log_more?'+':'')+')'],
    );
    for(const [key,label] of tabs){
      const btn=toolButton(label,()=>{
        if(scenariosState.editing&&key!=='definitions'){
          confirmLeaveScenarioEditor(()=>{scenariosState.subtab=key;if(key==='review'&&!scenariosState.reviewData)loadScenarioReview(scenariosState.reviewProject);},'перейти');
          return;
        }
        scenariosState.subtab=key;
        if(key==='review'&&!scenariosState.reviewData&&!scenariosState.reviewLoading)loadScenarioReview(scenariosState.reviewProject);
        renderScenarios();
      },'scn-subtab');
      btn.setAttribute('aria-pressed',String(scenariosState.subtab===key));
      subtabs.append(btn);
    }
    root.append(subtabs);

    if(scenariosState.subtab==='review'){
      renderScenarioReviewHub(root);
    }else if(scenariosState.subtab==='definitions'){
      if(scenariosState.editing)renderScenarioEditor(root);
      else renderScenarioCatalog(root);
    }else if(scenariosState.subtab==='runs'){
      renderScenarioRuns(root);
    }else{
      renderScenarioLog(root);
    }
    syncScenarioTabBadge();
  }
