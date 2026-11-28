'use strict';
  // Standing rules (richi rules): the user's instructions agents load at the start of a chat.
  const viewModes=['map','encyclopedia','tools','scenarios','rules','agents'];
  const RULE_SECRET_KEY='richi-map-edit-secret',RULE_HEADER='X-Richi-Map-Secret',RULE_SOURCE='richi map';
  // rules-agents-pure: begin. No DOM here: tests/test_map_rules_ui.py runs these lines under Node.
  // Agents as the server last listed them (a rules, skills or agents answer). A rule may also
  // name agents this list lacks, and those are kept and shown as they are.
  // Rules, skills and agent listings share the agent names; the latter two also share a catalog.
  // Apply each part in request order. A committed edit supersedes every read already in flight.
  let mapAgents=[],agentSequence=0,namesSequence=0,catalogSequence=0;
  function setMapAgents(value,sequence){
    if(!Array.isArray(value)||sequence<namesSequence)return;
    namesSequence=sequence;mapAgents=value.filter(agent=>agent&&typeof agent.id==='string').map(agent=>({id:agent.id,name:agent.name}));
  }
  function setSkills(catalog,sequence){
    if(sequence<catalogSequence)return;
    catalogSequence=sequence;setMapAgents(catalog?.agents,sequence);rulesState.skills=catalog;
    rulesState.skillMap=new Map(list(catalog?.skills).filter(skill=>skill&&typeof skill.name==='string').map(skill=>[skill.name,skill]));
  }
  const knownAgentIds=()=>mapAgents.map(agent=>agent.id);
  const agentName=id=>mapAgents.find(agent=>agent.id===id)?.name||id;
  const agentList=ids=>ids.map(agentName).join(', ');
  const ruleAgentIds=(...rules)=>[...new Set([...knownAgentIds(),...rules.flatMap(rule=>Object.keys(rule?.skills||{}))])];
  // A rule names a skill for every agent (skill) and, where agents differ, one per agent (skills).
  const ruleSkillFor=(rule,agent)=>rule?.skills&&Object.prototype.hasOwnProperty.call(rule.skills,agent)?rule.skills[agent]||null:rule?.skill||null;
  function installedRuleSkill(name,agent){
    const aliases=rulesState.skills?.rule_aliases?.[agent];
    return aliases&&Object.prototype.hasOwnProperty.call(aliases,name)?aliases[name]:name;
  }
  // How a rule's skills read to a person: one for all listed agents, or one per agent.
  function ruleSkillSetting(rule,installed=false){
    const ids=ruleAgentIds(rule);if(!ids.length)return {skill:rule?.skill||null,skills:null};
    const own=Object.fromEntries(ids.map(agent=>{const name=ruleSkillFor(rule,agent);return [agent,installed?installedRuleSkill(name,agent):name];}));
    if(new Set(Object.values(own)).size===1)return {skill:own[ids[0]],skills:null};
    return {skill:null,skills:Object.fromEntries(Object.entries(own).filter(([,name])=>name))};
  }
  const ruleHasSkill=rule=>Boolean(rule?.skill)||Object.values(rule?.skills||{}).some(Boolean);
  // The every-agent skill of a rule in which each listed agent has one of its own: it then serves
  // only agents this computer does not list, and the per-agent view alone would never show it.
  const ruleOtherSkill=rule=>{const ids=ruleAgentIds(rule);return rule?.skill&&ids.length&&ids.every(agent=>Object.prototype.hasOwnProperty.call(rule.skills||{},agent))?rule.skill:null;};
  // The same skill for every agent listed here, and for the agents that are not (the rule's skill).
  const sameRuleSkills=(a,b)=>(a?.skill||null)===(b?.skill||null)&&ruleAgentIds(a,b).every(agent=>ruleSkillFor(a,agent)===ruleSkillFor(b,agent));
  // Whether the editor may show a rule as one skill for every agent: only when that skill is also
  // the one agents not listed here get, so that saving it unchanged changes nothing for anybody.
  const ruleSharesSkill=rule=>ruleAgentIds(rule).every(agent=>ruleSkillFor(rule,agent)===(rule?.skill||null));
  // The same pick for every agent listed here, with no skill of their own for the other agents, is
  // stored as the rule's skill for every agent (rules._skill_setting): agents added later get it too.
  // An agent the rule names beyond the listed ones keeps its own pick beside it, "no skill" included.
  const becomesShared=(base,chosen)=>{const names=knownAgentIds().map(agent=>(chosen||{})[agent]||null);return !base&&names.length>0&&Boolean(names[0])&&names.every(name=>name===names[0]);};
  // What a change sends. chosen is one name for every agent, or {agent: name} from the per-agent
  // lists; base is the rule's every-agent skill, which stays the default for agents this computer
  // does not list, so only agents that differ from it get an entry (null: no skill for that agent).
  // An agent this computer does not list keeps its null too: the rule names it, and its "no skill"
  // must not be merged into the skill of the others (rules._skill_setting keeps it the same way).
  function skillRequest(base,chosen){
    if(chosen===null||typeof chosen!=='object')return {skill:chosen||null,skills:null};
    base=base||null;const known=knownAgentIds();
    const own=Object.entries(chosen).map(([agent,name])=>[agent,name||null]).filter(([agent,name])=>name!==base||(name===null&&!known.includes(agent)));
    return {skill:base,skills:own.length?Object.fromEntries(own):null};
  }
  // A word zsh would expand (=word at its start) is quoted like any other.
  const shellQuote=value=>{value=String(value);return /^[A-Za-z0-9_.:\/@%+=,-]+$/.test(value)&&value[0]!=='='?value:"'"+value.replace(/'/g,"'\\''")+"'";};
  // --option=value: a value that starts with a dash is still read as the value.
  const commandOption=(flag,value)=>flag+'='+shellQuote(value);
  // store: the global options that reach the store the map shows, as its server gives them. A
  // command without them would change whatever store the terminal currently uses, so none is composed.
  function ruleCommand(change,store){
    const selector=list(store).map(shellQuote);if(!selector.length)return '';
    // Only a damaged row has an ID that needs quoting; one that begins with a dash is read as the
    // ID only after --, at the end of the command.
    const parts=['richi',...selector,'rules',change.action],dashed=String(change.id).startsWith('-');if(!dashed)parts.push(shellQuote(change.id));
    if(change.action==='add')parts.push(change.scope==='global'?'--global':commandOption('--project',change.project));
    else parts.push(commandOption('--expected-updated-at',change.expected_updated_at));
    if('text' in change)parts.push(commandOption('--text',change.text));
    if('check' in change){
      if(change.check===null)parts.push('--clear-check');
      else if('format' in change.check)parts.push(commandOption('--branch-format',change.check.format));
      else parts.push(commandOption('--forbid-branch-prefix',change.check.forbid_prefix));
    }
    if('skill' in change||'skills' in change){
      const options=[];if(change.skill)options.push(change.skill);for(const agent of Object.keys(change.skills||{}).sort())options.push(agent+'='+(change.skills[agent]||''));
      for(const option of options.length?options:[''])parts.push(commandOption('--skill',option));
    }
    if('scenario' in change)parts.push(commandOption('--scenario',change.scenario||''));
    if(change.action==='add')parts.push(commandOption('--source',RULE_SOURCE));
    if(dashed)parts.push('--',shellQuote(change.id));
    return parts.join(' ');
  }
  function agentCommand(change){
    const parts=['richi','skill-sources',change.action,change.id];
    if(change.expected_updated_at)parts.push(commandOption('--expected-updated-at',change.expected_updated_at));
    if('name' in change)parts.push(commandOption('--name',change.name));
    for(const folder of list(change.folders))parts.push(commandOption('--skills',folder));
    if('variable' in change&&(change.variable||change.action==='update'))parts.push(commandOption('--variable',change.variable||''));
    if(change.manual_skills&&(change.action==='update'||change.manual_skills!=='none'))parts.push(commandOption('--manual-skills',change.manual_skills));
    for(const [key,flag] of [['add_folders','--add-skills'],['remove_folders','--remove-skills'],['disable','--disable'],['enable','--enable']])for(const value of list(change[key]))parts.push(commandOption(flag,value));
    return parts.join(' ');
  }
  // Who changed a rule last: the map, or a chat and the agent whose shell it was (agent, which
  // the server stores beside the chat). A rule saved before it did names the agent only by the
  // prefix of the chat, which Codex's chat identity does not have.
  function changedBy(value,agent){
    if(typeof value!=='string'||!value)return '';if(value===RULE_SOURCE)return 'карта';
    const at=value.indexOf(':'),prefix=at>0?value.slice(0,at):'',known=knownAgentIds();
    // A build that does not know changed_by_agent keeps it unchanged while it records another chat: the
    // chat's own prefix wins where there is one.
    const who=prefix&&known.includes(prefix)?prefix:typeof agent==='string'&&known.includes(agent)?agent:'';
    if(!who)return value.length>40?value.slice(0,39)+'…':value;
    return agentName(who)+' · чат '+(who===prefix?value.slice(at+1):value).slice(0,8);
  }
  const SOURCE_LABELS={'claude-app':'Плагины claude.ai из приложения Claude',system:'Встроенные скиллы',plugins:'Плагины',skills:'Свои скиллы'};
  // A source is named by its own key where Richi knows it, else by its kind.
  const agentSourceLabel=(agent,source)=>source.added?(agent.builtin?'Добавленная папка':'Папка скиллов'):SOURCE_LABELS[source.key]||SOURCE_LABELS[source.kind]||SOURCE_LABELS.skills;
  // What the forms check before sending; the server checks again. tests/test_map_rules_ui.py holds
  // these against the Python validators, so the two cannot drift apart unnoticed.
  const RULE_NAME=/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$/;
  // u: characters are counted as Python counts them, and half of a surrogate pair is refused as there.
  const AGENT_ID=/^[a-z][a-z0-9-]{0,31}$/,AGENT_VARIABLE=/^[A-Za-z_][A-Za-z0-9_]{0,63}(=[^\x00-\x1f\x7f\ud800-\udfff]{1,100})?$/u;
  // As agents._one_line: at most limit characters as Python counts them, no control character, no
  // half of a surrogate pair, and nothing that Python's strip() removes at either end.
  const PYTHON_SPACE_AT_END=/^[ \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]|[ \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]$/u;
  const oneLine=(value,limit)=>typeof value==='string'&&value.length>0&&Array.from(value).length<=limit&&!/[\x00-\x1f\x7f]|[\ud800-\udfff]/u.test(value)&&!PYTHON_SPACE_AT_END.test(value);
  const agentNameOk=(value,limit)=>oneLine(value,limit);
  const agentFolderOk=(value,limit)=>oneLine(value,limit)&&(value==='~'||value.startsWith('~/')||value.startsWith('/'));
  // What an edit sends: only what differs from the version the form was opened with (opened), named
  // against the latest version known (rule). After a conflict the two differ, and a retry then keeps
  // the other change to whatever this form left alone. null: nothing was changed here.
  // A text field gives every line end as \n; the server stores them so (rules._text), and a rule
  // stored before may still hold \r.
  const lineEnds=value=>String(value??'').replace(/\r\n?/g,'\n');
  function branchCheckValue(check){return check?.format??check?.forbid_prefix??'';}
  function branchCheckKind(check){return check?('format' in check?'format':'forbid_prefix'):'';}
  function sameBranchCheck(a,b){return branchCheckKind(a)===branchCheckKind(b)&&branchCheckValue(a)===branchCheckValue(b);}
  function branchCheckLabel(check){
    if(!check)return 'Без автоматической проверки';
    return ('format' in check?'Формат имени ветки: ':'Запрещённый префикс ветки: ')+branchCheckValue(check);
  }
  function branchCheckFromForm(kind,value){
    if(!kind)return null;
    if(!value||Array.from(value).length>128||/[\p{C}\p{Z}]/u.test(value.replace(/ /g,'')))return 'Введите от 1 до 128 печатных символов для проверки имени ветки.';
    if(kind==='format'){
      const fields=value.match(/\{number\}|\{description\}/g)||[],literal=value.replace(/\{number\}|\{description\}/g,'');
      if(!fields.length||new Set(fields).size!==fields.length||/[^\p{L}\p{N}_./-]/u.test(literal))return 'В формате нужны {number} или {description}, каждый не более одного раза. Между ними допустимы буквы, цифры и знаки / . _ -.';
    }
    return {action:'branch-name',[kind]:value};
  }
  const sameCreation=(opened,current)=>opened===current||Boolean(opened.created_at&&opened.created_at===current.created_at);
  function ruleTargetProblem(opened,current){
    if(opened.scope!==current.scope||opened.project_id!==current.project_id)return 'Правило с этим ID создали заново в другой области. Скопируйте нужный текст и откройте актуальное правило.';
    if(!sameCreation(opened,current))return 'Правило с этим ID создали заново. Скопируйте нужный текст и откройте актуальное правило.';
    return '';
  }
  function ruleUpdate(opened,rule,text,setting,check=opened.check,baseCheck=opened.check,scenario=opened.scenario??null,baseScenario=opened.scenario??null){
    const target=ruleTargetProblem(opened,rule);if(target)return target;
    const update={action:'update',id:rule.id,expected_updated_at:rule.updated_at};
    if(lineEnds(text)!==lineEnds(opened.text))update.text=text;
    if(!sameBranchCheck(check,baseCheck))update.check=check;
    if((scenario||null)!==(baseScenario||null))update.scenario=scenario||null;
    if(!sameRuleSkills(setting,opened)){
      // An agent that neither this computer nor the latest version of the rule knows (another window
      // took it out): the server refuses any entry for it, "no skill" included, so it gets what
      // «Другие агенты» gives. A pick that this form left as it was gives way to that version, and one
      // equal to «Другие агенты» changes nothing; any other cannot be stored, and the form says so
      // (a text instead of a change).
      const known=knownAgentIds(),named=Object.keys(rule.skills||{}),gone=agent=>!known.includes(agent)&&!named.includes(agent);
      const others=setting.skill||null;
      const lost=Object.entries(setting.skills||{}).filter(([agent,name])=>gone(agent)&&name!==ruleSkillFor(opened,agent)&&name!==others).map(([agent])=>agent);
      if(lost.length)return 'Агента «'+lost.join('», «')+'» нет ни на этом компьютере, ни в новой версии правила: отдельный выбор для него сохранить нельзя, он получит то же, что «Другие агенты». Чтобы сохранить, выберите для него '+(others?'«'+others+'»':'«Без скилла»')+'.';
      const skills=Object.entries(setting.skills||{}).filter(([agent])=>!gone(agent));
      update.skill=setting.skill;update.skills=skills.length?Object.fromEntries(skills):null;
    }
    return 'text' in update||'skill' in update||'check' in update||'scenario' in update?update:null;
  }
  // What a save stored for the skills when the form did not say so beforehand (announced: it said
  // the skills become one for every agent). The server merges by the agents it knows, and they may
  // differ from the page's list when another window changed them; problem is the answer's
  // agent_settings_problem, which stops any merge.
  function skillOutcome(request,stored,announced,problem){
    if(!request?.skills||!stored)return '';
    if(announced&&!stored.skill)return 'Скиллы сохранены для каждого агента отдельно, а не как общий: '+(problem?'Richi сейчас не может использовать настройки некоторых агентов (подробности на вкладке «Агенты»).':'Richi знает агентов, которых нет в списке на этой странице. Нажмите «Обновить».');
    if(!announced&&!request.skill&&stored.skill)return 'Скилл «'+stored.skill+'» сохранён как общий для всех агентов: он один и тот же у каждого агента этого компьютера.';
    return '';
  }
  // The same for an own agent: opened is the agent the form was built from, agent the latest one known.
  const agentFields=agent=>({name:agent.name,variable:list(agent.variables)[0]||'',manual_skills:agent.manual_skills==='front-matter'?'front-matter':'none'});
  const agentTargetProblem=(opened,current)=>sameCreation(opened,current)?'':'Агента могли создать заново под тем же ID. Скопируйте нужные значения и откройте его актуальные настройки.';
  function agentUpdate(opened,agent,fields){
    const target=agentTargetProblem(opened,agent);if(target)return target;
    const before=agentFields(opened),update={action:'update',id:agent.id};
    for(const key of ['name','variable','manual_skills'])if(fields[key]!==before[key])update[key]=fields[key];
    if(Object.keys(update).length===2)return null;
    if(agent.updated_at)update.expected_updated_at=agent.updated_at;return update;
  }
  // What another change did to an agent, for the form that has to be saved once more.
  function agentDifference(before,after){
    const was=agentFields(before),now=agentFields(after),parts=[];
    if(was.name!==now.name)parts.push('имя «'+now.name+'»');
    if(was.variable!==now.variable)parts.push(now.variable?'переменная '+now.variable:'переменной больше нет');
    if(was.manual_skills!==now.manual_skills)parts.push(now.manual_skills==='front-matter'?'скиллы только по вызову скрываются':'показываются все скиллы');
    return parts.join(', ');
  }
  // A change waits up to 25 seconds for its turn and runs up to 20, so the page waits a minute.
  const CHANGE_TIMEOUT=60000;
  // A refused change in the user's words. The code decides, and subject where one code covers
  // several things; the server's message is English and only the last resort.
  function changeFailure(error){
    if(error.name==='AbortError')return 'Сервер не ответил за '+CHANGE_TIMEOUT/1000+' секунд. Изменение могло сохраниться: обновите список и проверьте.';
    if(error.code==='offline')return 'Связь с картой прервалась. Изменение могло сохраниться: откройте карту через richi map и проверьте список перед повтором.';
    if(error.code==='busy')return 'Сейчас выполняется другое изменение, или хранилище занято другой командой. Повторите через несколько секунд.';
    if(error.code==='timeout')return 'Команда не уложилась в отведённое время. Изменение могло сохраниться: обновите список и проверьте.';
    if(error.code==='unreadable')return 'Ответ карты не прочитался. Изменение могло сохраниться: обновите список и проверьте.';
    return error.message;
  }
  function ruleRefusal(error){
    if(error.code==='conflict')return 'Правило изменили после загрузки списка. Закройте форму, нажмите «Обновить» и повторите изменение.';
    if(error.code==='exists')return 'Правило с таким ID уже есть. Выберите другой ID.';
    if(error.code==='not_found')return 'Правило или проект не найдены: список устарел. Закройте форму и нажмите «Обновить».';
    if(error.code==='limit')return error.subject==='rules'?'Все правила workspace вместе превысят лимит символов. Сократите или удалите другие правила.':error.subject==='record'?'Правило вместе со всем, что в нём хранится, слишком велико. Сократите текст.':'Текст правила длиннее допустимого.';
    return changeFailure(error);
  }
  function agentRefusal(error,settingsFile){
    if(error.code==='conflict')return 'Настройки этого агента изменили в другом окне. Список обновлён: проверьте его и повторите изменение.';
    if(error.code==='exists')return error.subject==='source'?'Эта папка уже есть среди источников агента.':'Агент с таким ID уже есть.';
    if(error.code==='not_found')return (error.subject==='source'?'Источник не найден':error.subject==='agent'?'Агент не найден':'Агент или источник не найден')+': список устарел. Нажмите «Обновить».';
    if(error.code==='limit')return error.subject==='agents'?'Своих агентов уже столько, сколько Richi хранит. Удалите ненужного.':error.subject==='folders'?'У агента уже столько папок, сколько Richi хранит. Уберите ненужную.':error.subject==='size'?'Настройки агентов не поместятся в файл, который Richi читает. Удалите агента или папку.':'Достигнут предел числа агентов или папок.';
    if(error.code==='settings'&&error.subject==='agent')return 'Настройки этого агента записала более новая версия Richi, или они повреждены: эта версия их хранит, но не меняет. Обновите Richi (richi update) или удалите эти настройки командой из предупреждения выше.';
    if(error.code==='settings'&&error.subject==='large')return 'Файл настроек агентов больше, чем читает эта версия Richi, поэтому изменения не сохраняются. Если его записала более новая версия, обновите Richi: richi update; иначе уменьшите файл: '+(settingsFile||'agents.json')+'.';
    if(error.code==='settings')return error.subject==='newer'?'Настройки агентов записала более новая версия Richi, и эта версия их не меняет, чтобы ничего в них не потерять. Обновите Richi: richi update.':'Файл настроек агентов повреждён, поэтому изменения не сохраняются. Исправьте или удалите файл: '+(settingsFile||'agents.json')+'.';
    return changeFailure(error);
  }
  // Why the agent settings are not used, by the listing's problem_code.
  function agentSettingsProblem(code,settingsFile){
    const file=settingsFile||'agents.json';
    if(code==='newer')return 'Настройки агентов записала более новая версия Richi, поэтому эта версия показывает только встроенных агентов без ваших изменений и не меняет настройки. Обновите Richi: richi update.';
    if(code==='large')return 'Файл настроек агентов больше, чем читает эта версия Richi, поэтому показаны только встроенные агенты без ваших изменений, и настройки не меняются. Если его записала более новая версия, обновите Richi: richi update; иначе уменьшите файл: '+file+'.';
    if(code==='unreadable')return 'Настройки агентов сейчас не прочитались, поэтому показаны только встроенные агенты без ваших изменений. Обновите список; если не поможет, проверьте файл: '+file+'.';
    return 'Настройки агентов сейчас не используются: файл повреждён, поэтому показаны только встроенные агенты без ваших изменений. Исправьте или удалите файл: '+file+'.';
  }
  // A stored agent this version does not use: one a newer Richi has built in, or settings it cannot read.
  function unknownAgentText(item){
    if(!item.error)return 'В настройках есть агент «'+item.id+'», записанный более новой версией Richi. Эта версия его настройки хранит, но не использует. Обновите Richi: richi update.';
    return 'Настройки агента «'+item.id+'» эта версия Richi использовать не может: их записала более новая версия, они повреждены или своих агентов больше, чем она использует. Они хранятся как есть. Обновите Richi (richi update)'+(typeof item.next==='string'?' или удалите их: '+item.next:'')+'.';
  }
  const translit={а:'a',б:'b',в:'v',г:'g',д:'d',е:'e',ё:'e',ж:'zh',з:'z',и:'i',й:'y',к:'k',л:'l',м:'m',н:'n',о:'o',п:'p',р:'r',с:'s',т:'t',у:'u',ф:'f',х:'h',ц:'c',ч:'ch',ш:'sh',щ:'sch',ъ:'',ы:'y',ь:'',э:'e',ю:'yu',я:'ya'};
  // An ID suggested from what the user typed: Latin words joined by dashes.
  function ruleSlug(value){
    const latin=Array.from(String(value||'').toLocaleLowerCase('ru')).map(ch=>translit[ch]??ch).join('');
    return latin.replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').split('-').slice(0,5).join('-').slice(0,48).replace(/-+$/,'');
  }
  // The skills of a rule as the words of its text.
  function skillPhrase(setting){
    const said=ruleSkillSetting(setting);if(!said.skills)return said.skill?'используй скилл '+said.skill:'';
    const parts=ruleAgentIds(setting).map(agent=>[agent,ruleSkillFor(setting,agent)]).filter(([,name])=>name);
    if(parts.length===1)return 'в '+agentName(parts[0][0])+' используй скилл '+parts[0][1];
    const pieces=parts.map(([agent,name])=>name+' в '+agentName(agent));
    return parts.length?'используй скилл '+pieces.slice(0,-1).join(', ')+' и '+pieces[pieces.length-1]:'';
  }
  // Every form saves the same way: send the change, then close the form, or say what went wrong
  // and, once the edit key has stopped working, show the command instead. The form may have been
  // closed or replaced before the answer came (alive); then the tab carries the message.
  // steps: send, saved(answer), explain(error) -> text, failed(error) -> a better text or '' (it
  // may reload what the form edits), lost(text), problem(text), commands(), label(), busy.
  async function saveFromForm(panel,submit,steps){
    // The submitted values are already captured. Keep the draft visible and the close button usable
    // while saving, but do not accept edits that the success callback would silently discard.
    if(submit.disabled)return;
    submit.disabled=true;panel.body.inert=true;if(steps.busy)submit.textContent=steps.busy;
    let answer,failure=null;
    try{answer=await steps.send();}catch(error){failure=error;}
    if(failure){
      try{
        const explained=steps.explain(failure),message=(steps.failed?await steps.failed(failure):'')||explained;
        if(!panel.alive())steps.lost(message);
        else{steps.problem(message);if(!rulesState.editing)steps.commands();}
      }finally{if(panel.alive()){panel.body.inert=false;submit.disabled=false;steps.label();}}
      return;
    }
    // Saved. Whatever happens while the page takes the answer in is not a failed save.
    if(panel.alive())panel.dialog.close();
    steps.saved(answer);
  }
  // rules-agents-pure: end.
  const ruleTemplates={
    text:{label:'Свой текст',hint:'Любая постоянная инструкция или промт.'},
    skill:{label:'Скилл для действия',hint:'Агент берёт выбранный скилл, когда делает это действие.'},
    naming:{label:'Формат имён',hint:'Как называть ветки, коммиты, PR или задачи.'}
  };
  const skillSituations=['создании pull request','написании описания pull request','ревью кода','создании коммита','написании тестов','работе с задачами в Jira'];
  const namingTargets=[['ветки','branch-naming'],['коммиты','commit-naming'],['pull request','pr-naming'],['задачи','task-naming'],['файлы','file-naming']];
  const rulesState={data:null,loading:false,error:'',failure:'',outcome:'',editing:false,request:0,query:'',scope:'',skills:null,skillMap:new Map(),skillsLoading:false,notice:''};
  let ruleSecret='',ruleKeyVersion=0,ticketRequest=0,ruleFieldCount=0,rulesController=null;
  try{ruleSecret=sessionStorage.getItem(RULE_SECRET_KEY)||'';}catch{ruleSecret='';}
  function storeRuleSecret(value){if((value||'')!==ruleSecret)ruleKeyVersion++;ruleSecret=value||'';try{if(ruleSecret)sessionStorage.setItem(RULE_SECRET_KEY,ruleSecret);else sessionStorage.removeItem(RULE_SECRET_KEY);}catch{}}
  const ruleList=()=>list(rulesState.data?.rules).filter(rule=>rule&&typeof rule.id==='string');
  const ruleLength=value=>Array.from(String(value||'')).length;
  // Rules of a newer format: this version does not serve them, but their text counts toward the limit.
  const newerRuleCharacters=()=>list(rulesState.data?.unsupported_rules).reduce((sum,item)=>sum+(Number(item?.characters)||0),0);
  const ruleCountLabel=n=>{const a=n%10,b=n%100;return a===1&&b!==11?'правило':a>=2&&a<=4&&(b<12||b>14)?'правила':'правил';};
  const rulesNote=()=>rulesState.editing?'Правила · изменения сохраняются в workspace':'Правила · режим команд';
  function ruleProjectNames(){
    const names=new Map();for(const node of state.nodes)if(groupFor(node)==='project')names.set(String(node.id).slice(8),titleFor(node));
    // The rules answer lists the registered projects, so the form does not wait for the graph.
    for(const project of list(rulesState.data?.projects))if(project&&typeof project.id==='string')names.set(project.id,project.name||project.id);
    for(const rule of ruleList())if(rule.project_id&&!names.has(rule.project_id))names.set(rule.project_id,rule.project_id);
    return new Map([...names.entries()].sort((a,b)=>String(a[1]).localeCompare(String(b[1]),'ru')));
  }
  const ruleProjectName=id=>ruleProjectNames().get(id)||id;
  const ruleScopeText=rule=>rule.scope==='global'?'Все чаты workspace':'Проект · '+ruleProjectName(rule.project_id);
  function uniqueRuleId(base){
    const taken=new Set(ruleList().map(rule=>rule.id));base=base||'rule';
    let id=base,index=2;while(taken.has(id))id=base+'-'+index++;return id;
  }
  function ruleMark(kind){
    const mark=make('span','rule-mark'+(kind==='skill'?' skill':''));mark.setAttribute('aria-hidden','true');const icon=makeSvg('svg',{viewBox:'0 0 24 24',fill:'none'});
    icon.append(makeSvg('path',{d:kind==='skill'?'M12 3.5l2.5 5 5.5.8-4 3.9.9 5.5-4.9-2.6-4.9 2.6.9-5.5-4-3.9 5.5-.8Z':'M6.5 3.5h11v17l-2.75-1.8-2.75 1.8-2.75-1.8-2.75 1.8v-17ZM9.5 8.5h5M9.5 12h5',stroke:'currentColor','stroke-width':'1.4','stroke-linejoin':'round','stroke-linecap':'round'}));
    mark.append(icon);return mark;
  }
  function ruleField(label,control,hint=null,target=control){
    const field=make('div','rule-field');if(!target.id)target.id='rule-field-'+(++ruleFieldCount);const caption=make('label','field-label',label);caption.htmlFor=target.id;field.append(caption,control);
    if(hint)field.append(make('span','rule-field-hint',hint));return field;
  }

  // One edit key serves rules and agents: a page either saves both or composes commands for both.
  function setMapEditing(editing,sent){rulesState.editing=editing;if(sent&&!editing){storeRuleSecret('');rulesState.notice='expired';}}
  const skillsKnown=()=>Boolean(rulesState.skills)&&!rulesState.skills.unavailable&&!rulesState.skills.truncated;
  function refreshRulePicker(){
    const dialog=$('rule-dialog');
    if(dialog.open)dialog.querySelector('.rule-skill-picker')?.dispatchEvent(new Event('catalogchange'));
    $('scenarios-content')?.querySelectorAll('.scn-skill-picker')?.forEach(el=>el.dispatchEvent(new Event('catalogchange')));
  }
  async function loadSkills(){
    if(rulesState.skillsLoading)return;const sequence=++agentSequence;rulesState.skillsLoading=true;
    // Like the other lists: a reading that never ends must not leave the pickers loading for good.
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),20000);
    try{const response=await fetch('/api/skills',{cache:'no-store',signal:controller.signal});const data=await response.json();if(!response.ok||!data||!Array.isArray(data.skills))throw new Error(data&&data.error||'HTTP '+response.status);setSkills(data,sequence);}
    catch(_error){setSkills({skills:[],unavailable:true},sequence);}
    finally{clearTimeout(timeout);rulesState.skillsLoading=false;refreshRulePicker();if(state.mode==='scenarios'&&!scenariosState.editing)renderScenarios();if(state.mode==='rules')renderRules();if(state.mode==='agents')renderAgents();}
  }
  async function loadRules(){
    if(rulesController)rulesController.abort();const request=++rulesState.request,sequence=++agentSequence,sent=ruleSecret;rulesState.loading=true;rulesState.error='';renderRules();
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),20000);rulesController=controller;
    try{
      const response=await fetch('/api/rules',{cache:'no-store',headers:sent?{[RULE_HEADER]:sent}:{},signal:controller.signal});let data=null;try{data=await response.json();}catch{data=null;}
      if(!response.ok)throw new Error(data&&data.error||'Сервер вернул HTTP '+response.status);if(!data||!Array.isArray(data.rules))throw new Error('Неожиданный формат ответа сервера');
      if(request!==rulesState.request)return;
      if(sent!==ruleSecret){rulesState.loading=false;loadRules();return;}
      rulesState.data=data;rulesState.failure='';setMapAgents(data.agents,sequence);setMapEditing(data.editing===true,sent);
    }catch(error){if(request!==rulesState.request)return;rulesState.error=error.name==='AbortError'?'Сервер не ответил за 20 секунд.':error.message;}
    finally{clearTimeout(timeout);if(request===rulesState.request){rulesState.loading=false;rulesController=null;if(state.mode==='rules'){renderProjectOptions();renderRules();}if(state.mode==='agents')renderAgents();}}
    if(!rulesState.skills&&state.mode!=='agents')loadSkills();
  }
  async function redeemRuleTicket(){
    const match=/^#ticket=([0-9a-f]{64})$/.exec(location.hash);if(!match)return;
    const request=++ticketRequest,controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),20000);
    // The ticket works once; it never stays in the address bar or history entry.
    history.replaceState(null,'',location.pathname+location.search);
    try{
      const response=await fetch('/api/session',{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticket:match[1]}),signal:controller.signal});
      let data=null;try{data=await response.json();}catch{}
      if(request!==ticketRequest)return;
      if(response.ok&&typeof data?.secret==='string'){storeRuleSecret(data.secret);rulesState.notice='';}
      else rulesState.notice=response.status===403?'ticket':'unavailable';
    }catch{if(request===ticketRequest)rulesState.notice='unavailable';}
    finally{
      clearTimeout(timeout);
      if(request===ticketRequest){if(typeof scenariosState!=='undefined'&&(state.mode==='scenarios'||scenariosState.data))loadScenarios();if(state.mode==='rules'||rulesState.data)loadRules();if(state.mode==='agents'||agentsState.data)loadAgents();}
    }
  }
  const sendRuleChange=change=>sendMapChange('/api/rules',change);
  async function sendMapChange(url,change){
    const keyVersion=ruleKeyVersion,controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),CHANGE_TIMEOUT);let response,data=null;
    try{
      try{response=await fetch(url,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json',[RULE_HEADER]:ruleSecret},body:JSON.stringify(change),signal:controller.signal});}
      catch(error){if(error.name==='AbortError')throw error;const offline=new Error(error.message);offline.code='offline';throw offline;}
      try{data=await response.json();}catch(error){if(error.name==='AbortError')throw error;data=null;}
    }finally{clearTimeout(timeout);}
    if(!response.ok){const error=new Error(data&&data.error||'Сервер вернул HTTP '+response.status);error.code=data&&data.code;error.subject=data&&data.subject;error.keyVersion=keyVersion;throw error;}
    // Saved, but the answer was cut off: what the page would take in is missing.
    if(!data||typeof data!=='object'){const error=new Error('Ответ карты не прочитался');error.code='unreadable';throw error;}
    return data;
  }
  function editKeyProblem(error){
    if(error.code!=='not_editing')return '';
    if(ruleSecret&&error.keyVersion!==ruleKeyVersion)return 'Пока выполнялся запрос, ключ редактирования сменился. Повторите изменение с текущим ключом.';
    setMapEditing(false,true);
    return 'Ключ редактирования больше не действует: карту перезапустили или открыли заново в другой вкладке. Чтобы сохранять здесь, снова выполните richi map.';
  }
  const ruleChangeProblem=error=>editKeyProblem(error)||ruleRefusal(error);
  // A tab is rebuilt whenever its data arrives, which drops the focused control. Controls carry a
  // key (data-focus) by which the rebuilt one is found again; the first key that works wins.
  function focusByKey(root,...keys){
    for(const key of keys){if(!key)continue;const target=[...root.querySelectorAll('[data-focus]')].find(element=>element.dataset.focus===key&&!element.disabled);if(target){target.focus({preventScroll:true});return target;}}
    return null;
  }
  const keyed=(element,key)=>{element.dataset.focus=key;return element;};
  function restoreTabFocus(key){
    if(state.mode==='scenarios')focusByKey($('scenarios-content'),key,'scn-add');
    else if(state.mode==='rules')focusByKey($('rules-content'),key,'rule-add');
    else if(state.mode==='agents')focusByKey($('agents-content'),key,'item:'+agentsState.selected,'add-agent');
  }
  let dialogInstance=0;
  function ruleDialog(title,subtitle){
    const dialog=$('rule-dialog'),opener=document.activeElement?.dataset?.focus||'';if(dialog.open)dialog.close();dialog.replaceChildren();const form=make('form'),instance=++dialogInstance;form.noValidate=true;
    const head=make('div','rule-dialog-head'),titles=make('div'),heading=make('h2','',title);heading.id='rule-dialog-title';titles.append(heading);if(subtitle)titles.append(make('p','',subtitle));
    const close=toolButton('×',()=>dialog.close(),'icon');close.setAttribute('aria-label','Закрыть');close.title='Закрыть · Esc';head.append(titles,close);
    const body=make('div','rule-dialog-body'),foot=make('div','rule-dialog-foot'),unsaved=make('div','rule-unsaved','Есть несохранённые изменения. Нажмите Esc ещё раз, чтобы закрыть без сохранения.');unsaved.hidden=true;unsaved.setAttribute('role','status');
    form.append(head,body,unsaved,foot);dialog.append(form);
    // Esc closes at once only a form nobody typed into: the first Esc on a changed form asks.
    let dirty=false,asked=false;form.addEventListener('input',()=>{dirty=true;asked=false;unsaved.hidden=true;});
    dialog.oncancel=event=>{if(instance!==dialogInstance||!dirty||asked)return;event.preventDefault();asked=true;unsaved.hidden=false;};
    // The browser returns focus to the control that opened the form, unless the tab was rebuilt
    // meanwhile: then focus is nowhere, or still on a control of the form that has just closed.
    dialog.onclose=()=>{const active=document.activeElement;if(instance===dialogInstance&&(!active||active===document.body||!active.isConnected||dialog.contains(active)))restoreTabFocus(opener);};
    // alive: a save answers later, and by then the user may have closed this form or opened another.
    return {dialog,form,body,foot,alive:()=>instance===dialogInstance&&dialog.open};
  }
  const ruleCommandFor=change=>ruleCommand(change,state.mapStore)||'# Карта ещё не сообщила, какое хранилище она показывает. Нажмите «Обновить» и повторите.';
  function ruleProblem(parent){const box=make('div','rule-error');box.setAttribute('role','alert');box.hidden=true;parent.append(box);return text=>{box.textContent=text||'';box.hidden=!text;};}
  function ruleCommandBox(parent,hint=''){
    const box=make('div','rule-command');box.hidden=true;const pre=make('pre');
    const copy=toolButton('Скопировать команду',async()=>{try{await navigator.clipboard.writeText(pre.textContent);copy.textContent='Скопировано';setTimeout(()=>copy.textContent='Скопировать команду',1600);}catch{copy.textContent='Выделите текст';}},'small');
    box.append(make('div','rule-field-hint',hint||(list(state.mapStore).length===2&&state.mapStore[0]==='--workspace'?'Выполните в терминале или передайте агенту. Команда меняет правила workspace «'+state.mapStore[1]+'».':'Выполните в терминале. Команда называет файл хранилища, которое показывает карта; чат агента, привязанный к workspace, такую команду не примет.')),pre,copy);parent.append(box);
    return command=>{pre.textContent=command;box.hidden=false;box.scrollIntoView({block:'nearest'});};
  }
  function skillSelect(agent,current){
    // agent: list only the skills this agent has; null lists every skill with the agents that have it.
    const box=make('div','rule-skill-control'),select=make('select'),other=make('input'),about=make('div','rule-field-hint');
    let skills;
    function populate(){
      skills=list(rulesState.skills?.skills).filter(skill=>!agent||list(skill.agents).includes(agent));
      select.replaceChildren();select.add(new Option('Без скилла',''));
      const kinds=[['skills','Скиллы'],['system','Встроенные скиллы'],['plugins','Скиллы плагинов']],kindOf=skill=>kinds.some(([kind])=>kind===skill.kind)?skill.kind:'skills';
      for(const [kind,label] of kinds){const members=skills.filter(skill=>kindOf(skill)===kind);if(!members.length)continue;const group=make('optgroup');group.label=label;
        for(const skill of members){const agents=agent?'':agentList(list(skill.agents));group.append(new Option(skill.name+(agents?' · '+agents:''),skill.name));}select.append(group);}
      select.add(new Option('Другой скилл — ввести имя…','__other__'));
    }
    populate();
    other.type='text';other.maxLength=100;other.placeholder='Например, plugin:skill-name';other.autocomplete='off';other.spellcheck=false;other.setAttribute('aria-label','Имя скилла'+(agent?' для '+agentName(agent):''));
    const describe=()=>{const value=select.value,skill=rulesState.skillMap.get(value);other.hidden=value!=='__other__';
      about.textContent=value==='__other__'?'Имя так, как его называет агент. Карта не проверяет, что такой скилл установлен.':skill?cut(skill.description||'Описание в SKILL.md не указано.',280):rulesState.skillsLoading?'Загружаем установленные скиллы…':
        !skills.length?(rulesState.skills?.truncated?'В показанной части каталога подходящих скиллов нет. Имя можно ввести вручную.':'Установленные скиллы не найдены. Имя можно ввести вручную.'):(agent?'Скиллы, которые видит '+agentName(agent)+'.':'Скиллы агентов на этом компьютере ('+agentList(knownAgentIds())+'): свои, встроенные и из плагинов.')+(rulesState.skills?.truncated?' Показана часть каталога.':'');};
    const set=name=>{name=name||'';if(name&&!skills.some(skill=>skill.name===name)){select.value='__other__';other.value=name;}else{select.value=name;other.value='';}describe();};
    // typing: the name is still being written, so nothing may react to a prefix of it.
    const changed=typing=>box.dispatchEvent(new CustomEvent('skillchange',{bubbles:true,detail:{typing}}));
    select.addEventListener('change',()=>{describe();changed(false);if(select.value==='__other__')other.focus();});
    other.addEventListener('input',()=>changed(true));other.addEventListener('change',()=>changed(false));set(current);box.append(select,other,about);
    function refresh(){
      const selected=select.value;populate();
      // The user may be halfway through a manual name. Preserve the field, its value and its caret.
      if(selected==='__other__'){select.value=selected;describe();}
      else set(selected);
    }
    return {element:box,select,set,refresh,value:()=>select.value==='__other__'?other.value.trim():select.value};
  }
  function ruleSkillPicker(rule){
    // One skill for every agent when they share it; otherwise a list per agent, and the skill of the
    // agents the list does not name, which is the rule's skill for every agent.
    const box=make('div','rule-skill-picker'),modes=make('div','rule-skill-modes'),group='rule-skill-mode-'+(++ruleFieldCount);
    modes.setAttribute('role','radiogroup');modes.setAttribute('aria-label','Для каких агентов скилл');
    const radio=label=>{const wrap=make('label','check-label'),input=make('input');input.type='radio';input.name=group;wrap.append(input,document.createTextNode(label));modes.append(wrap);return input;};
    const sharedRadio=radio('Один скилл для всех агентов'),ownRadio=radio('Свой для каждого агента');
    const ids=ruleAgentIds(rule||{}),known=knownAgentIds(),base=rule?.skill||'',shared=skillSelect(null,base),status=make('div','rule-skill-sync'),sharedPanel=make('div','rule-skill-shared');sharedPanel.append(shared.element,status);
    const agentsPanel=make('div','rule-agent-skills'),notice=make('div','rule-field-hint rule-skill-notice'),perAgent=new Map();
    for(const agent of ids){
      const row=make('div','rule-agent-skill'),control=skillSelect(agent,ruleSkillFor(rule,agent)||''),label=make('label','rule-agent-name',agentName(agent));
      control.select.id='rule-agent-skill-'+agent+'-'+(++ruleFieldCount);label.htmlFor=control.select.id;row.append(label,control.element);agentsPanel.append(row);perAgent.set(agent,control);
    }
    // The rule's skill for every agent stays visible beside the per-agent ones: it is what agents this
    // list does not name get, those added later included.
    const others=skillSelect(null,base),othersRow=make('div','rule-agent-skill rule-other-skill'),othersLabel=make('label','rule-agent-name','Другие агенты');
    others.select.id='rule-other-skill-'+(++ruleFieldCount);othersLabel.htmlFor=others.select.id;othersRow.append(othersLabel,others.element);
    const merged=make('div','rule-field-hint rule-skill-merged');
    merged.textContent='У всех агентов этого компьютера один и тот же скилл: правило сохранит его как общий, и его получат агенты, добавленные позже.'+(ids.some(agent=>!known.includes(agent))?' Агенты из правила, которых нет на этом компьютере, сохранят свой выбор.':'');
    // Without a list of agents there is nothing to choose per agent.
    agentsPanel.append(othersRow,make('div','rule-field-hint','Агенты, которых нет в этом списке, например добавленные позже.'),merged,notice);modes.hidden=!ids.length;box.append(modes,sharedPanel,agentsPanel);
    let mode=ruleSharesSkill(rule||{})?'shared':'agents';
    const changed=()=>box.dispatchEvent(new CustomEvent('skillchange',{bubbles:true,detail:{typing:false}}));
    const listed=name=>rulesState.skillMap.has(name)||known.some(agent=>installedRuleSkill(name,agent)!==name);
    const lacking=name=>listed(name)?known.filter(agent=>!list(rulesState.skillMap.get(installedRuleSkill(name,agent))?.agents).includes(agent)):[];
    const similar=(name,agent)=>{const base=name.split(':').pop().toLowerCase(),found=list(rulesState.skills?.skills).find(skill=>list(skill.agents).includes(agent)&&skill.name.split(':').pop().toLowerCase()===base);return found?found.name:'';};
    function showStatus(){
      const name=shared.value(),missing=name?lacking(name):[];status.className='rule-skill-sync';status.replaceChildren();
      if(!name||!listed(name))return;
      if(!missing.length){status.classList.add('ok');status.textContent='✓ Один и тот же скилл у всех агентов: '+agentList(known.length?known:ids);return;}
      status.classList.add('warn');status.append(document.createTextNode(agentList(missing)+(missing.length>1?' не видят':' не видит')+' этот скилл.'),toolButton('Выбрать для каждого агента',()=>split(name),'small'));
    }
    const picks=()=>Object.fromEntries(ids.map(agent=>[agent,perAgent.get(agent).value()||null]));
    const setting=()=>mode==='shared'?skillRequest(null,shared.value()):skillRequest(others.value(),picks());
    // Said only when saving would change the skills: a rule that already reads so is left alone.
    // Nor while Richi cannot use the agent settings: it then merges nothing (agent_settings_problem).
    function showMerged(){merged.hidden=!(mode==='agents'&&!rulesState.data?.agent_settings_problem&&becomesShared(others.value(),picks())&&!sameRuleSkills(setting(),rule||{}));}
    function setMode(next){mode=next;sharedRadio.checked=mode==='shared';ownRadio.checked=mode==='agents';sharedPanel.hidden=mode!=='shared';agentsPanel.hidden=mode!=='agents';showStatus();showMerged();}
    function split(name){
      // Agents that lack the skill get one with the same name after the plugin prefix, if they have it.
      const notes=[];
      for(const agent of ids){
        const has=!name||!lacking(name).includes(agent),pick=has?name:similar(name,agent);perAgent.get(agent).set(pick);
        if(!has)notes.push(pick?agentName(agent)+' не видит «'+name+'», поэтому выбран похожий «'+pick+'».':agentName(agent)+' не видит «'+name+'»: выберите для него свой скилл или оставьте без скилла.');
      }
      // The skill chosen for every agent stays the one of the agents the list does not name.
      others.set(name);
      notice.textContent=notes.join(' ');setMode('agents');changed();
    }
    shared.element.addEventListener('skillchange',event=>{const name=shared.value();if(!event.detail?.typing&&name&&lacking(name).length)split(name);else showStatus();});
    for(const control of perAgent.values())control.element.addEventListener('skillchange',()=>{notice.textContent='';showMerged();});
    others.element.addEventListener('skillchange',showMerged);
    sharedRadio.addEventListener('change',()=>{const names=[...ids.map(agent=>perAgent.get(agent).value()),others.value()].filter(Boolean);shared.set(names[0]||'');notice.textContent='';setMode('shared');changed();});
    ownRadio.addEventListener('change',()=>{if(shared.value())split(shared.value());else{setMode('agents');changed();}});
    box.addEventListener('catalogchange',()=>{
      shared.refresh();others.refresh();for(const control of perAgent.values())control.refresh();showStatus();showMerged();
    });
    setMode(mode);
    const values=()=>mode==='shared'?[shared.value()]:[...ids.map(agent=>perAgent.get(agent).value()),others.value()];
    return {element:box,focusTarget:shared.select,names:()=>values().filter(Boolean),value:setting,announcesMerge:()=>!merged.hidden};
  }
  function openRuleEditor(options={}){
    // opened: the version this form was built from; rule: the latest one known, whose version a save names.
    let rule=options.rule||null,template=rule?'text':options.template||'text',textTouched=Boolean(rule),idTouched=false;const opened=rule;rulesState.failure='';rulesState.outcome='';
    const panel=ruleDialog(rule?'Изменить правило':'Новое правило',rule?rule.id+' · '+ruleScopeText(rule):'Агенты загрузят его в начале каждого подходящего чата.'),{dialog,form,body,foot}=panel;
    const templateRow=make('div','rule-templates'),templateButtons=new Map();templateRow.setAttribute('role','group');templateRow.setAttribute('aria-label','Шаблон правила');
    for(const [key,item] of Object.entries(ruleTemplates)){const button=toolButton('',()=>{template=key;syncTemplate();compose();},'rule-template');button.append(make('strong','',item.label),make('span','',item.hint));templateButtons.set(key,button);templateRow.append(button);}
    const situation=make('input'),situations=make('datalist');situation.type='text';situation.maxLength=200;situation.placeholder='например, создании pull request';situations.id='rule-situations';situation.setAttribute('list',situations.id);skillSituations.forEach(value=>situations.append(new Option(value)));
    const situationField=ruleField('Когда применять',situation,'Попадёт в текст: «При … используй скилл …».');situationField.append(situations);
    const target=make('select'),format=make('input'),example=make('input');namingTargets.forEach(([label])=>target.add(new Option(label,label)));
    for(const input of [format,example]){input.type='text';input.maxLength=300;input.spellcheck=false;input.autocomplete='off';}format.placeholder='<ключ задачи>_<описание>';example.placeholder='PROJ-1234_fix_login';
    const namingRow=make('div','rule-field-row');namingRow.append(ruleField('Что называть',target),ruleField('Формат',format,'Постоянные части и <заполнители>. То, что меняется от задачи к задаче или от проекта к проекту, пишите заполнителем.'),ruleField('Пример',example,'Необязательно.'));
    const skill=ruleSkillPicker(rule),skillField=ruleField('Скилл',skill.element,null,skill.focusTarget);
    const text=make('textarea'),counter=make('div','rule-counter'),own=make('span'),total=make('span');text.rows=6;text.value=rule?.text||'';text.placeholder='Например: отвечай пользователю на русском языке, а код и команды оставляй как есть.';counter.append(own,total);
    const textField=ruleField('Текст правила',text,'Агент получит этот текст дословно. Одна инструкция на правило.');textField.insertBefore(counter,textField.lastChild);
    let baseCheck=rule?.check||null,baseScenario=rule?.scenario||null;
    const scenarioInput=make('input'),scenarioList=make('datalist');
    scenarioInput.type='text';scenarioInput.maxLength=100;scenarioInput.spellcheck=false;scenarioInput.autocomplete='off';
    scenarioInput.value=rule?.scenario||'';scenarioInput.placeholder='например, deep-review или review (необязательно)';
    scenarioList.id='rule-scenarios-list';scenarioInput.setAttribute('list',scenarioList.id);
    const knownScenarioIds=new Set();
    for(const item of list(scenariosState?.data?.scenarios))knownScenarioIds.add(item.id);
    for(const item of list(scenariosState?.data?.builtin_templates))knownScenarioIds.add(item.id);
    if(!knownScenarioIds.size){knownScenarioIds.add('review');knownScenarioIds.add('deep-review');}
    for(const sid of [...knownScenarioIds].sort())scenarioList.append(new Option(sid,sid));
    const scenarioField=ruleField('Рекомендуемый сценарий (необязательно)',scenarioInput,'Агент предложит запустить этот сценарий, когда сработает правило (молча не запускает).');
    scenarioField.append(scenarioList);
    const checkKind=make('select'),checkInput=make('input'),checkRow=make('div','rule-field-row');
    for(const [key,label] of [['','Без проверки'],['format','Формат имени ветки'],['forbid_prefix','Запрещённый префикс ветки']])checkKind.add(new Option(label,key));
    checkInput.type='text';checkInput.spellcheck=false;checkInput.autocomplete='off';
    const checkDetail=ruleField('Значение',checkInput);
    checkRow.append(ruleField('Автоматическая проверка',checkKind),checkDetail);
    function syncCheck(){
      checkDetail.hidden=!checkKind.value;
      checkDetail.querySelector('label').textContent=checkKind.value==='format'?'Шаблон ветки':'Префикс';
      checkInput.placeholder=checkKind.value==='format'?'REC-{number}_{description}':'codex/';
    }
    function setCheck(check){checkKind.value=branchCheckKind(check);checkInput.value=branchCheckValue(check);syncCheck();}
    setCheck(baseCheck);checkKind.addEventListener('change',syncCheck);
    const scopeField=make('div','rule-field');let scopeValue=()=>({scope:rule.scope,project:rule.project_id});
    if(rule)scopeField.append(make('span','field-label','Область'),make('div','rule-readonly',ruleScopeText(rule)+'. Чтобы сменить область, добавьте новое правило и удалите это.'));
    else{
      const legend=make('span','field-label','Область'),choices=make('div','rule-scope-options'),projectSelect=make('select'),projects=ruleProjectNames();choices.setAttribute('role','radiogroup');choices.setAttribute('aria-label','Область правила');
      const radio=(value,label)=>{const wrap=make('label','check-label'),input=make('input');input.type='radio';input.name='rule-scope';input.value=value;wrap.append(input,document.createTextNode(label));choices.append(wrap);return input;};
      const globalRadio=radio('global','Все чаты workspace'),projectRadio=radio('project','Только проект');projectSelect.setAttribute('aria-label','Проект');for(const [id,name] of projects)projectSelect.add(new Option(name===id?id:name+' · '+id,id));choices.append(projectSelect);
      const preset=rulesState.scope.startsWith('project:')?rulesState.scope.slice(8):'';if(preset&&projects.has(preset)){projectRadio.checked=true;projectSelect.value=preset;}else globalRadio.checked=true;
      if(!projects.size){projectRadio.disabled=true;projectSelect.hidden=true;}
      const sync=()=>{projectSelect.disabled=!projectRadio.checked;};globalRadio.addEventListener('change',sync);projectRadio.addEventListener('change',sync);sync();
      scopeField.append(legend,choices,make('span','rule-field-hint',projects.size?'Правило проекта агент получает при работе в этом репозитории.':'В workspace нет зарегистрированных проектов, поэтому доступна только глобальная область.'));
      scopeValue=()=>projectRadio.checked?(projectSelect.value?{scope:'project',project:projectSelect.value}:null):{scope:'global'};
    }
    const idInput=make('input');idInput.type='text';idInput.maxLength=100;idInput.spellcheck=false;idInput.autocomplete='off';idInput.placeholder='branch-naming';
    const idField=ruleField('ID правила',idInput,'Латиница, цифры, «.», «_», «:» или «-». По ID правило потом меняют и удаляют.');idInput.addEventListener('input',()=>{idTouched=true;});
    if(rule)body.append(skillField,scenarioField,textField,checkRow,scopeField);else body.append(templateRow,situationField,namingRow,skillField,scenarioField,textField,checkRow,scopeField,idField);
    const problem=ruleProblem(body),showCommand=ruleCommandBox(body),cancel=toolButton('Отмена',()=>dialog.close()),submit=make('button','primary');submit.type='submit';foot.append(cancel,submit);
    const submitLabel=()=>{submit.textContent=rulesState.editing?(rule?'Сохранить':'Добавить правило'):'Показать команду';};
    function templateText(){
      if(template==='skill'){const phrase=skillPhrase(skill.value()),when=situation.value.trim();return phrase?(when?'При '+when+' '+phrase:phrase.charAt(0).toUpperCase()+phrase.slice(1))+'.':'';}
      if(template==='naming'){const shape=format.value.trim(),sample=example.value.trim();return shape?'Называй '+target.value+' по формату '+shape+'.'+(sample?' Пример: '+sample+'.':''):'';}
      return '';
    }
    function suggestedId(){
      if(template==='skill'&&skill.names().length)return uniqueRuleId(ruleSlug('skill-'+skill.names()[0].replace(/:/g,'-')));
      if(template==='naming'){const found=namingTargets.find(([label])=>label===target.value);return uniqueRuleId(found?found[1]:'naming');}
      return uniqueRuleId(ruleSlug(text.value));
    }
    function updateCounter(){
      const limit=rulesState.data?.limit_text||2000,totalLimit=rulesState.data?.limit_characters||20000,length=ruleLength(text.value),others=ruleList().filter(item=>item.id!==rule?.id).reduce((sum,item)=>sum+ruleLength(item.text),0)+newerRuleCharacters();
      own.textContent=length.toLocaleString('ru-RU')+' / '+limit.toLocaleString('ru-RU');own.className=length>limit?'over':'';total.textContent='Все правила: '+(others+length).toLocaleString('ru-RU')+' / '+totalLimit.toLocaleString('ru-RU');total.className=others+length>totalLimit?'over':'';
    }
    function syncTemplate(){
      for(const [key,button] of templateButtons)button.setAttribute('aria-pressed',String(key===template));situationField.hidden=template!=='skill';namingRow.hidden=template!=='naming';
      skillField.querySelector('.field-label').textContent=template==='skill'||(rule&&ruleHasSkill(rule))?'Скилл':'Скилл (необязательно)';
    }
    function compose(){if(!rule&&!textTouched&&template!=='text')text.value=templateText();if(!rule&&!idTouched)idInput.value=suggestedId();updateCounter();}
    function change(){
      const value=text.value,setting=skill.value(),limit=rulesState.data?.limit_text||2000;
      if(!value.trim())return 'Напишите текст правила.';if(value.trim()==='-')return 'Текст из одного дефиса команда прочитала бы как «возьми текст из stdin». Напишите правило словами.';if(ruleLength(value)>limit)return 'Текст длиннее '+limit+' символов. Разделите его на несколько правил.';
      if(skill.names().some(name=>!RULE_NAME.test(name)))return 'Имя скилла: латиница, цифры, «.», «_», «:» или «-», до 100 символов, начиная с буквы или цифры.';
      if(!rule&&template==='skill'&&!ruleHasSkill(setting))return 'Выберите скилл.';
      const check=branchCheckFromForm(checkKind.value,checkInput.value);
      if(typeof check==='string')return check;
      const scenarioVal=scenarioInput.value.trim()||null;
      if(scenarioVal&&!RULE_NAME.test(scenarioVal))return 'ID сценария: 1–100 символов — латиница, цифры, «.», «_», «:» или «-», начиная с буквы или цифры.';
      if(rule)return ruleUpdate(opened,rule,value,setting,check,baseCheck,scenarioVal,baseScenario)||'Изменений нет.';
      const id=idInput.value.trim();if(!RULE_NAME.test(id))return 'ID: 1–100 символов — латиница, цифры, «.», «_», «:» или «-», начиная с буквы или цифры.';
      if(ruleList().some(item=>item.id===id))return 'Правило с ID «'+id+'» уже есть.';const scope=scopeValue();if(!scope)return 'Выберите проект.';
      const add={action:'add',id,scope:scope.scope,text:value};if(scope.project)add.project=scope.project;if(setting.skill)add.skill=setting.skill;if(setting.skills)add.skills=setting.skills;if(scenarioVal)add.scenario=scenarioVal;if(check)add.check=check;return add;
    }
    text.addEventListener('input',()=>{textTouched=text.value!=='';if(!rule&&!idTouched&&template==='text')idInput.value=suggestedId();updateCounter();});
    for(const input of [situation,format,example])input.addEventListener('input',compose);target.addEventListener('change',compose);skill.element.addEventListener('skillchange',compose);
    form.addEventListener('submit',async event=>{
      event.preventDefault();problem('');const request=change();if(typeof request==='string'){problem(request);return;}
      if(!rulesState.editing){showCommand(ruleCommandFor(request));return;}
      const announced=skill.announcesMerge();
      await saveFromForm(panel,submit,{busy:'Сохраняем…',send:()=>sendRuleChange(request),explain:ruleChangeProblem,problem,label:submitLabel,
        saved:answer=>{rulesState.outcome=skillOutcome(request,answer?.rule,announced,answer?.agent_settings_problem);announce((rule?'Правило изменено':'Правило добавлено')+(rulesState.outcome?'. '+rulesState.outcome:''));loadRules();},
        // Somebody changed the rule meanwhile: take its new version and keep what is typed here.
        failed:async error=>{
          if(error.code!=='conflict'||!rule)return '';
          await loadRules();const fresh=ruleList().find(item=>item.id===rule.id);
          if(!fresh)return 'Правило удалили, пока форма была открыта. Скопируйте текст, закройте форму и добавьте правило заново.';
          if(fresh.updated_at===rule.updated_at)return '';
          const target=ruleTargetProblem(opened,fresh);if(target){rule=fresh;return target;}
          const differs=[],skillsDiffer=!sameRuleSkills(fresh,rule),mine=!sameRuleSkills(skill.value(),opened);if(fresh.text!==rule.text)differs.push('другой текст: «'+cut(fresh.text,240)+'»');if(skillsDiffer)differs.push('другие скиллы');
          if(!sameBranchCheck(fresh.check,baseCheck)){
            differs.push('проверка «'+branchCheckLabel(fresh.check)+'»');
            if(checkKind.value===branchCheckKind(baseCheck)&&(!checkKind.value||checkInput.value===branchCheckValue(baseCheck)))setCheck(fresh.check);
          }
          baseCheck=fresh.check||null;rule=fresh;
          // The skills of a rule are written together: the ones chosen here replace the new ones.
          return 'Правило изменили, пока форма была открыта'+(differs.length?'; в памяти теперь '+differs.join(' и '):'')+'. Нажмите «Сохранить» ещё раз: запишется только то, что вы изменили в этой форме'+(skillsDiffer&&mine?' (скиллы — целиком так, как они выбраны здесь)':'')+', остальное останется как в новой версии.';
        },
        lost:message=>{rulesState.failure=message;renderRules();},commands:()=>{showCommand(ruleCommandFor(request));renderRules();}});
    });
    syncTemplate();compose();submitLabel();dialog.showModal();(rule?text:template==='skill'?situation:template==='naming'?format:text).focus();
  }
  function openRuleRemoval(rule){
    rulesState.failure='';rulesState.outcome='';
    const panel=ruleDialog(rule.invalid?'Удалить повреждённую запись?':'Удалить правило?',rule.invalid?rule.id:rule.id+' · '+ruleScopeText(rule)),{dialog,form,body,foot}=panel,quote=make('blockquote','rule-quote',cut(rule.invalid||rule.text,500));
    body.append(make('p','rule-confirm',rule.invalid?'Эта запись правила повреждена, и агенты её не получают. Удаление нельзя отменить.':'Агенты перестанут получать это правило, когда в следующий раз загрузят правила. Удаление нельзя отменить: вернуть правило можно, только добавив его заново.'),quote);
    const problem=ruleProblem(body),showCommand=ruleCommandBox(body),cancel=toolButton('Отмена',()=>dialog.close()),submit=make('button',rulesState.editing?'danger solid':'primary',rulesState.editing?'Удалить правило':'Показать команду');submit.type='submit';foot.append(cancel,submit);
    form.addEventListener('submit',async event=>{
      event.preventDefault();problem('');const request={action:'remove',id:rule.id,expected_updated_at:rule.updated_at};
      if(!rulesState.editing){showCommand(ruleCommandFor(request));return;}
      await saveFromForm(panel,submit,{send:()=>sendRuleChange(request),explain:ruleChangeProblem,problem,
        saved:()=>{announce('Правило удалено');loadRules();},
        failed:async error=>{
          if(error.code!=='not_found'&&(error.code!=='conflict'||rule.invalid))return '';
          await loadRules();const fresh=ruleList().find(item=>item.id===rule.id);
          if(error.code!=='conflict'||!fresh||fresh.updated_at===rule.updated_at)return '';
          const target=ruleTargetProblem(rule,fresh);if(target)return target;
          rule=fresh;quote.textContent=cut(fresh.text,500);
          return 'Правило изменили, пока вы подтверждали удаление. Выше — его новая версия: нажмите «Удалить правило» ещё раз, если всё ещё хотите удалить.';
        },
        lost:message=>{rulesState.failure=message;renderRules();},commands:()=>{submit.className='primary';showCommand(ruleCommandFor(request));renderRules();},
        label:()=>{submit.textContent=rulesState.editing?'Удалить правило':'Показать команду';}});
    });
    dialog.showModal();cancel.focus();
  }
  function ruleMatches(rule){
    const query=normalized(rulesState.query);if(!query)return true;
    const shown=ruleSkillSetting(rule,true);
    const fields=[rule.id,rule.text,branchCheckValue(rule.check),rule.skill,...Object.values(rule.skills||{}),shown.skill,...Object.values(shown.skills||{}),
      rule.source,rule.project_id&&ruleProjectName(rule.project_id)].map(normalized);
    return query.split(/\s+/).filter(Boolean).every(word=>fields.some(field=>field.includes(word)));
  }
  function ruleSkillChip(label,name,agents){
    // Warns when an agent this chip is for does not have the skill installed.
    const known=skillsKnown(),info=name?rulesState.skillMap.get(name):null;
    const missing=known&&name&&info?agents.filter(agent=>knownAgentIds().includes(agent)&&!list(info.agents).includes(agent)):[];
    const chip=make('div','rule-skill'+(name?'':' none')+(name&&known&&(!info||missing.length)?' missing':''));chip.append(make('span','rule-skill-label',label),make('code','',name||'без скилла'));
    if(name&&known){
      if(!info)chip.append(make('span','rule-skill-agents','не найден среди установленных'));
      else{const have=agents.filter(agent=>!missing.includes(agent));if(agents.length>1&&have.length)chip.append(make('span','rule-skill-agents',have.map(agentName).join(' · ')));
        if(missing.length)chip.append(make('span','rule-skill-agents warn',agents.length>1?'нет у '+agentList(missing):'не установлен'));}
    }
    if(info?.description)chip.title=info.description;return chip;
  }
  function ruleSkillChips(rule){
    const setting=ruleSkillSetting(rule,true),hidden=ruleOtherSkill(rule),other=hidden&&(setting.skills||hidden!==setting.skill)?hidden:null;
    if(!setting.skills&&!other)return ruleSkillChip('Скилл',setting.skill,knownAgentIds());
    const box=make('div','rule-skills');
    if(!setting.skills)box.append(ruleSkillChip('Скилл',setting.skill,knownAgentIds()));
    else for(const agent of ruleAgentIds(setting))box.append(ruleSkillChip(agentName(agent),ruleSkillFor(setting,agent),[agent]));
    if(other){const chip=make('div','rule-skill');chip.append(make('span','rule-skill-label','Другие агенты'),make('code','',other));chip.title='Скилл для агентов, которых нет в списке на этом компьютере.';box.append(chip);}
    return box;
  }
  function renderRuleCard(rule){
    const card=make('article','rule-card'),top=make('div','rule-card-top'),title=make('div','rule-title');card.dataset.rule=rule.id;
    title.append(make('code','rule-id',rule.id),make('span','rule-scope',ruleScopeText(rule)));top.append(ruleMark(ruleHasSkill(rule)?'skill':'rule'),title);card.append(top);
    const text=make('p','rule-text',rule.text);card.append(text);
    if(ruleLength(rule.text)>360||String(rule.text).split('\n').length>6){text.classList.add('clamped');const more=toolButton('Показать полностью',()=>{const folded=text.classList.toggle('clamped');more.textContent=folded?'Показать полностью':'Свернуть';more.setAttribute('aria-expanded',String(!folded));},'rule-more');more.setAttribute('aria-expanded','false');card.append(more);}
    if(ruleHasSkill(rule))card.append(ruleSkillChips(rule));
    if(rule.scenario){
      const scnRow=make('div','rule-meta');
      scnRow.append(make('span','','Рекомендуемый сценарий:'),toolButton(rule.scenario,()=>{scenariosState.subtab='definitions';scenariosState.query=rule.scenario;setMode('scenarios');},'small'));
      card.append(scnRow);
    }
    if(rule.check)card.append(make('p','rule-meta',branchCheckLabel(rule.check)));
    const bottom=make('div','rule-card-bottom'),meta=make('div','rule-meta'),actions=make('div','rule-actions');meta.append(make('span','','Изменено '+dateText(rule.updated_at)));
    const author=changedBy(rule.changed_by,rule.changed_by_agent);if(author){const who=make('span','','Кем: '+author);who.title=rule.changed_by;meta.append(who);}
    if(rule.source){const source=make('span','','Источник: '+cut(rule.source,70));source.title=rule.source;meta.append(source);}
    const edit=keyed(toolButton('Изменить',()=>openRuleEditor({rule}),'small'),'rule-edit:'+rule.id),remove=keyed(toolButton('Удалить',()=>openRuleRemoval(rule),'small danger'),'rule-remove:'+rule.id);edit.setAttribute('aria-label','Изменить правило '+rule.id);remove.setAttribute('aria-label','Удалить правило '+rule.id);
    actions.append(edit,remove);bottom.append(meta,actions);card.append(bottom);return card;
  }
  function renderRuleMode(root,saved='Изменения сразу сохраняются в памяти workspace. Агенты получат их при следующей загрузке правил: в новом чате, после сжатия контекста или при смене проекта.'){
    const box=make('div','rules-mode '+(rulesState.editing?'editing':'commands')),icon=make('span','rules-mode-icon',rulesState.editing?'✓':'›_'),content=make('div');box.setAttribute('role','status');icon.setAttribute('aria-hidden','true');
    if(rulesState.editing)content.append(make('strong','','Редактирование включено'),make('span','',saved));
    else{
      const line=make('span');line.append(document.createTextNode(rulesState.notice==='expired'?'Ключ редактирования больше не действует: карту перезапустили или открыли заново в другой вкладке. ':rulesState.notice==='ticket'?'Ключ из ссылки не подошёл: он уже использован или устарел. ':rulesState.notice==='unavailable'?'Не удалось получить ключ редактирования от карты. ':'Вкладка открыта без ключа редактирования, например по закладке. '),
        document.createTextNode('Форма соберёт команду richi. Чтобы сохранять прямо здесь, откройте карту командой '),make('code','','richi map'),document.createTextNode('.'));
      content.append(make('strong','','Режим команд'),line);
    }
    box.append(icon,content);root.append(box);
  }
  function renderRuleStarters(root){
    const box=make('div','tools-empty rules-empty'),starters=make('div','rule-starters');box.append(ruleMark('rule'),make('h3','','Правил пока нет'),make('p','','Закрепите то, что агент должен делать всегда: формат имён, обязательные проверки, нужный скилл или любой промт.'));
    for(const [key,title,description] of [['skill','Скилл для действия','«При создании pull request используй скилл pr-description.»'],['naming','Формат имён','«Называй ветки по формату <ключ задачи>_<описание>.»'],['text','Свой текст','Любая постоянная инструкция или промт для агентов.']]){const button=keyed(toolButton('',()=>openRuleEditor({template:key}),'rule-starter'),'rule-add:'+key);button.append(make('strong','',title),make('span','',description));starters.append(button);}
    box.append(starters);root.append(box);
  }
  function renderRules(){
    const root=$('rules-content'),active=document.activeElement,key=active&&root.contains(active)&&active.dataset.focus||'';
    renderRulesContent();if(key&&!$('rule-dialog').open)focusByKey(root,key,'rule-add');
  }
  function renderRulesContent(){
    const root=$('rules-content');root.replaceChildren();$('rules-panel').setAttribute('aria-busy',String(rulesState.loading));if(state.mode==='rules')$('data-note').textContent=rulesNote();
    const all=ruleList(),limit=rulesState.data?.limit_characters||20000,used=all.reduce((sum,rule)=>sum+ruleLength(rule.text),0)+newerRuleCharacters();
    const heading=make('div','tools-heading rules-heading'),intro=make('div'),totals=make('div','rules-totals'),count=make('div','tools-total'),meter=make('div','rules-meter'+(used>limit*.85?' near':'')),bar=make('div','rules-meter-bar'),fill=make('i');
    intro.append(make('div','tools-eyebrow','Постоянные инструкции'),make('h2','','Правила агентов'),make('p','tools-lead','Агенты загружают эти правила в начале каждого чата'+(state.workspaceName?' workspace «'+state.workspaceName+'»':'')+', после сжатия контекста и при смене проекта и выполняют их как ваши инструкции. Глобальные действуют во всех чатах, правила проекта — при работе с ним. Более новая просьба в чате важнее правила.'));
    count.append(make('strong','',String(all.length)),make('span','',ruleCountLabel(all.length)));fill.style.width=Math.min(100,used/limit*100).toFixed(1)+'%';bar.append(fill);
    meter.append(make('strong','',used.toLocaleString('ru-RU')+' / '+limit.toLocaleString('ru-RU')),make('span','','символов загружают агенты'),bar);meter.title='Агенты загружают правила целиком, поэтому общий объём ограничен.';
    totals.append(count,meter);heading.append(intro,totals);root.append(heading);
    if(!rulesState.data){
      const failed=Boolean(rulesState.error)&&!rulesState.loading,box=make('div','tools-empty');box.append(ruleMark('rule'),make('h3','',failed?'Не удалось загрузить правила':'Загружаем правила'),make('p','',failed?rulesState.error:'Читаем правила workspace.'));
      if(failed)box.append(toolButton('Повторить загрузку',()=>loadRules(),'primary'));root.append(box);return;
    }
    if(rulesState.error)toolWarning(root,'Не удалось обновить правила: '+rulesState.error+' Показан предыдущий список.');
    if(rulesState.failure)toolWarning(root,'Изменение не сохранено. '+rulesState.failure);
    if(rulesState.outcome)toolWarning(root,rulesState.outcome);
    if(rulesState.data.agent_settings_problem)toolWarning(root,'Richi сейчас не может использовать все настройки агентов (подробности на вкладке «Агенты»): в списках агентов здесь нет ваших агентов из этих настроек, и одинаковые скиллы всех агентов не объединяются в общий.');
    // A row that cannot be read is not a rule any more: agents do not get it, and only removing it helps.
    for(const item of list(rulesState.data.invalid_rules).filter(item=>item&&typeof item.id==='string')){
      const box=make('div','tools-warning rule-invalid');box.setAttribute('role','status');
      box.append(make('span','','Запись правила «'+item.id+'» повреждена, и агенты её не получают: '+cut(String(item.error||'запись не читается'),200)));
      // Only a row the server can name (next) can be removed from here: its key is shown in full and is text.
      if(typeof item.next==='string'&&typeof item.updated_at==='string')box.append(keyed(toolButton(rulesState.editing?'Удалить запись':'Команда удаления',()=>openRuleRemoval({id:item.id,updated_at:item.updated_at,invalid:String(item.error||'Запись не читается.')}),'small'),'rule-remove:'+item.id));
      else box.append(make('span','',' Удалить её можно только инструментом для SQLite.'));
      root.append(box);
    }
    for(const item of list(rulesState.data.unsupported_rules).filter(item=>item&&typeof item.id==='string'))toolWarning(root,'Правило «'+item.id+'» записано более новой версией Richi, и эта версия его агентам не отдаёт. Обновите Richi: richi update.');
    renderRuleMode(root);
    const actions=make('div','rules-actions');actions.append(keyed(toolButton('+ Новое правило',()=>openRuleEditor(),'primary'),'rule-add'),keyed(toolButton('Скилл для действия',()=>openRuleEditor({template:'skill'})),'rule-add:skill'),keyed(toolButton('Формат имён',()=>openRuleEditor({template:'naming'})),'rule-add:naming'));root.append(actions);
    if(!all.length){renderRuleStarters(root);return;}
    const shown=all.filter(rule=>(!rulesState.scope||(rulesState.scope==='global'?rule.scope==='global':'project:'+rule.project_id===rulesState.scope))&&ruleMatches(rule));
    if(!shown.length){const box=make('div','tools-empty');box.append(ruleMark('rule'),make('h3','','Ничего не найдено'),make('p','','Измените запрос или область.'));root.append(box);return;}
    const groups=new Map();for(const rule of shown){const key=rule.scope==='global'?'':rule.project_id;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(rule);}
    for(const [key,items] of groups){
      toolSection(root,key?'Проект · '+ruleProjectName(key):'Глобальные',items.length);root.append(make('p','rules-section-note',key?'Действуют при работе с проектом '+key+'.':'Действуют во всех чатах, привязанных к этому workspace.'));
      const grid=make('div','rules-grid');items.forEach(rule=>grid.append(renderRuleCard(rule)));root.append(grid);
    }
  }


  // Agents: who Richi works with on this computer and where each finds skills (richi skill-sources).
  const agentsState={data:null,loading:false,error:'',request:0,query:'',selected:'',addingFolder:'',folderDraft:'',focus:'',busy:false,problem:''};
  let agentsController=null;
  const agentsNote=()=>rulesState.editing?'Агенты · изменения сохраняются для этого компьютера':'Агенты · режим команд';
  const agentItems=()=>list(agentsState.data?.agents).filter(agent=>agent&&typeof agent.id==='string');
  const plural=(n,forms)=>{const a=n%10,b=n%100;return n.toLocaleString('ru-RU')+' '+(a===1&&b!==11?forms[0]:a>=2&&a<=4&&(b<12||b>14)?forms[1]:forms[2]);};
  const skillForms=['скилл','скилла','скиллов'];
  const agentSaved='Изменения сразу сохраняются в настройках Richi на этом компьютере и действуют во всех workspace.';
  const agentCommandHint='Выполните в терминале или передайте агенту. Команда меняет настройки агентов на этом компьютере.';
  // The numbers a form may accept come with the agents answer; the fallback serves a page that has none yet.
  const agentLimit=(name,fallback)=>Number(agentsState.data?.limits?.[name])||fallback;
  const agentFolderProblem=value=>!value?'Укажите папку со скиллами.':!agentFolderOk(value,agentLimit('folder',1000))?'Нужен полный путь или путь от домашней папки: /opt/skills или ~/skills.':'';
  // Rules that give this agent a skill, and whether the agent really has it.
  function agentRules(agent){
    const known=skillsKnown();
    return ruleList().map(rule=>({rule,skill:installedRuleSkill(ruleSkillFor(rule,agent.id),agent.id)})).filter(item=>item.skill).map(item=>{const info=rulesState.skillMap.get(item.skill);return {...item,missing:known&&(!info||!list(info.agents).includes(agent.id))};});
  }
  function agentMatches(agent){
    const query=normalized(agentsState.query);if(!query)return true;const fields=[agent.id,agent.name,...list(agent.sources).map(source=>source.path)].map(normalized);
    return query.split(/\s+/).filter(Boolean).every(word=>fields.some(field=>field.includes(word)));
  }
  async function loadAgents(){
    if(agentsController)agentsController.abort();const request=++agentsState.request,sequence=++agentSequence,sent=ruleSecret;agentsState.loading=true;agentsState.error='';renderAgents();
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),20000);agentsController=controller;
    try{
      const response=await fetch('/api/agents',{cache:'no-store',headers:sent?{[RULE_HEADER]:sent}:{},signal:controller.signal});let data=null;try{data=await response.json();}catch{data=null;}
      if(!response.ok)throw new Error(data&&data.error||'Сервер вернул HTTP '+response.status);if(!data||!Array.isArray(data.agents))throw new Error('Неожиданный формат ответа сервера');
      if(request!==agentsState.request)return;
      if(sent!==ruleSecret){agentsState.loading=false;loadAgents();return;}
      setAgents(data,sequence);setMapEditing(data.editing===true,sent);
    }catch(error){if(request!==agentsState.request)return;agentsState.error=error.name==='AbortError'?'Сервер не ответил за 20 секунд.':error.message;}
    finally{clearTimeout(timeout);if(request===agentsState.request){agentsState.loading=false;agentsController=null;if(state.mode==='agents')renderAgents();}}
  }
  function agentChangeProblem(error){return editKeyProblem(error)||agentRefusal(error,agentsState.data?.settings_file);}
  // An agents answer: the list, and the skill catalog it was counted from.
  function setAgents(data,sequence){const {catalog,...listing}=data;agentsState.data=listing;if(catalog&&Array.isArray(catalog.skills))setSkills(catalog,sequence);setMapAgents(listing.agents,sequence);refreshRulePicker();}
  // The answer to a saved change is the newest list: a list still on its way is older and is dropped.
  function acceptAgents(data){agentsState.request++;if(agentsController){agentsController.abort();agentsController=null;}agentsState.loading=false;agentsState.error='';setAgents(data,++agentSequence);}
  // Closing a saving form lets another form start. The server commits one agent change at a time
  // but can deliver their full listings in reverse order; keep this page's writes ordered through receipt.
  let agentSave=Promise.resolve();
  function saveAgentChange(change){
    const save=agentSave.then(async()=>{
      const answer=await sendMapChange('/api/agents',change);
      acceptAgents(answer.listing);
    });
    agentSave=save.catch(()=>{});
    return save;
  }
  const closeFolderForm=()=>{agentsState.addingFolder='';agentsState.folderDraft='';};
  function openAgentCommand(change,title='Команда для терминала'){
    const {dialog,body,foot}=ruleDialog(title,'Вкладка открыта без ключа редактирования, поэтому изменение нужно выполнить командой.');
    ruleCommandBox(body,agentCommandHint)(agentCommand(change));foot.append(toolButton('Закрыть',()=>dialog.close(),'primary'));dialog.showModal();
  }
  // Save a change made right in the tab; without the edit key it becomes a command.
  async function applyAgentChange(change,done='Настройки агента сохранены'){
    if(agentsState.busy)return false;
    const agent=agentItems().find(item=>item.id===change.id);if(agent?.updated_at&&change.action!=='add')change.expected_updated_at=agent.updated_at;
    if(!rulesState.editing){openAgentCommand(change);return false;}
    agentsState.busy=true;agentsState.problem='';renderAgents();
    try{
      await saveAgentChange(change);announce(done);return true;
    }catch(error){
      agentsState.problem=agentChangeProblem(error);if(error.code==='conflict'||error.code==='not_found')await loadAgents();
      // A form the user opened meanwhile is theirs: the command waits in the tab's message instead.
      if(!rulesState.editing&&!$('rule-dialog').open)openAgentCommand(change);return false;
    }
    finally{agentsState.busy=false;renderAgents();}
  }
  function openAgentEditor(agent=null){
    // opened: the agent this form was built from; agent: the latest one known, whose version a save names.
    const opened=agent;
    const panel=ruleDialog(agent?'Изменить агента':'Новый агент',agent?agent.name+' · '+agent.id:'Опишите агента, которого в Richi нет, чтобы правила могли называть скилл и для него.'),{dialog,form,body,foot}=panel;
    const name=make('input'),folder=make('input'),id=make('input'),variable=make('input'),manual=make('select');let idTouched=false;
    for(const input of [name,folder,id,variable]){input.type='text';input.autocomplete='off';input.spellcheck=false;}
    // maxLength counts UTF-16 units, so it only stops a runaway paste; change() counts characters.
    name.maxLength=2*agentLimit('name',60);name.placeholder='Gemini CLI';folder.maxLength=2*agentLimit('folder',1000);folder.placeholder='~/.gemini/skills';id.maxLength=32;id.placeholder='gemini-cli';variable.maxLength=165;variable.placeholder='GEMINI_CLI=1';
    manual.add(new Option('Показывать все скиллы','none'));manual.add(new Option('Скрывать скиллы с disable-model-invocation: true','front-matter'));
    const taken=()=>new Set(agentItems().map(item=>item.id));
    const slug=value=>{let base=ruleSlug(value).slice(0,28).replace(/-+$/,'');if(!/^[a-z]/.test(base))return '';let result=base,index=2;while(taken().has(result))result=base+'-'+index++;return result;};
    if(agent){name.value=agent.name;variable.value=list(agent.variables)[0]||'';manual.value=agent.manual_skills==='front-matter'?'front-matter':'none';}
    else{
      const presets=list(agentsState.data?.presets).filter(preset=>preset&&typeof preset.id==='string').sort((a,b)=>Number(b.exists)-Number(a.exists));
      if(presets.length){
        const row=make('div','agent-presets');row.setAttribute('role','group');row.setAttribute('aria-label','Известные агенты');
        for(const preset of presets){const button=toolButton(preset.name,()=>{name.value=preset.name;folder.value=preset.folder;id.value=preset.id;idTouched=true;problem('');name.focus();},'agent-preset'+(preset.exists?' found':''));button.title=preset.exists?'Папка скиллов найдена на этом компьютере: '+preset.folder:preset.folder;row.append(button);}
        const field=make('div','rule-field');field.append(make('span','field-label','Начать с известного агента'),row,make('span','rule-field-hint',presets.some(preset=>preset.exists)?'Выделены агенты, чья папка скиллов уже есть на этом компьютере.':'Имя, ID и папка скиллов подставятся сами; их можно поправить.'));body.append(field);
      }
      name.addEventListener('input',()=>{if(!idTouched)id.value=slug(name.value);});id.addEventListener('input',()=>{idTouched=true;});
    }
    body.append(ruleField('Имя',name,'Так агент называется в правилах и на этой вкладке.'));
    if(!agent)body.append(ruleField('Папка со скиллами',folder,'Папка, где каждый скилл лежит в своей подпапке с файлом SKILL.md. Ещё папки можно добавить позже.'));
    const more=make('details'),summary=make('summary','','Дополнительно');more.append(summary);
    if(!agent)more.append(ruleField('ID',id,'Короткое постоянное имя для команд и правил, например --skill gemini-cli=review. Подставляется из имени; после создания не меняется.'));
    more.append(ruleField('Переменная окружения',variable,'Необязательно. Если терминал агента задаёт свою переменную, Richi поймёт по ней, какой агент запрашивает правила. Без неё агент называет себя сам: richi rules show --agent ID.'),
      ruleField('Скиллы только по вызову пользователя',manual,'Скилл, который агент не выбирает сам, правило использовать не сможет, поэтому в списках он не нужен.'));
    body.append(more);
    const problem=ruleProblem(body),showCommand=ruleCommandBox(body,agentCommandHint),cancel=toolButton('Отмена',()=>dialog.close()),submit=make('button','primary',rulesState.editing?(agent?'Сохранить':'Добавить агента'):'Показать команду');submit.type='submit';foot.append(cancel,submit);
    function change(){
      const label=name.value.trim(),mark=variable.value.trim();
      if(!label)return 'Введите имя агента.';
      if(!agentNameOk(label,agentLimit('name',60)))return 'Имя: одна строка до '+agentLimit('name',60)+' символов.';
      if(mark&&!AGENT_VARIABLE.test(mark)){more.open=true;return 'Переменная: ИМЯ или ИМЯ=ЗНАЧЕНИЕ, имя из латинских букв, цифр и «_».';}
      if(agent)return agentUpdate(opened,agent,{name:label,variable:mark,manual_skills:manual.value})||'Изменений нет.';
      const identifier=id.value.trim()||slug(label),path=folder.value.trim(),wrong=agentFolderProblem(path);
      if(wrong)return wrong;
      if(!AGENT_ID.test(identifier)){more.open=true;return 'ID: до 32 символов — строчные латинские буквы, цифры и дефис, начиная с буквы.';}
      if(taken().has(identifier)){more.open=true;return 'Агент с ID «'+identifier+'» уже есть.';}
      const add={action:'add',id:identifier,name:label,folders:[path],manual_skills:manual.value};if(mark)add.variable=mark;return add;
    }
    form.addEventListener('submit',async event=>{
      event.preventDefault();problem('');const request=change();if(typeof request==='string'){problem(request);return;}
      if(!rulesState.editing){showCommand(agentCommand(request));return;}
      await saveFromForm(panel,submit,{busy:'Сохраняем…',send:()=>saveAgentChange(request),explain:agentChangeProblem,problem,
        saved:()=>{agentsState.selected=request.id;agentsState.problem='';announce(agent?'Агент изменён':'Агент добавлен');renderAgents();},
        // Somebody changed the agent meanwhile: take its new version and keep what is typed here.
        failed:async error=>{
          if(!agent||error.code!=='conflict')return '';
          await loadAgents();const fresh=agentItems().find(item=>item.id===agent.id);
          if(!fresh)return 'Агента удалили, пока форма была открыта. Закройте форму и добавьте его заново.';
          if(fresh.updated_at===agent.updated_at)return '';
          const target=agentTargetProblem(opened,fresh);if(target){agent=fresh;return target;}
          const differs=agentDifference(agent,fresh);agent=fresh;
          return 'Настройки агента изменили, пока форма была открыта'+(differs?'; теперь у него '+differs:'')+'. Нажмите «Сохранить» ещё раз: запишется только то, что вы изменили в этой форме, остальное останется как в новой версии.';
        },
        lost:message=>{agentsState.problem=message;renderAgents();},commands:()=>{showCommand(agentCommand(request));renderAgents();},
        label:()=>{submit.textContent=rulesState.editing?(agent?'Сохранить':'Добавить агента'):'Показать команду';}});
    });
    dialog.showModal();name.focus();
  }
  function openAgentRemoval(agent){
    const rules=agentRules(agent).length,reset=agent.builtin;
    const panel=ruleDialog(reset?'Сбросить к встроенным настройкам?':'Удалить агента?',agent.name+' · '+agent.id),{dialog,form,body,foot}=panel;
    body.append(make('p','rule-confirm',reset?'Выключенные источники скиллов снова включатся, а добавленные папки исчезнут из списка. Сами папки и скиллы на диске не меняются.':
      'Richi перестанет учитывать этого агента: он пропадёт из форм правил и списков скиллов. Его папки и скиллы на диске не меняются.'+(rules?' Правила, которые называют скилл для него ('+rules+'), останутся как есть.':'')));
    const problem=ruleProblem(body),showCommand=ruleCommandBox(body,agentCommandHint),cancel=toolButton('Отмена',()=>dialog.close()),label=reset?'Сбросить':'Удалить агента',submit=make('button',rulesState.editing?(reset?'primary':'danger solid'):'primary',rulesState.editing?label:'Показать команду');submit.type='submit';foot.append(cancel,submit);
    form.addEventListener('submit',async event=>{
      event.preventDefault();problem('');const request={action:'remove',id:agent.id,expected_updated_at:agent.updated_at};
      if(!rulesState.editing){showCommand(agentCommand(request));return;}
      await saveFromForm(panel,submit,{send:()=>saveAgentChange(request),explain:agentChangeProblem,problem,
        saved:()=>{agentsState.problem='';announce(reset?'Настройки агента сброшены':'Агент удалён');renderAgents();},
        failed:async error=>{
          if(error.code!=='conflict'&&error.code!=='not_found')return '';
          await loadAgents();
          return 'Настройки изменились, пока вы подтверждали удаление. Закройте форму и проверьте актуальные настройки агента перед новым подтверждением.';
        },
        lost:message=>{agentsState.problem=message;renderAgents();},commands:()=>{submit.className='primary';showCommand(agentCommand(request));renderAgents();},
        label:()=>{submit.textContent=rulesState.editing?label:'Показать команду';}});
    });
    dialog.showModal();cancel.focus();
  }
  function renderAgentDetail(agent){
    const box=make('section','agent-detail'),head=make('div','agent-head'),titles=make('div'),actions=make('div','agent-head-actions'),heading=make('h3','',agent.name);box.setAttribute('aria-label','Агент '+agent.name);
    const idLine=make('code','agent-id',agent.id);idLine.title='ID агента: так он называется в командах и правилах';titles.append(heading,idLine);
    if(!agent.builtin)actions.append(keyed(toolButton('Изменить',()=>openAgentEditor(agent),'small'),'edit:'+agent.id),keyed(toolButton('Удалить',()=>openAgentRemoval(agent),'small danger'),'remove-agent:'+agent.id));
    else if(agent.updated_at)actions.append(keyed(toolButton('Сбросить к встроенным',()=>openAgentRemoval(agent),'small'),'reset:'+agent.id));
    head.append(titles,actions);box.append(head);
    const badges=make('div','agent-badges');badges.append(make('span','agent-badge'+(agent.builtin?'':' own'),agent.builtin?'Встроенный':'Свой'));
    // null: every source of the agent is switched off, and Richi does not look at those.
    badges.append(make('span','agent-badge'+(agent.detected?' ok':''),agent.detected===null?'Не проверялся: источники выключены':agent.detected?'Найден на компьютере':'Не найден на компьютере'));
    if(agent.connection==='configured')badges.append(make('span','agent-badge ok','Richi подключён'));
    else if(agent.connection==='not_configured'){const badge=make('span','agent-badge warn','Richi не подключён');badge.title='Подключить: richi setup --agent '+agent.id+' --yes';badges.append(badge);}
    else if(agent.connection){const badge=make('span','agent-badge warn','Подключение Richi нужно проверить');badge.title='Проверить: richi setup --agent '+agent.id+' --status';badges.append(badge);}
    else if(agent.builtin&&agentsState.data?.connections_problem){const badge=make('span','agent-badge','Подключение Richi неизвестно');badge.title=agentsState.data.connections_problem;badges.append(badge);}
    if(agent.builtin&&agent.updated_at)badges.append(make('span','agent-badge own','Есть ваши изменения'));
    box.append(badges);
    if(agentsState.problem){const error=make('div','rule-error',agentsState.problem);error.setAttribute('role','alert');error.style.marginTop='14px';box.append(error);}
    box.append(make('div','agent-section','Откуда брать скиллы'));
    for(const source of list(agent.sources)){
      const row=make('div','agent-source'+(source.enabled?'':' off')),toggle=make('input'),text=make('div'),label=agentSourceLabel(agent,source);
      toggle.type='checkbox';toggle.className='agent-switch';toggle.setAttribute('role','switch');toggle.checked=source.enabled!==false;toggle.disabled=agentsState.busy;keyed(toggle,'source:'+agent.id+':'+source.key);toggle.setAttribute('aria-label',label+': '+source.path);
      toggle.addEventListener('change',async()=>{const enabled=toggle.checked;if(!await applyAgentChange({action:'update',id:agent.id,[enabled?'enable':'disable']:[source.key]},enabled?'Источник включён':'Источник выключен'))toggle.checked=!enabled;});
      text.append(make('div','agent-source-name',label),make('div','agent-source-path',source.path));
      const count=make('span','agent-source-count',!source.enabled?'выключено':source.denied?'нет доступа':!source.exists?'папки нет':plural(Number(source.skills)||0,skillForms)+(source.truncated?' (по части папки)':''));
      row.append(toggle,text,count);
      if(source.added&&(agent.builtin||list(agent.sources).length>1)){const remove=keyed(toolButton('×',async()=>{if(await applyAgentChange({action:'update',id:agent.id,remove_folders:[source.key]},'Папка убрана'))focusByKey($('agents-content'),'add-folder:'+agent.id);},'icon'),'remove:'+agent.id+':'+source.key);remove.title='Убрать папку из источников';remove.setAttribute('aria-label','Убрать папку '+source.path);remove.disabled=agentsState.busy;row.append(remove);}
      else row.append(make('span'));
      box.append(row);
    }
    if(agentsState.addingFolder===agent.id){
      const formBox=make('form','agent-folder-form'),field=make('div','rule-field'),line=make('div','agent-folder-row'),input=make('input'),problem=make('div','rule-error');input.type='text';input.id='agent-folder-input';keyed(input,'folder-input');input.maxLength=2*agentLimit('folder',1000);input.placeholder='~/work/team-skills';input.autocomplete='off';input.spellcheck=false;input.setAttribute('aria-label','Папка со скиллами');input.value=agentsState.folderDraft;problem.hidden=true;problem.setAttribute('role','alert');
      const add=make('button','primary',agentsState.busy?'Сохраняем…':rulesState.editing?'Добавить':'Показать команду');add.type='submit';add.disabled=agentsState.busy;
      line.append(input,add,keyed(toolButton('Отмена',()=>{closeFolderForm();agentsState.focus='add-folder:'+agent.id;renderAgents();}),'folder-cancel'));
      field.append(line,make('span','rule-field-hint','Папка, где каждый скилл лежит в своей подпапке с файлом SKILL.md.'),problem);formBox.append(field);formBox.noValidate=true;
      // The page is rebuilt whenever data arrives; what is being typed lives in the state, not in the field.
      input.addEventListener('input',()=>{agentsState.folderDraft=input.value;problem.hidden=true;});
      formBox.addEventListener('submit',async event=>{event.preventDefault();if(agentsState.busy)return;const value=input.value.trim(),wrong=agentFolderProblem(value);if(wrong){problem.textContent=wrong;problem.hidden=false;return;}
        if(await applyAgentChange({action:'update',id:agent.id,add_folders:[value]},'Папка добавлена')){closeFolderForm();agentsState.focus='add-folder:'+agent.id;renderAgents();}});
      box.append(formBox);
    }else{const row=make('div','agent-row-actions');row.append(keyed(toolButton('+ Добавить папку',()=>{agentsState.addingFolder=agent.id;agentsState.folderDraft='';agentsState.focus='folder-input';agentsState.problem='';renderAgents();},'small'),'add-folder:'+agent.id));box.append(row);}
    box.append(make('div','agent-section','Как Richi узнаёт агента'));
    const lines=make('div','agent-lines'),line=(label,value)=>{const item=make('div');item.append(document.createTextNode(label+' '),make('code','',value));lines.append(item);};
    if(agent.chat_prefix!==false)line('Идентификатор чата:','RICHI_CHAT_ID='+agent.id+':…');
    for(const value of list(agent.variables))line('Переменная в терминале агента:',value);
    line('Явно в команде:','richi rules show --agent '+agent.id);box.append(lines);
    box.append(make('div','agent-muted',agent.builtin?'Задано в Richi и обновляется вместе с ним. По этим признакам команда rules show отдаёт агенту его скилл.':'По этим признакам команда rules show отдаёт агенту его скилл. Чтобы агент загружал правила, добавьте эту команду в его постоянные инструкции.'));
    box.append(make('div','agent-section','Правила со скиллом для этого агента'));
    const rules=agentRules(agent);
    if(!rulesState.data)box.append(make('div','agent-muted',rulesState.error?'Правила не загрузились: '+rulesState.error:'Загружаем правила…'));
    else if(!rules.length)box.append(make('div','agent-muted','Правила пока не называют скилл для этого агента.'));
    else for(const item of rules.slice(0,12)){
      const row=toolButton('',()=>{setMode('rules');$('search').value=item.rule.id;rulesState.query=item.rule.id;renderRules();},'agent-rule'),skill=make('span',item.missing?'agent-warn':'',item.skill+(item.missing?' · у агента такого нет':''));
      row.append(make('code','',item.rule.id),skill);keyed(row,'agent-rule:'+item.rule.id);row.setAttribute('aria-label','Открыть правило '+item.rule.id);box.append(row);
    }
    if(rules.length>12)box.append(make('div','agent-muted','И ещё '+(rules.length-12)+'. Все правила — на вкладке «Правила».'));
    return box;
  }
  function renderAgents(){
    const root=$('agents-content'),active=document.activeElement,inside=Boolean(active)&&root.contains(active);
    // The user moved elsewhere while a control waited to get focus back: leave it where they put it.
    if(!inside&&active&&active!==document.body)agentsState.focus='';
    const key=agentsState.focus||(inside&&active.dataset.focus)||'',caret=inside&&active.id==='agent-folder-input'?[active.selectionStart,active.selectionEnd]:null;
    renderAgentsContent();
    if(!key||$('rule-dialog').open)return;
    const target=focusByKey(root,key);
    if(target){agentsState.focus='';if(caret&&target.id==='agent-folder-input')target.setSelectionRange(caret[0],caret[1]);}
    // A control is disabled while its change is being saved and takes focus back afterwards.
    else if(agentsState.busy)agentsState.focus=key;
    else{agentsState.focus='';focusByKey(root,'item:'+agentsState.selected,'add-agent');}
  }
  function renderAgentsContent(){
    const root=$('agents-content');root.replaceChildren();$('agents-panel').setAttribute('aria-busy',String(agentsState.loading));if(state.mode==='agents')$('data-note').textContent=agentsNote();
    const all=agentItems(),heading=make('div','tools-heading rules-heading'),intro=make('div'),count=make('div','tools-total');
    intro.append(make('div','tools-eyebrow','Кто работает с памятью'),make('h2','','Агенты'),make('p','tools-lead','Richi учитывает этих агентов на компьютере: правило может назвать скилл для всех сразу или свой для каждого, а агент получает именно свой. Встроенных агентов Richi настраивает и обновляет сам. Здесь можно выключить источник скиллов, добавить папку или описать ещё одного агента.'));
    count.append(make('strong','',String(all.length)),make('span','',all.length%10===1&&all.length%100!==11?'агент':all.length%10>=2&&all.length%10<=4&&(all.length%100<12||all.length%100>14)?'агента':'агентов'));heading.append(intro,count);root.append(heading);
    if(!agentsState.data){
      const failed=Boolean(agentsState.error)&&!agentsState.loading,box=make('div','tools-empty');box.append(ruleMark('rule'),make('h3','',failed?'Не удалось загрузить агентов':'Загружаем агентов'),make('p','',failed?agentsState.error:'Читаем настройки и считаем скиллы.'));
      if(failed)box.append(toolButton('Повторить загрузку',()=>loadAgents(),'primary'));root.append(box);return;
    }
    if(agentsState.error)toolWarning(root,'Не удалось обновить список агентов: '+agentsState.error+' Показан предыдущий список.');
    if(agentsState.data.problem)toolWarning(root,agentSettingsProblem(agentsState.data.problem_code,agentsState.data.settings_file));
    for(const item of list(agentsState.data.unknown_agents).filter(item=>item&&typeof item.id==='string'))toolWarning(root,unknownAgentText(item));
    const moreUnknown=Number(agentsState.data.unknown_agents_more)||0;
    if(moreUnknown>0)toolWarning(root,'И ещё '+plural(moreUnknown,['запись','записи','записей'])+' в настройках агентов эта версия Richi не использует. Полный список — в файле '+(agentsState.data.settings_file||'agents.json')+'.');
    renderRuleMode(root,agentSaved);
    const shown=all.filter(agentMatches),layout=make('div','agents-layout'),column=make('div','agents-list');
    if(!shown.some(agent=>agent.id===agentsState.selected))agentsState.selected=shown[0]?.id||'';
    for(const agent of shown){
      const rules=agentRules(agent),missing=rules.filter(item=>item.missing).length,button=toolButton('',()=>{agentsState.selected=agent.id;closeFolderForm();agentsState.problem='';renderAgents();},'agent-item');
      button.setAttribute('aria-pressed',String(agent.id===agentsState.selected));button.dataset.agent=agent.id;keyed(button,'item:'+agent.id);
      button.append(make('strong','',agent.name),make('code','',agent.id),make('span','',plural(Number(agent.skills)||0,skillForms)+(list(agent.sources).some(source=>source.truncated)?' (по части каталога)':'')+' · '+plural(rules.length,['правило','правила','правил'])));
      if(missing)button.append(make('span','warn',plural(missing,['правило','правила','правил'])+' со скиллом, которого у агента нет'));
      column.append(button);
    }
    if(!shown.length)column.append(make('div','agent-muted','По запросу «'+agentsState.query+'» агентов нет.'));
    column.append(keyed(toolButton('+ Добавить агента',()=>openAgentEditor(),'agent-add'),'add-agent'));layout.append(column);
    const selected=shown.find(agent=>agent.id===agentsState.selected);
    if(selected)layout.append(renderAgentDetail(selected));
    root.append(layout);
  }
