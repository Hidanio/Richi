'use strict';
  const $ = id => document.getElementById(id);
  const NS = 'http://www.w3.org/2000/svg';
  const kindNames = {project:'Проекты',task:'Задачи',decision:'Решения',fact:'Факты',note:'Заметки',investigation:'Исследования',experiment:'Эксперименты',procedure:'Процедуры',service:'Сервисы',metric:'Метрики',concept:'Понятия',source:'Источники',document:'Документы',api:'API',dataset:'Наборы данных',table:'Таблицы',person:'Люди',team:'Команды',artifact:'Артефакты',system:'Системы',entity:'Сущности',repository:'Репозитории',component:'Компоненты',tool:'Инструменты',jira:'Jira'};
  const singularNames = {project:'Проект',task:'Задача',decision:'Решение',fact:'Факт',note:'Заметка',investigation:'Исследование',experiment:'Эксперимент',procedure:'Процедура',service:'Сервис',metric:'Метрика',concept:'Понятие',source:'Источник',document:'Документ',api:'API',dataset:'Набор данных',table:'Таблица',person:'Человек',team:'Команда',artifact:'Артефакт',system:'Система',entity:'Сущность',repository:'Репозиторий',component:'Компонент',tool:'Инструмент',jira:'Jira'};
  const stateNames = {confirmed:'Подтверждено',hypothesis:'Гипотеза',superseded:'Устарело'};
  const workNames = {planned:'Запланировано',in_progress:'В работе',implemented:'Реализовано',merged:'Влито',released:'Выпущено',blocked:'Заблокировано',done:'Завершено'};
  const relationNames = {depends_on:'зависит от',related_to:'связано с',implements:'реализует',explains:'объясняет',supersedes:'заменяет',variant_of:'вариант понятия',used_in:'используется в',contradicts:'противоречит',uses:'использует',uses_tool:'применяет инструмент',produces:'производит',consumes:'потребляет',part_of:'часть',belongs_to:'относится к',belongs_to_project:'относится к проекту',documented_by:'описано в',supported_by:'подтверждается',supports:'подтверждает',affects:'влияет на',references:'ссылается на',has_entry:'содержит запись',associated_with:'связано с',entry_project:'относится к проекту'};
  const state = {mode:'map',mapQuery:'',conceptQuery:'',conceptProject:'',toolQuery:'',toolFilter:'',toolId:'',toolProject:'',toolRecord:null,toolsData:null,toolsLoading:false,toolsError:'',toolsRequest:0,toolsFlags:'',toolsRequestedFlags:'',toolDetails:new Map(),toolDetailLoading:false,toolDetailError:'',toolDetailMissing:'',toolDetailRequest:0,truncated:false,lastError:'',nodes:[],edges:[],nodeMap:new Map(),edgeMap:new Map(),adjacency:new Map(),kinds:new Set(),disabledKinds:new Set(),focus:null,selection:null,depth:1,positions:new Map(),visibleNodes:[],visibleEdges:[],camera:{x:0,y:0,k:1},loaded:false,loading:false,request:0,suppressClick:false,sidebarOpen:true};
  const svg = $('graph');
  const make = (tag,cls,text) => {const el=document.createElement(tag);if(cls)el.className=cls;if(text!==undefined)el.textContent=String(text);return el;};
  const makeSvg = (tag,attrs={}) => {const el=document.createElementNS(NS,tag);for(const [k,v]of Object.entries(attrs))el.setAttribute(k,String(v));return el;};
  const titleFor = node => node ? String(node.title || node.id) : 'Неизвестный узел';
  const kindFor = node => String(node.kind || (String(node.id).startsWith('project:')?'project':'entity'));
  const groupFor = node => String(node.id).startsWith('project:')?'project':String(node.id).startsWith('entry:')?'record':'entity';
  const colorFor = node => `var(--${groupFor(node)})`;
  const kindLabel = (kind,singular=false) => (singular?singularNames:kindNames)[kind] || kind;
  const edgeLabel = edge => relationNames[edge.kind] || edge.kind || 'связано с';
  const cut = (value,length=24) => {const chars=Array.from(String(value));return chars.length>length?chars.slice(0,length-1).join('')+'…':chars.join('');};
  const cutMiddle = (value,length=26) => {const chars=Array.from(String(value));if(chars.length<=length)return chars.join('');const head=8,tail=length-head-1;return chars.slice(0,head).join('')+'…'+chars.slice(-tail).join('');};
  const list = value => Array.isArray(value)?value:[];
  const sortNodes = (a,b) => titleFor(a).localeCompare(titleFor(b),'ru') || String(a.id).localeCompare(String(b.id));
  const dateText = value => {if(!value)return 'не указана';if(/^\d{4}-\d{2}-\d{2}$/.test(String(value))){const [year,month,day]=String(value).split('-');return day+'.'+month+'.'+year;}const date=new Date(value);return Number.isNaN(date.getTime())?String(value):date.toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'});};
  const announce = text => {$('announcer').textContent=text;};
  const safeUrl = reference => {try{const u=new URL(reference);return ['http:','https:','codex:'].includes(u.protocol)?u.href:null;}catch{return null;}};

  function addSection(parent,title){const h=make('h3','',title);parent.append(h);return parent;}
  function addBadge(parent,label,cls=''){parent.append(make('span','badge '+cls,label));}
  function addMeta(parent,label,value){if(!value)return;const row=make('div','meta-row');row.append(make('span','',label),make('code','',value));parent.append(row);}
  function addSources(parent,sources,owner=null){
    addSection(parent,'Источники');
    if(!list(sources).length){parent.append(make('div','detail-empty','Источники не указаны.'));return;}
    for(const [sourceOffset,item] of sources.entries()){const source=typeof item==='string'?{reference:item}:item;if(!source || !source.reference)continue;
      const block=make('div','source');const main=make('div','source-main');const ref=String(source.reference);const url=safeUrl(ref);
      if(url){const link=make('a','source-ref',source.label || ref);link.href=url;link.target='_blank';link.rel='noopener noreferrer';link.title=ref;main.append(link);}
      else {const text=make('span','source-ref',source.label?source.label+'\n'+ref:ref);const copy=make('button','small','Копировать');copy.type='button';copy.title='Копировать ссылку или путь';copy.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(ref);copy.textContent='Скопировано';setTimeout(()=>copy.textContent='Копировать',1600);}catch{copy.textContent='Выделите текст';}});main.append(text,copy);}
      block.append(main);const meta=[source.type,source.observed_at?'Проверено: '+dateText(source.observed_at):null].filter(Boolean);if(meta.length)block.append(make('div','source-meta',meta.join(' · ')));addGitSource(block,source,sourceOffset+1,owner);parent.append(block);
    }
  }
  const gitActionNames={show:'Код',history:'История',diff:'Diff',check:'Проверить',commit:'Файлы коммита'};
  let gitViewer=null,gitRequest=0,gitController=null;
  function nodeSourceOwner(node){return node&&/^(entry|entity):/.test(node.id)&&node.updated_at?{ref:node.id,updated_at:node.updated_at,title:titleFor(node)}:null;}
  function edgeSourceOwner(edge){
    const id=edge.provenance?.record_id;
    if(edge.origin==='explicit'&&id&&edge.updated_at)return {ref:'edge:'+id,updated_at:edge.updated_at,title:edgeLabel(edge)};
    if(edge.origin==='entry_project'&&id)return nodeSourceOwner(state.nodeMap.get('entry:'+id));
    return null;
  }
  function hasGitAnchor(source){const g=source?.git;return g&&g.version===1&&typeof g.repo_id==='string'&&typeof g.path==='string'&&/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(g.commit)&&['commit','worktree'].includes(g.mode)&&typeof g.dirty==='boolean';}
  function legacyGitReference(source){
    if(source?.git||typeof source?.reference!=='string')return null;
    const match=/^git:([0-9a-f]{40}|[0-9a-f]{64})(?::(.+))?$/.exec(source.reference);
    if(!match)return null;
    const path=match[2]||null;
    if(path&&(path.startsWith('/')||path.includes('\\')||/[\x00-\x1f\x7f]/.test(path)||path.split('/').some(part=>!part||part==='.'||part==='..'||part.toLowerCase()==='.git')))return null;
    return {commit:match[1],path};
  }
  function addGitSource(parent,source,index,owner){
    const anchored=hasGitAnchor(source),legacy=legacyGitReference(source);
    if(!anchored&&!legacy){if(String(source?.reference||'').startsWith('git:'))parent.append(make('div','source-meta','Для просмотра нужна ссылка git:полный_SHA или git:полный_SHA:путь/к/файлу.'));return;}
    const g=anchored?source.git:legacy,box=make('div','source-git');box.append(make('code','',g.path||'Коммит '+g.commit.slice(0,12)));
    box.append(make('div','source-git-label',anchored?g.repo_id+' · '+g.commit.slice(0,12)+' · '+(g.mode==='worktree'?(g.dirty?'снимок с правками':'снимок рабочей копии'):'коммит'):'Git-ссылка · '+g.commit.slice(0,12)+' · репозиторий определяется при открытии'));
    if(owner){const actions=make('div','source-git-actions');const names=legacy&&!legacy.path?['commit']:['show','history','diff','check'];for(const action of names){const label=gitActionNames[action],button=make('button','small',label);button.type='button';button.dataset.gitSource=String(index);button.dataset.gitAction=action;button.title=label+' · '+(g.path||g.commit)+' · источник '+index;button.addEventListener('click',()=>openGit(source,index,owner,action,button));actions.append(button);}box.append(actions);}
    else box.append(make('div','source-meta','Для просмотра откройте карточку владельца источника.'));
    parent.append(box);
  }
  function stopGitRequest(){gitRequest++;if(gitController)gitController.abort();gitController=null;}
  function closeGit(){stopGitRequest();const trigger=gitViewer?.trigger;gitViewer=null;if($('git-dialog').open)$('git-dialog').close();if(trigger?.isConnected)trigger.focus({preventScroll:true});}
  function updateGitHeading(){
    if(!gitViewer)return;const viewer=gitViewer,g=viewer.source.git||viewer.legacy,commitView=viewer.action==='commit';
    $('git-title').textContent=commitView?'Файлы коммита':g.path||viewer.selectedPath||'Git-источник';
    $('git-repository').textContent='Git · '+(g.repo_id||viewer.resolvedRepoId||'репозиторий ещё не определён');$('git-owner').textContent=cut(viewer.owner.title,200)+' · источник '+viewer.index;
    const captured=$('git-captured');captured.replaceChildren(document.createTextNode(g.mode==='worktree'?'Базовый коммит снимка: ':viewer.legacy?'Коммит из источника: ':'Сохранённый коммит: '),make('code','',commitView?viewer.legacy.commit:g.commit));
    captured.append(make('div','',viewer.legacy?(commitView?'Выберите файл, изменённый этим коммитом. Привязка определяется при чтении; память не изменяется.':'Код из коммита, указанного в источнике. Привязка определяется при чтении; память не изменяется.'):g.mode==='worktree'?(g.dirty?'Сохранены байты рабочей копии с незакоммиченными изменениями.':'Сохранены байты рабочей копии, совпадавшие с коммитом.'):'Сохранённая версия файла из коммита.'));
  }
  function openGit(source,index,owner,action,trigger){
    stopGitRequest();gitViewer={source,legacy:legacyGitReference(source),selectedPath:null,index,owner,trigger,action,targets:{history:'',diff:'HEAD',check:'HEAD'},worktree:false,maxChars:32000,historyLimit:20};
    if(!$('git-dialog').open)$('git-dialog').showModal();configureGitAction(action);$('git-close').focus({preventScroll:true});requestGit();
  }
  function rememberGitControls(){if(!gitViewer||['show','commit'].includes(gitViewer.action))return;gitViewer.targets[gitViewer.action]=$('git-target').value.trim();if(gitViewer.action!=='history')gitViewer.worktree=$('git-worktree').checked;}
  function configureGitAction(action){
    if(!gitViewer)return;gitViewer.action=action;const bare=gitViewer.legacy&&!gitViewer.legacy.path;
    for(const tab of $('git-dialog').querySelectorAll('[role=tab]')){const selected=tab.dataset.gitAction===action;tab.hidden=tab.dataset.gitAction==='commit'&&!bare;tab.disabled=Boolean(bare&&!gitViewer.selectedPath&&tab.dataset.gitAction!=='commit');tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;}
    $('git-result').setAttribute('aria-labelledby','git-tab-'+action);$('git-controls').hidden=['show','commit'].includes(action);$('git-worktree-label').hidden=action==='history';
    $('git-target-label').textContent=action==='history'?'История до локального ref или SHA':'Сравнить сохранённый код с локальной версией';
    $('git-target').placeholder=action==='history'?'Сохранённый коммит (по умолчанию)':'HEAD, ветка, тег или SHA';$('git-target').value=gitViewer.targets[action]||'';
    $('git-worktree').checked=gitViewer.worktree;$('git-target').disabled=action!=='history'&&gitViewer.worktree;$('git-run').textContent=action==='check'?'Проверить':'Показать';updateGitHeading();
  }
  function switchGitAction(action){if(!gitViewer)return;rememberGitControls();configureGitAction(action);requestGit();}
  function appendGitMeta(parent,label,value){if(value===undefined||value===null)return;const row=make('div');row.append(document.createTextNode(label+': '),make('code','',value));parent.append(row);}
  function gitResultMeta(result){
    const meta=make('div','git-result-meta');
    if(typeof result.target==='string')appendGitMeta(meta,'История до коммита',result.target);
    else if(result.target){appendGitMeta(meta,result.target.mode==='worktree'?'HEAD рабочей копии':'Целевой коммит',result.target.commit);appendGitMeta(meta,'Путь',result.target.path);if(result.target.mode==='worktree')meta.append(make('div','',result.target.sha256===null?'Путь в рабочей копии не найден.':result.target.dirty?'Рабочая копия содержит правки этого файла.':'Файл рабочей копии совпадает с HEAD.'));}
    const ancestry={same:'тот же коммит',ahead:'цель находится после сохранённого коммита',behind:'цель находится до сохранённого коммита',diverged:'ветки истории разошлись',unknown:'отношение коммитов не определено'};
    if(result.ancestry)meta.append(make('div','','История: '+(ancestry[result.ancestry]||result.ancestry)+'.'));
    return meta;
  }
  function gitCode(content,label,patch=false){
    const pre=make('pre','git-code');pre.tabIndex=0;pre.setAttribute('aria-label',label);
    if(patch){const lines=String(content).split('\n');if(lines[lines.length-1]==='')lines.pop();for(const line of lines){const cls=line.startsWith('@@')?'git-diff-hunk':line.startsWith('+')&&!line.startsWith('+++')?'git-diff-add':line.startsWith('-')&&!line.startsWith('---')?'git-diff-remove':'';pre.append(make('span','git-diff-line '+cls,line));}}
    else pre.textContent=String(content);
    return pre;
  }
  function gitStatus(result){
    const labels={unchanged:'Содержимое совпадает',changed:'Код изменился',deleted:'Файл отсутствует в выбранной версии',unavailable:'Не удалось сравнить источник'};
    const descriptions={unchanged:'Сохранённые байты совпадают с выбранной версией. Зависимости и состояние релиза этой проверкой не определяются.',changed:'Сохранённые байты отличаются от выбранной версии. Проверьте, влияет ли изменение на связанное знание.',deleted:'Сохранённая версия остаётся доступна во вкладке «Код». Путь в выбранной версии не найден.',unavailable:'Результат сравнения неизвестен. Проверьте доступность репозитория, Git-объектов и сохранённого снимка.'};
    const box=make('div','git-status');box.dataset.status=result.status||'unavailable';box.append(make('strong','',labels[result.status]||'Результат не определён'),make('p','',descriptions[result.status]||'Сервер не вернул состояние сравнения.'));if(result.reason)box.append(make('p','',result.reason));return box;
  }
  function renderGitResult(data,action){
    const root=$('git-result'),result=data.result;root.replaceChildren();root.scrollTop=0;
    if(!result||typeof result!=='object')throw new Error('Неожиданный формат ответа Git.');
    if(action==='check'||action==='diff')root.append(gitStatus(result));
    const meta=gitResultMeta(result);if(action==='show'){appendGitMeta(meta,'Размер сохранённого файла',typeof result.bytes==='number'?result.bytes.toLocaleString('ru-RU')+' байт':null);appendGitMeta(meta,'SHA-256',result.sha256);}if(meta.childElementCount)root.append(meta);
    if(action==='show'){
      if(result.encoding==='base64'){root.append(make('p','git-empty','Двоичный файл. Текстовый просмотр недоступен.'));if(typeof result.content==='string'&&result.content){const detail=make('details','git-binary');detail.append(make('summary','','Показать сохранённые байты в base64'),gitCode(result.content,'Содержимое файла в base64'));root.append(detail);}}
      else if(typeof result.content!=='string')root.append(make('p','git-empty','Сервер не вернул содержимое файла.'));
      else if(!result.content)root.append(make('p','git-empty',result.bytes===0?'Сохранённый файл пуст.':'Содержимое не вошло в ограничение ответа.'));
      else root.append(gitCode(result.content,'Сохранённый код'));
    }
    if(action==='commit'){
      root.append(make('h3','git-commit-title',result.subject||'(без темы)'),make('p','git-result-meta',dateText(result.authored_at)));
      const files=list(result.files),statusNames={A:'Добавлен',M:'Изменён',D:'Удалён',T:'Изменён тип',R:'Переименован',C:'Скопирован'};
      root.append(make('p','git-result-meta','Изменения относительно первого родителя коммита. Удалённые и неподдерживаемые файлы недоступны для просмотра в этой версии.'));
      if(!files.length)root.append(make('p','git-empty','В этом коммите не найдено изменённых файлов.'));
      const items=make('ol','git-history');for(const file of files){const row=make('li','git-commit');row.append(make('code','git-commit-title',file.path),make('div','git-commit-date',statusNames[file.status]||file.status||'Файл'));const open=make('button','small','Открыть файл');open.type='button';open.disabled=file.available!==true;open.title=file.available?'Код из коммита · '+file.path:file.reason||'Файл недоступен в выбранном коммите';open.addEventListener('click',()=>{if(!gitViewer)return;gitViewer.selectedPath=file.path;gitViewer.source={...gitViewer.source,git:undefined};gitViewer.targets={history:'',diff:'HEAD',check:'HEAD'};gitViewer.worktree=false;configureGitAction('show');$('git-tab-show').focus({preventScroll:true});requestGit();});if(file.available!==true)row.append(make('div','source-meta',file.reason||'Недоступен в выбранном коммите'));row.append(open);items.append(row);}root.append(items);
    }
    if(action==='history'){
      root.append(make('p','git-result-meta','Нажмите «Сравнить», чтобы увидеть diff сохранённого кода с выбранным коммитом. Переименования отслеживаются назад эвристикой Git; история новых путей вперёд не достраивается.'));
      const commits=list(result.commits);if(!commits.length)root.append(make('p','git-empty','Для этого пути до выбранного коммита история не найдена.'));
      const items=make('ol','git-history');for(const commit of commits){const row=make('li','git-commit');row.append(make('div','git-commit-title',commit.subject||'(без темы)'),make('time','git-commit-date',dateText(commit.authored_at)),make('code','',commit.commit));const compare=make('button','small','Сравнить');compare.type='button';compare.title='Diff сохранённого кода с '+commit.commit;compare.addEventListener('click',()=>{if(!gitViewer)return;rememberGitControls();gitViewer.targets.diff=commit.commit;gitViewer.worktree=false;configureGitAction('diff');$('git-tab-diff').focus({preventScroll:true});requestGit();});row.append(compare);items.append(row);}root.append(items);
    }
    if(action==='diff'){
      if(result.binary)root.append(make('p','git-empty','Двоичные данные: доступен результат сравнения байтов, текстовый diff не строится.'));
      else if(typeof result.patch==='string'&&result.patch)root.append(gitCode(result.patch,'Различия сохранённого и целевого кода',true));
      else if(result.status==='unchanged')root.append(make('p','git-empty','Различий нет.'));
      else if(result.status==='deleted')root.append(make('p','git-empty','Файл удалён; текстовых строк для отображения diff нет.'));
      else root.append(make('p','git-empty','Текстовый diff отсутствует в ответе.'));
    }
    const candidates=list(result.rename_candidates);if(candidates.length){root.append(make('p','git-result-meta','Возможные переименования, которые Git нашёл эвристически:'));const items=make('ul','git-rename');for(const item of candidates)items.append(make('li','',item.path+(item.similarity!==undefined?' · сходство '+item.similarity+'%':'')));root.append(items,make('p','git-result-meta','Путь источника автоматически не меняется.'));
    }
    if(result.shallow)root.append(make('div','git-warning','Репозиторий неглубокий (shallow): локальная история может быть неполной.'));
    const budget=data.budget||{};
    if(result.truncated||budget.truncated){
      const warning=make('div','git-warning');warning.append(make('div','','Показана только часть результата.'+(budget.content_chars_omitted?' Пропущено символов: '+budget.content_chars_omitted+'.':'')+(budget.items_omitted?' Пропущено элементов: '+budget.items_omitted+'.':'')));
      if(gitViewer&&(gitViewer.maxChars<100000||(action==='history'&&gitViewer.historyLimit<100))){const more=make('button','small','Показать больше');more.type='button';more.addEventListener('click',()=>{if(!gitViewer)return;gitViewer.maxChars=100000;if(action==='history')gitViewer.historyLimit=100;requestGit();});warning.append(more);}
      else warning.append(make('div','',action==='commit'?'Достигнут предел списка. Остальные файлы можно посмотреть через локальный Git.':action==='history'?'Достигнут предел просмотра. Для дальнейшей истории используйте CLI или локальный Git.':'Достигнут предел просмотра. Полные сохранённые байты доступны в Git или файле снимка.'));
      root.append(warning);
    }
  }
  async function requestGit(){
    if(!gitViewer)return;rememberGitControls();stopGitRequest();const request=gitRequest,viewer=gitViewer,action=viewer.action;const root=$('git-result');root.replaceChildren(make('div','git-loading','Читаем '+(action==='commit'?'файлы коммита':action==='show'?'сохранённый код':action==='history'?'историю файла':action==='diff'?'различия':'версии файла')+'…'));root.setAttribute('aria-busy','true');
    const controller=new AbortController();gitController=controller;const timeout=setTimeout(()=>controller.abort(),35000);
    const params=new URLSearchParams({action,ref:viewer.owner.ref,source:String(viewer.index),expected_updated_at:viewer.owner.updated_at,max_chars:String(viewer.maxChars)});
    if(viewer.legacy&&!viewer.legacy.path&&viewer.selectedPath&&action!=='commit')params.set('path',viewer.selectedPath);
    if(action==='history'){params.set('limit',String(viewer.historyLimit));if(viewer.targets.history)params.set('target',viewer.targets.history);}
    if(action==='diff'||action==='check'){if(viewer.worktree)params.set('worktree','1');else params.set('target',viewer.targets[action]||'HEAD');}
    try{
      const response=await fetch('/api/git?'+params,{cache:'no-store',signal:controller.signal});const data=await response.json();if(request!==gitRequest||viewer!==gitViewer)return;
      if(!response.ok){const error=new Error(data.error||'Сервер вернул HTTP '+response.status);error.status=response.status;error.code=data.code;throw error;}
      if(data.resolved_source&&hasGitAnchor(data.resolved_source))viewer.source=data.resolved_source;if(data.repo_id)viewer.resolvedRepoId=data.repo_id;updateGitHeading();
      renderGitResult(data,action);announce('Git: '+gitActionNames[action]+' · '+(viewer.source.git?.path||viewer.selectedPath||viewer.legacy?.path||viewer.legacy?.commit));
    }catch(error){
      if(request!==gitRequest||viewer!==gitViewer)return;
      root.replaceChildren();const box=make('div','git-error');box.setAttribute('role','alert');const stale=error.code==='stale_record'||(error.status===409&&!error.code);
      box.append(make('strong','',stale?'Карточка уже изменилась':error.code==='busy'?'Git уже обрабатывает другой запрос':error.code==='legacy_repository_ambiguous'?'Репозиторий нельзя определить однозначно':error.code==='legacy_repository_unavailable'?'Коммит не найден в доступных репозиториях':error.code==='legacy_path_unavailable'?'Файл недоступен в этом коммите':'Не удалось открыть Git-источник'));
      box.append(make('p','',stale?'Обновите карту, чтобы работать с текущим источником и его номером.':error.name==='AbortError'?'Запрос не завершился за 35 секунд. Попробуйте ещё раз.':error.message));
      if(error.code==='legacy_repository_ambiguous')box.append(make('p','','Нужна явная Git-привязка источника к проекту и репозиторию. Обновление карты само по себе эту неоднозначность не устранит.'));
      const retry=make('button','small',stale?'Обновить карту':error.code==='legacy_repository_ambiguous'?'Закрыть':'Повторить');retry.type='button';retry.addEventListener('click',()=>{if(stale){closeGit();loadGraph();}else if(error.code==='legacy_repository_ambiguous')closeGit();else requestGit();});box.append(retry);root.append(box);announce(stale?'Обновите карту: карточка изменилась.':'Не удалось открыть Git-источник.');
    }finally{clearTimeout(timeout);if(request===gitRequest){root.setAttribute('aria-busy','false');gitController=null;}}
  }
  $('git-close').addEventListener('click',closeGit);
  $('git-dialog').addEventListener('cancel',event=>{event.preventDefault();closeGit();});
  $('git-dialog').addEventListener('click',event=>{if(event.target!==$('git-dialog'))return;const rect=$('git-dialog').getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)closeGit();});
  for(const tab of $('git-dialog').querySelectorAll('[role=tab]')){
    tab.addEventListener('click',()=>switchGitAction(tab.dataset.gitAction));
    tab.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const actions=[...$('git-dialog').querySelectorAll('[role=tab]')].filter(item=>!item.hidden&&!item.disabled).map(item=>item.dataset.gitAction),index=actions.indexOf(tab.dataset.gitAction),next=event.key==='Home'?0:event.key==='End'?actions.length-1:(index+(event.key==='ArrowLeft'?-1:1)+actions.length)%actions.length;switchGitAction(actions[next]);$('git-tab-'+actions[next]).focus();});
  }
  $('git-controls').addEventListener('submit',event=>{event.preventDefault();requestGit();});
  $('git-worktree').addEventListener('change',()=>{$('git-target').disabled=$('git-worktree').checked;});

  function openSidebar(){state.sidebarOpen=true;$('main').classList.remove('sidebar-closed');$('main').classList.toggle('tools-reading',state.mode==='tools');$('open-details').hidden=true;}
  function showIntro(){
    if(state.mode==='encyclopedia'){
      $('detail-label').textContent='Как читать определения';const d=$('detail');d.replaceChildren();
      d.append(make('div','intro-symbol','Aa'),make('h2','','Один термин — несколько значений'),make('p','','Выберите понятие, чтобы прочитать полное определение, ограничения и источники.'));
      const tips=make('ul','tips');for(const tip of ['Одинаковые названия могут иметь разные значения в разных проектах.','Область применения задаётся сохранёнными связями с проектами.','История изменения смысла и варианты отображаются в связях карточки.'])tips.append(make('li','',tip));
      d.append(tips,make('p','detail-note','Краткий текст в списке — начало определения. Для выводов прочитайте полную карточку и источники.'));return;
    }
    $('detail-label').textContent='Как читать карту';const d=$('detail');d.replaceChildren();d.append(make('div','intro-symbol','◉'),make('h2','','Контекст связан между собой'),make('p','','Начните с проекта или найдите задачу, решение и другой объект в строке поиска.'));
    const tips=make('ul','tips');for(const tip of ['Выберите проект, чтобы раскрыть его записи и связи.','Нажмите на линию — увидите тип связи и её источник.','Глубина 1–3 показывает соседей по сохранённым связям.','Пунктир обозначает гипотезу. Бледные линии связывают запись с её проектом.'])tips.append(make('li','',tip));d.append(tips,make('p','detail-note','Карта отображает записи локальной памяти. Обновляйте данные кнопкой сверху; изменения в базу с этой страницы не записываются.'));
  }
  function renderNodeDetail(node){
    openSidebar();$('detail-label').textContent=kindFor(node)==='concept'?'Определение понятия':'Карточка узла';const d=$('detail');d.replaceChildren();const badges=make('div','badges');addBadge(badges,kindLabel(kindFor(node),true));if(node.knowledge_state)addBadge(badges,stateNames[node.knowledge_state] || node.knowledge_state,node.knowledge_state);if(node.work_state)addBadge(badges,workNames[node.work_state] || node.work_state);d.append(badges,make('h2','',titleFor(node)));
    const actions=make('div','detail-actions');const focus=make('button','small primary',state.mode==='encyclopedia'?'Открыть на карте':state.focus===node.id?'Фокус на этом узле':'Показать связи узла');focus.type='button';focus.disabled=state.mode==='map'&&state.focus===node.id;focus.addEventListener('click',()=>focusNode(node.id));actions.append(focus);if(kindFor(node)==='tool')actions.append(toolButton('Открыть инструмент',()=>{const id=node.id.replace(/^entity:/,'');state.toolId=id;state.toolProject='';setMode('tools',false);openTool(id);},'small'));d.append(actions);
    if(kindFor(node)==='concept'){d.append(make('div','scope-list',scopeLabel(node)));}
    if(node.summary)d.append(make('div','summary',node.summary));else d.append(make('p','','Описание пока не добавлено.'));
    addMeta(d,'Идентификатор',node.id);if(node.verified_at)addMeta(d,'Последняя проверка',dateText(node.verified_at));
    const tags=list(node.tags);if(tags.length){addSection(d,'Теги');const box=make('div','tag-list');tags.forEach(tag=>box.append(make('span','tag',tag)));d.append(box);}
    const aliases=list(node.aliases);if(aliases.length){addSection(d,'Другие названия');d.append(make('p','',aliases.join(' · ')));}
    if(kindFor(node)==='concept')addAlternativeDefinitions(d,node);
    addSources(d,node.sources,nodeSourceOwner(node));
    const related=list(state.adjacency.get(node.id));addSection(d,`Связи · ${related.length}`);
    if(!related.length)d.append(make('p','detail-empty','Связей в загруженных данных пока нет.'));
    const rows=make('div','related');
    for(const edge of related){const outgoing=edge.from_ref===node.id;const other=state.nodeMap.get(outgoing?edge.to_ref:edge.from_ref);if(!other)continue;const row=make('div','related-row');const btn=make('button','related-node',titleFor(other));btn.type='button';btn.addEventListener('click',()=>state.mode==='encyclopedia'?selectNode(other.id):focusNode(other.id));const relation=make('button','related-edge',(outgoing?'→ ':'← ')+edgeLabel(edge)+(edge.knowledge_state==='hypothesis'?' · гипотеза':''));relation.type='button';relation.title='Открыть основание связи';relation.addEventListener('click',()=>selectEdge(edge.id));row.append(btn,relation);rows.append(row);}
    d.append(rows);$('sidebar').scrollTop=0;
  }
  function renderEdgeDetail(edge){
    openSidebar();$('detail-label').textContent='Основание связи';const d=$('detail');d.replaceChildren();const badges=make('div','badges');addBadge(badges,edge.origin==='entry_project'?'Привязка записи к проекту':edge.origin==='project_relation'?'Сохранённая связь проектов':'Явная связь');if(edge.knowledge_state)addBadge(badges,stateNames[edge.knowledge_state] || edge.knowledge_state,edge.knowledge_state);d.append(badges,make('h2','',edgeLabel(edge)));
    const endpoints=make('div','edge-endpoints');[edge.from_ref,edge.to_ref].forEach((id,i)=>{if(i)endpoints.append(make('span','edge-direction','→'));const b=make('button','endpoint',titleFor(state.nodeMap.get(id)));b.type='button';b.addEventListener('click',()=>focusNode(id));endpoints.append(b);});d.append(endpoints);
    if(edge.origin==='entry_project')d.append(make('div','summary','Запись относится к контексту этого проекта.'));else if(edge.description)d.append(make('div','summary',edge.description));else d.append(make('p','','Описание связи пока не добавлено.'));
    addMeta(d,'Тип связи',edge.kind);addMeta(d,'Идентификатор',edge.id);if(edge.verified_at)addMeta(d,'Последняя проверка',dateText(edge.verified_at));addSources(d,edge.sources,edgeSourceOwner(edge));
    if(edge.origin==='entry_project')d.append(make('p','detail-note','Производная связь показывает организацию памяти. Она сама по себе не утверждает техническую зависимость.'));$('sidebar').scrollTop=0;
  }
  function showMessage(title,text,action=null){$('message').hidden=false;$('message-title').textContent=title;$('message-text').textContent=text;$('message-icon').textContent=state.loading?'◌':'◎';$('message-action').hidden=!action;$('message-action').textContent=action==='retry'?'Повторить загрузку':'Сбросить фильтры';$('message-action').dataset.action=action || '';}
  function hideMessage(){$('message').hidden=true;}

  function renderFilters(){
    const root=$('kind-filters');root.replaceChildren(make('span','filters-label','Типы узлов'));const kinds=[...state.kinds].sort((a,b)=>a==='project'?-1:b==='project'?1:kindLabel(a).localeCompare(kindLabel(b),'ru'));
    for(const kind of kinds){const members=state.nodes.filter(n=>kindFor(n)===kind);const b=make('button','chip');b.type='button';b.dataset.kind=kind;b.setAttribute('aria-pressed',String(!state.disabledKinds.has(kind)));const dot=make('i','swatch');dot.style.background=colorFor(members[0]);b.append(dot,document.createTextNode(kindLabel(kind)),make('span','',members.length));b.addEventListener('click',()=>{if(state.disabledKinds.has(kind))state.disabledKinds.delete(kind);else state.disabledKinds.add(kind);renderFilters();renderGraph(true);});root.append(b);}
  }
  function renderProjectOptions(){
    const select=$('project-filter');select.replaceChildren(new Option('Все проекты',''));
    if(state.mode==='scenarios'){
      select.replaceChildren(new Option('Все области',''),new Option('Глобальные','global'),new Option('Встроенные шаблоны','builtin'));for(const [id,name] of scenarioProjectNames())select.add(new Option('Проект · '+name,'project:'+id));
      if(scenariosState.scope&&![...select.options].some(option=>option.value===scenariosState.scope))scenariosState.scope='';select.value=scenariosState.scope;return;
    }
    if(state.mode==='rules'){
      select.replaceChildren(new Option('Все области',''),new Option('Глобальные','global'));for(const [id,name] of ruleProjectNames())select.add(new Option('Проект · '+name,'project:'+id));
      if(rulesState.scope&&![...select.options].some(option=>option.value===rulesState.scope))rulesState.scope='';select.value=rulesState.scope;return;
    }
    if(state.mode==='tools'){
      const projects=new Map();for(const tool of list(state.toolsData?.tools))for(const project of toolProjects(tool))projects.set(project.ref||'project:'+project.id,project);
      [...projects.entries()].sort((a,b)=>String(a[1].name).localeCompare(String(b[1].name),'ru')).forEach(([ref,project])=>select.add(new Option(project.name,ref)));if(state.toolFilter&&!projects.has(state.toolFilter))state.toolFilter='';select.value=state.toolFilter;return;
    }
    state.nodes.filter(n=>groupFor(n)==='project').sort(sortNodes).forEach(n=>select.add(new Option(titleFor(n),n.id)));select.value=state.mode==='encyclopedia'?state.conceptProject:state.focus&&groupFor(state.nodeMap.get(state.focus))==='project'?state.focus:'';
  }
  function nodeAllowed(node){return !state.disabledKinds.has(kindFor(node));}
  function computeVisible(){
    const distance=new Map();
    if(!state.focus){const nodes=state.nodes.filter(n=>groupFor(n)==='project'&&nodeAllowed(n));const ids=new Set(nodes.map(n=>n.id));return {nodes,edges:state.edges.filter(e=>(e.origin==='project_relation'||e.origin==='explicit')&&ids.has(e.from_ref)&&ids.has(e.to_ref)),distance};}
    if(!state.nodeMap.has(state.focus))return {nodes:[],edges:[],distance};
    distance.set(state.focus,0);let frontier=[state.focus];
    for(let depth=1;depth<=state.depth;depth++){const next=[];for(const id of frontier){for(const edge of list(state.adjacency.get(id))){const other=edge.from_ref===id?edge.to_ref:edge.from_ref;const node=state.nodeMap.get(other);if(!node||!nodeAllowed(node)||distance.has(other))continue;distance.set(other,depth);next.push(other);}}frontier=next;}
    const nodes=state.nodes.filter(n=>distance.has(n.id)&&nodeAllowed(n));const ids=new Set(nodes.map(n=>n.id));return {nodes,edges:state.edges.filter(e=>ids.has(e.from_ref)&&ids.has(e.to_ref)),distance};
  }
  function layout(nodes,distance){
    const positions=new Map();if(!nodes.length)return positions;
    if(!state.focus){const sorted=[...nodes].sort(sortNodes);const cols=Math.min(4,Math.max(1,Math.ceil(Math.sqrt(sorted.length*1.7))));const rows=Math.ceil(sorted.length/cols);sorted.forEach((n,i)=>positions.set(n.id,{x:(i%cols-(cols-1)/2)*235,y:(Math.floor(i/cols)-(rows-1)/2)*145}));return positions;}
    positions.set(state.focus,{x:0,y:0});let lastRadius=0;
    for(let depth=1;depth<=state.depth;depth++){const ring=nodes.filter(n=>distance.get(n.id)===depth).sort((a,b)=>{if(groupFor(a)!==groupFor(b))return groupFor(a).localeCompare(groupFor(b));return sortNodes(a,b);});if(!ring.length)continue;const radius=Math.max(lastRadius+235,ring.length*155/(Math.PI*2));lastRadius=radius;ring.forEach((n,i)=>{const angle=-Math.PI/2+2*Math.PI*i/ring.length;positions.set(n.id,{x:Math.cos(angle)*radius,y:Math.sin(angle)*radius*.78});});}
    return positions;
  }
  function nodeWidth(node){return groupFor(node)==='project'?188:154;}
  function getEdgePath(edge){
    const a=state.positions.get(edge.from_ref),b=state.positions.get(edge.to_ref);if(!a||!b)return '';
    if(edge.from_ref===edge.to_ref)return `M${a.x+25},${a.y-25} C${a.x+85},${a.y-110} ${a.x-85},${a.y-110} ${a.x-25},${a.y-25}`;
    const dx=b.x-a.x,dy=b.y-a.y,wa=nodeWidth(state.nodeMap.get(edge.from_ref))/2+4,wb=nodeWidth(state.nodeMap.get(edge.to_ref))/2+7;
    const ta=Math.min(Math.abs(wa/(dx||.001)),Math.abs(29/(dy||.001))),tb=Math.min(Math.abs(wb/(dx||.001)),Math.abs(30/(dy||.001)));
    return `M${a.x+dx*ta},${a.y+dy*ta} L${b.x-dx*tb},${b.y-dy*tb}`;
  }
  function drawPositions(){
    for(const el of $('nodes').children){const p=state.positions.get(el.dataset.id);if(p)el.setAttribute('transform',`translate(${p.x},${p.y})`);}
    for(const el of $('edges').children){const edge=state.edgeMap.get(el.dataset.id);if(!edge)continue;const path=getEdgePath(edge);el.querySelectorAll('path').forEach(p=>p.setAttribute('d',path));const label=el.querySelector('text');if(label){const a=state.positions.get(edge.from_ref),b=state.positions.get(edge.to_ref);label.setAttribute('x',(a.x+b.x)/2);label.setAttribute('y',(a.y+b.y)/2-7);}}
  }
  function renderGraph(fit=false){
    if(!state.loaded)return;
    const view=computeVisible();state.visibleNodes=view.nodes;state.visibleEdges=view.edges;state.positions=layout(view.nodes,view.distance);
    $('nodes').replaceChildren();$('edges').replaceChildren();
    for(const edge of view.edges){const selected=state.selection?.type==='edge'&&state.selection.id===edge.id;const g=makeSvg('g',{class:'edge-group '+(edge.origin==='entry_project'?'derived ':'')+(edge.knowledge_state||'')+(selected?' selected':''),'data-id':edge.id,tabindex:'0',role:'button','aria-label':`${titleFor(state.nodeMap.get(edge.from_ref))} — ${edgeLabel(edge)} — ${titleFor(state.nodeMap.get(edge.to_ref))}`});const title=makeSvg('title');title.textContent=edgeLabel(edge)+(edge.description?' · '+edge.description:'');g.append(title,makeSvg('path',{class:'edge-line','marker-end':'url(#arrow)'}),makeSvg('path',{class:'edge-hit'}));if(!state.focus&&view.edges.length<=16){const label=makeSvg('text',{class:'edge-label','text-anchor':'middle'});label.textContent=cut(edgeLabel(edge),25);g.append(label);}g.addEventListener('click',e=>{e.stopPropagation();if(!state.suppressClick)selectEdge(edge.id);});g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();selectEdge(edge.id);}});$('edges').append(g);}
    for(const node of view.nodes){const selected=state.selection?.type==='node'&&state.selection.id===node.id;const g=makeSvg('g',{class:'node '+(node.knowledge_state||'')+(selected?' selected':'')+(state.focus===node.id?' focus':''),'data-id':node.id,tabindex:'0',role:'button','aria-label':`${kindLabel(kindFor(node),true)}: ${titleFor(node)}`});const width=nodeWidth(node);const rect=makeSvg('rect',{class:'node-rect',x:-width/2,y:-25,width,height:50,rx:9});const dot=makeSvg('circle',{class:'node-dot',cx:-width/2+13,cy:-5,r:3.5,fill:colorFor(node)});const title=makeSvg('text',{class:'node-title',x:-width/2+23,y:-2});title.textContent=groupFor(node)==='project'?cutMiddle(titleFor(node),26):cut(titleFor(node),21);const kind=makeSvg('text',{class:'node-kind',x:-width/2+13,y:14});const count=list(state.adjacency.get(node.id)).length;kind.textContent=kindLabel(kindFor(node),true)+(count?' · '+count+' связ.':'');const tooltip=makeSvg('title');tooltip.textContent=titleFor(node);g.append(rect,dot,title,kind,tooltip);g.addEventListener('click',e=>{e.stopPropagation();if(state.suppressClick)return;if(!state.focus)focusNode(node.id);else selectNode(node.id);});g.addEventListener('dblclick',e=>{e.stopPropagation();if(!state.suppressClick)focusNode(node.id);});g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();if(!state.focus)focusNode(node.id);else selectNode(node.id);}});$('nodes').append(g);}
    drawPositions();
    $('view-label').textContent=state.focus?'Окрестность узла':'Обзор';$('view-title').textContent=state.focus?titleFor(state.nodeMap.get(state.focus)):'Все проекты';$('view-sub').textContent=state.focus?'Связи читаются в обоих направлениях':'Выберите проект, чтобы раскрыть знания';$('depth-bar').hidden=!state.focus;document.querySelectorAll('[data-depth]').forEach(b=>b.setAttribute('aria-pressed',String(Number(b.dataset.depth)===state.depth)));
    $('counts').textContent=`На карте: ${view.nodes.length} из ${state.nodes.length} узлов · ${view.edges.length} из ${state.edges.length} связей`;
    if(!view.nodes.length)showMessage(state.nodes.length?'Нет узлов для показа':'В памяти пока нет узлов',state.nodes.length?'Измените типы узлов или вернитесь к обзору проектов.':'Добавленные в память проекты появятся здесь после обновления.',state.nodes.length?'reset':null);else hideMessage();if(fit)fitGraph();
  }
  function updateSelectionClasses(){for(const el of $('nodes').children)el.classList.toggle('selected',state.selection?.type==='node'&&el.dataset.id===state.selection.id);for(const el of $('edges').children)el.classList.toggle('selected',state.selection?.type==='edge'&&el.dataset.id===state.selection.id);}
  function selectNode(id){const node=state.nodeMap.get(id);if(!node)return;state.selection={type:'node',id};updateSelectionClasses();updateConceptSelection();renderNodeDetail(node);announce('Выбран узел: '+titleFor(node));}
  function selectEdge(id){const edge=state.edgeMap.get(id);if(!edge)return;state.selection={type:'edge',id};updateSelectionClasses();renderEdgeDetail(edge);announce('Выбрана связь: '+edgeLabel(edge));}
  function focusNode(id){const node=state.nodeMap.get(id);if(!node)return;if(state.mode!=='map')setMode('map');state.disabledKinds.delete(kindFor(node));state.focus=id;state.selection={type:'node',id};renderFilters();renderProjectOptions();hideSearch();renderNodeDetail(node);renderGraph(true);announce('Показаны связи узла: '+titleFor(node));}
  function resetView(clearFilters=true){state.focus=null;state.selection=null;state.depth=1;if(clearFilters)state.disabledKinds.clear();$('search').value='';hideSearch();renderProjectOptions();renderFilters();showIntro();renderGraph(true);announce('Обзор всех проектов');}
  function applyCamera(){const c=state.camera;$('viewport').setAttribute('transform',`translate(${c.x},${c.y}) scale(${c.k})`);$('zoom-value').textContent=Math.round(c.k*100)+'%';}
  function fitGraph(){
    if(!state.positions.size)return;const width=$('canvas').clientWidth,height=$('canvas').clientHeight;const points=[...state.positions.values()];const minX=Math.min(...points.map(p=>p.x))-98,maxX=Math.max(...points.map(p=>p.x))+98,minY=Math.min(...points.map(p=>p.y))-48,maxY=Math.max(...points.map(p=>p.y))+48;
    const top=state.focus?145:115,bottom=110;const usableW=Math.max(160,width-75),usableH=Math.max(160,height-top-bottom);const k=Math.max(.09,Math.min(1.25,usableW/(maxX-minX),usableH/(maxY-minY)));state.camera={k,x:width/2-(minX+maxX)/2*k,y:top+usableH/2-(minY+maxY)/2*k};applyCamera();
  }
  function zoom(factor,x,y){const rect=svg.getBoundingClientRect();const px=x ?? rect.width/2,py=y ?? rect.height/2;const old=state.camera,k=Math.min(4,Math.max(.06,old.k*factor));state.camera={k,x:px-(px-old.x)*k/old.k,y:py-(py-old.y)*k/old.k};applyCamera();}
  function hideSearch(){$('search-results').hidden=true;$('search').setAttribute('aria-expanded','false');}
  function runSearch(){
    if(state.mode==='scenarios'){scenariosState.query=$('search').value;hideSearch();if(!scenariosState.editing)renderScenarios();return;}
    if(state.mode==='rules'){rulesState.query=$('search').value;hideSearch();renderRules();return;}
    if(state.mode==='agents'){agentsState.query=$('search').value;hideSearch();renderAgents();return;}
    if(state.mode==='tools'){state.toolQuery=$('search').value;state.toolId='';state.toolProject='';closeToolRecord();writeToolsLocation();hideSearch();renderTools();return;}
    if(state.mode==='encyclopedia'){state.conceptQuery=$('search').value;hideSearch();renderEncyclopedia();return;}
    state.mapQuery=$('search').value;
    const query=$('search').value.trim().toLocaleLowerCase('ru');const results=$('search-results');results.replaceChildren();if(!query){hideSearch();return;}
    const words=query.split(/\s+/);const matches=state.nodes.map(node=>{const title=titleFor(node).toLocaleLowerCase('ru');const all=[node.id,node.title,node.summary,...list(node.tags),...list(node.aliases)].join(' ').toLocaleLowerCase('ru');const match=words.every(w=>all.includes(w));const score=title===query?100:title.startsWith(query)?75:title.includes(query)?50:words.filter(w=>title.includes(w)).length*10;return{node,match,score};}).filter(x=>x.match).sort((a,b)=>b.score-a.score||sortNodes(a.node,b.node));
    results.append(make('div','results-note',matches.length?`Найдено: ${matches.length}${matches.length>30?' · первые 30':''}`:'Ничего не найдено в загруженных данных'));
    for(const {node} of matches.slice(0,30)){const b=make('button','result');b.type='button';b.append(make('span','result-title',titleFor(node)),make('span','result-meta',kindLabel(kindFor(node),true)+(node.knowledge_state==='hypothesis'?' · гипотеза':'')+' · '+cut(node.id,58)));b.addEventListener('click',()=>{focusNode(node.id);$('search').value='';});results.append(b);}if(!matches.length&&!$('hypotheses').checked)results.append(make('div','results-note','Гипотезы и устаревшие записи доступны через переключатели.'));results.hidden=false;$('search').setAttribute('aria-expanded','true');
  }

  const normalized = value => String(value || '').normalize('NFKC').toLocaleLowerCase('ru').trim();
  const nameSet = node => new Set([titleFor(node),...list(node.aliases)].map(normalized).filter(Boolean));
  function scopesFor(node){
    return list(state.adjacency.get(node.id)).filter(edge=>edge.origin==='explicit'&&edge.knowledge_state==='confirmed'&&list(edge.sources).some(source=>source&&source.reference)&&edge.kind==='used_in'&&edge.from_ref===node.id&&groupFor(state.nodeMap.get(edge.to_ref))==='project').map(edge=>({node:state.nodeMap.get(edge.to_ref),edge})).sort((a,b)=>sortNodes(a.node,b.node));
  }
  function scopeLabel(node){const scopes=scopesFor(node);return scopes.length?scopes.map(item=>titleFor(item.node)+(item.edge.knowledge_state==='hypothesis'?' (гипотеза)':item.edge.knowledge_state==='superseded'?' (устарело)':'')).join(' · '):'Область применения не указана';}
  function definitionExcerpt(node){
    const summary=String(node.summary || '').trim();if(!summary)return 'Определение пока не добавлено.';
    const paragraphs=summary.split(/\n\s*\n/).filter(Boolean);const definition=paragraphs.find(p=>/^(?:Определение|Definition)\s*[:：]/i.test(p)) || paragraphs[0];
    return cut(definition.replace(/^(?:Определение|Definition)\s*[:：]\s*/i,'').replace(/\s+/g,' '),340);
  }
  function addAlternativeDefinitions(parent,node){
    const names=nameSet(node);const variants=state.nodes.filter(other=>other.id!==node.id&&kindFor(other)==='concept'&&[...nameSet(other)].some(name=>names.has(name))).sort(sortNodes);
    if(!variants.length)return;addSection(parent,'То же название в других карточках');
    parent.append(make('p','detail-empty','Совпадение названия не означает одинаковый смысл.'));
    const rows=make('div','related');for(const other of variants){const row=make('div','related-row');const button=make('button','related-node',titleFor(other));button.type='button';button.addEventListener('click',()=>selectNode(other.id));row.append(button,make('div','result-meta',scopeLabel(other)));rows.append(row);}parent.append(rows);
  }
  function conceptMatches(node,query){
    if(!query)return true;const fields=[titleFor(node),...list(node.aliases),node.summary,...list(node.tags),...scopesFor(node).map(item=>titleFor(item.node))].map(normalized);
    const words=query.split(/\s+/).filter(Boolean);const tokens=new Set(fields.join(' ').match(/[\p{L}\p{N}_]+/gu) || []);
    return words.every(word=>Array.from(word).length<=2?tokens.has(word)||fields.includes(word):fields.some(field=>field.includes(word)));
  }
  function updateConceptSelection(){for(const button of $('concepts').querySelectorAll('[data-concept]'))button.setAttribute('aria-pressed',String(state.selection?.type==='node'&&state.selection.id===button.dataset.concept));}
  function renderEncyclopedia(){
    const root=$('concepts'),alphabet=$('alphabet');root.replaceChildren();alphabet.replaceChildren();
    const concepts=state.nodes.filter(node=>kindFor(node)==='concept');const query=normalized(state.conceptQuery);
    const matches=concepts.filter(node=>(!state.conceptProject||scopesFor(node).some(item=>item.node.id===state.conceptProject))&&conceptMatches(node,query)).sort(sortNodes);
    $('encyclopedia-warning').hidden=!state.truncated;$('encyclopedia-count').textContent=state.loaded?`${matches.length} из ${concepts.length} определений`:'';
    const exactNames=query?concepts.filter(node=>nameSet(node).has(query)):[];
    $('encyclopedia-note').textContent=exactNames.length>1?(state.conceptProject?'У этого названия несколько определений. Сейчас показаны определения для выбранного проекта.':'У этого названия несколько определений. Сравните область применения и источники.'):state.conceptProject?'Показаны определения с явно указанным применением в выбранном проекте.':'Все определения по алфавиту. Поиск учитывает названия, синонимы и текст карточек.';
    if(!matches.length){const empty=make('div','encyclopedia-empty');const title=!state.loaded?(state.lastError?'Не удалось загрузить понятия':'Загружаем понятия'):concepts.length?'Подходящих определений нет':'Энциклопедия пока пуста';const text=!state.loaded?(state.lastError+' Нажмите «Обновить», чтобы повторить загрузку.').replace(/^ /,''):concepts.length?'Измените запрос или проект. Гипотезы и устаревшие определения доступны через переключатели.':'Новые понятия появятся здесь после сохранения в памяти и обновления страницы.';empty.append(make('strong','',title),document.createTextNode(state.loading?'Читаем сохранённые определения.':text));root.append(empty);return;}
    const groups=new Map();for(const node of matches){const letter=Array.from(titleFor(node).trim())[0]?.toLocaleUpperCase('ru') || '#';if(!groups.has(letter))groups.set(letter,[]);groups.get(letter).push(node);}
    let index=0;for(const [letter,nodes] of groups){const id='concept-group-'+index++;const jump=make('a','',letter);jump.href='#'+id;jump.addEventListener('click',event=>{event.preventDefault();$(id).scrollIntoView({block:'start'});$(id).focus({preventScroll:true});});alphabet.append(jump);const group=make('section','concept-group');group.id=id;group.tabIndex=-1;group.setAttribute('aria-label','Понятия на '+letter);group.append(make('h3','concept-letter',letter));const cards=make('div','concept-list');
      for(const node of nodes){const card=make('button','concept-card');card.type='button';card.dataset.concept=node.id;card.setAttribute('aria-pressed',String(state.selection?.type==='node'&&state.selection.id===node.id));card.append(make('span','concept-title',titleFor(node)),make('span','concept-scope',scopeLabel(node)),make('span','concept-excerpt',definitionExcerpt(node)));if(list(node.aliases).length)card.append(make('span','concept-aliases','Также: '+list(node.aliases).join(' · ')));if(node.knowledge_state&&node.knowledge_state!=='confirmed')card.append(make('span','concept-status',stateNames[node.knowledge_state] || node.knowledge_state));card.addEventListener('click',()=>selectNode(node.id));cards.append(card);}group.append(cards);root.append(group);
    }
  }
  function toolMark(){
    const mark=make('span','tool-mark');mark.setAttribute('aria-hidden','true');
    const icon=makeSvg('svg',{viewBox:'0 0 24 24',fill:'none'});icon.append(makeSvg('path',{d:'M8 6V4h8v2M4 7h16v13H4V7Zm0 5h16M10 11v3h4v-3',stroke:'currentColor','stroke-width':'1.4','stroke-linejoin':'round'}));mark.append(icon);return mark;
  }
  const toolRef=record=>record.ref||'entry:'+record.id;
  const toolRecordNode=record=>({...record,id:toolRef(record)});
  function toolExcerpt(record,length=200){return cut(String(record.summary||'').trim().split(/\n\s*\n/)[0].replace(/^(?:Определение|Назначение|Definition)\s*[:：]\s*/i,'').replace(/\s+/g,' '),length);}
  function toolButton(label,handler,cls=''){const button=make('button',cls,label);button.type='button';button.addEventListener('click',handler);return button;}
  function closeToolRecord(){state.toolRecord=null;$('main').classList.remove('tools-reading');}
  function writeToolsLocation(){
    const params=new URLSearchParams();if(state.toolId)params.set('tool',state.toolId);if(state.toolProject)params.set('project',state.toolProject);
    const query=params.toString(),hash='#tools'+(query?'?'+query:'');if(location.hash!==hash)history.pushState(null,'',hash);
  }
  function openTool(id,project='',focus=true){
    state.toolId=id;state.toolProject=project;closeToolRecord();writeToolsLocation();renderTools();ensureToolDetail();$('tool-catalog').scrollTop=0;
    if(focus){const heading=$('tools-content').querySelector('.tool-detail-heading');state.toolFocusPending=!heading;if(heading)heading.focus({preventScroll:true});}
  }
  function toolSection(parent,title,amount=null){const row=make('div','tools-subheading');row.append(make('h3','',title));if(amount!==null)row.append(make('span','',String(amount)));parent.append(row);}
  function toolWarning(parent,text){const box=make('div','tools-warning',text);box.setAttribute('role','status');parent.append(box);}
  function toolEmpty(parent,title,description,retry=false){const box=make('div','tools-empty');box.append(toolMark(),make('h3','',title),make('p','',description));if(retry)box.append(toolButton('Повторить загрузку',()=>loadTools(),'primary'));parent.append(box);}
  function toolBreadcrumbs(parent,tool=null,project=null){
    const nav=make('nav','tools-breadcrumbs');nav.setAttribute('aria-label','Навигация по инструментам');nav.append(toolButton('Инструменты',()=>openTool('')));
    if(tool){nav.append(make('span','','/'));if(project)nav.append(toolButton(tool.title,()=>openTool(tool.id)));else{const name=make('span','',tool.title);name.setAttribute('aria-current','page');nav.append(name);}}
    if(project){const name=make('span','',project.name);name.setAttribute('aria-current','page');nav.append(make('span','','/'),name);}parent.append(nav);
  }
  function toolStatus(parent,record){if(record.knowledge_state&&record.knowledge_state!=='confirmed')addBadge(parent,stateNames[record.knowledge_state]||record.knowledge_state,record.knowledge_state);}
  function toolProjects(tool){return list(tool.projects).filter(project=>project&&typeof project.id==='string');}
  function toolMatches(tool,query){
    const fields=[tool.title,tool.summary,...list(tool.aliases),...list(tool.tags),...toolProjects(tool).map(project=>project.name)].map(normalized);
    return query.split(/\s+/).filter(Boolean).every(word=>fields.some(field=>field.includes(word)));
  }
  function renderToolCard(tool){
    const card=make('article','tool-card');const top=make('div','tool-card-top');top.append(toolMark());toolStatus(top,tool);card.append(top);
    const open=toolButton('',()=>openTool(tool.id),'tool-card-open');open.dataset.tool=tool.id;open.append(make('span','',tool.title),make('span','tool-arrow','↗'));card.append(open,make('p','tool-card-summary',toolExcerpt(tool,340)||'Описание пока не добавлено.'));
    if(list(tool.tags).length){const tags=make('div','tag-list');list(tool.tags).slice(0,3).forEach(tag=>tags.append(make('span','tag',tag)));card.append(tags);}
    const bottom=make('div','tool-card-bottom');bottom.append(make('div','tool-card-label','Где применяли'));const projects=toolProjects(tool),chips=make('div','tool-project-chips');
    for(const project of projects.slice(0,3)){const button=toolButton(project.name,()=>openTool(tool.id,project.id),'tool-project-chip');button.title='Задачи и применение · '+project.name;chips.append(button);}
    if(projects.length>3)chips.append(toolButton('Ещё '+(projects.length-3),()=>openTool(tool.id),'tool-project-chip'));
    if(!projects.length)chips.append(make('span','tool-muted','Проекты пока не указаны'));bottom.append(chips);
    const stats=[];if(tool.task_count)stats.push('Задачи: '+tool.task_count);if(tool.record_count)stats.push('Применения: '+tool.record_count);if(tool.truncated)stats.push('Показана часть связей');else if(tool.verified_at)stats.push('Проверено '+dateText(tool.verified_at));if(stats.length)bottom.append(make('div','tool-card-footnote',stats.join(' · ')));card.append(bottom);return card;
  }
  function renderToolCatalog(root){
    const all=list(state.toolsData?.tools),query=normalized(state.toolQuery),matches=all.filter(tool=>(!state.toolFilter||toolProjects(tool).some(project=>project.ref===state.toolFilter))&&toolMatches(tool,query));
    const heading=make('div','tools-heading'),copy=make('div');copy.append(make('div','tools-eyebrow','Практика и опыт'),make('h2','tool-detail-heading','Инструменты'),make('p','tools-lead','Чем решали задачи, как настраивали и где применяли. Откройте инструмент, чтобы найти проекты и связанный опыт.'));copy.querySelector('h2').tabIndex=-1;
    const count=make('div','tools-total');count.append(make('strong','',matches.length),make('span','',query||state.toolFilter?'найдено':'в каталоге'));heading.append(copy,count);root.append(heading);
    if(state.toolsData?.truncated)toolWarning(root,'Показана часть каталога. Некоторые инструменты, проекты и задачи могут не попасть в этот список.');
    if(!matches.length){toolEmpty(root,all.length?'Подходящих инструментов нет':'Здесь будет ваш инструментарий',all.length?'Попробуйте другое название, назначение или проект.':'Сохраняйте инструменты и связывайте их с задачами и рецептами применения. Здесь появятся карточки с проектами и опытом применения.');if(all.length){const reset=toolButton('Сбросить поиск и проект',()=>{state.toolQuery='';state.toolFilter='';$('search').value='';renderProjectOptions();renderTools();},'small');root.querySelector('.tools-empty').append(reset);}return;}
    const cards=make('div','tools-grid');for(const tool of matches.sort((a,b)=>String(a.title).localeCompare(String(b.title),'ru')))cards.append(renderToolCard(tool));root.append(cards);
  }
  function renderToolFacts(tool){
    const panel=make('div','tools-panel'),facts=make('div','tools-facts');panel.append(make('h3','','Об инструменте'));
    for(const [label,value] of [['Другие названия',list(tool.aliases).join(' · ')],['Последняя проверка',tool.verified_at?dateText(tool.verified_at):'Пока не указана']]){if(!value)continue;const row=make('div');row.append(make('div','tools-fact-label',label),make('div','tools-fact-value',value));facts.append(row);}
    if(list(tool.tags).length){const row=make('div');row.append(make('div','tools-fact-label','Темы'));const tags=make('div','tag-list');list(tool.tags).forEach(tag=>tags.append(make('span','tag',tag)));row.append(tags);facts.append(row);}panel.append(facts);
    const sources=make('details','tools-fold');sources.append(make('summary','','Источники · '+list(tool.sources).length));addSources(sources,tool.sources,nodeSourceOwner({...tool,id:tool.ref||'entity:'+tool.id}));panel.append(sources);return panel;
  }
  function renderToolDescription(parent,tool){
    const summary=String(tool.summary||'').trim(),paragraphs=summary.split(/\n\s*\n/).filter(Boolean),box=make('div','tools-summary');
    if(!paragraphs.length)box.append(make('p','','Описание пока не добавлено.'));
    for(const paragraph of paragraphs.slice(0,2))box.append(make('p','',paragraph));parent.append(box);
    if(paragraphs.length>2){const more=make('details','tools-fold');more.append(make('summary','','Читать полное описание'));const rest=make('div','tools-summary');paragraphs.slice(2).forEach(paragraph=>rest.append(make('p','',paragraph)));more.append(rest);parent.append(more);}
  }
  function renderToolDetail(root,tool){
    toolBreadcrumbs(root,tool);const heading=make('div','tools-heading'),titleRow=make('div','tools-title-row'),copy=make('div','tools-title-main');copy.append(make('div','tools-eyebrow','Инструмент'),make('h2','tool-detail-heading',tool.title));copy.querySelector('h2').tabIndex=-1;const meta=make('div','tools-meta');meta.append(make('span','','Проекты: '+toolProjects(tool).length),make('span','','Задачи: '+(tool.task_count||0)));toolStatus(meta,tool);copy.append(meta);titleRow.append(toolMark(),copy);heading.append(titleRow);root.append(heading);
    if(tool.truncated||state.toolsData.truncated)toolWarning(root,'Часть связанного опыта не вошла в этот снимок. Список проектов, задач и применений может быть неполным.');
    const overview=make('div','tools-overview'),left=make('div'),description=make('div','tools-panel');description.append(make('h3','','Назначение и использование'));renderToolDescription(description,tool);left.append(description);
    toolSection(left,'Проекты',toolProjects(tool).length);const projects=make('div','tool-project-list');
    for(const project of toolProjects(tool)){const row=toolButton('',()=>openTool(tool.id,project.id),'tool-project-row');row.dataset.toolProject=project.id;const info=make('span','tool-project-info');info.append(make('span','tool-project-name',project.name),make('span','tool-project-meta','Задачи: '+(project.task_count||0)+' · Применения: '+(project.record_count||0)+(project.truncated?' · часть данных':'')));row.append(make('span','tool-project-icon','▱'),info,make('span','tool-arrow','→'));projects.append(row);}
    if(!projects.childElementCount)projects.append(make('p','tools-inline-note','Проекты применения пока не указаны. Они появятся после сохранения связи с проектом или записи об использовании инструмента.'));left.append(projects);overview.append(left,renderToolFacts(tool));root.append(overview);
    const unscoped=list(tool.records).filter(record=>!list(record.project_ids).length);if(unscoped.length){toolSection(root,'Применения без проекта',unscoped.length);const records=make('div','tools-task-list');for(const record of unscoped)records.append(renderToolTask(record,[]));root.append(records);}
  }
  function openToolRecord(record){
    state.toolRecord=record;openSidebar();$('detail-label').textContent=kindLabel(record.kind,true);const d=$('detail');d.replaceChildren();const badges=make('div','badges');addBadge(badges,kindLabel(record.kind,true));if(record.work_state)addBadge(badges,workNames[record.work_state]||record.work_state);toolStatus(badges,record);d.append(badges,make('h2','',record.title||record.id));
    if(record.jira_key)d.append(make('div','scope-list',record.jira_key));d.append(make('div','summary',record.summary||'Описание пока не добавлено.'));if(record.verified_at)addMeta(d,'Последняя проверка',dateText(record.verified_at));addSources(d,record.sources,nodeSourceOwner(toolRecordNode(record)));
    if(state.nodeMap.has(toolRef(record))){const actions=make('div','detail-actions');actions.append(toolButton('Открыть на карте',()=>focusNode(toolRef(record)),'small'));d.append(actions);}addMeta(d,'Идентификатор',record.id);$('sidebar').scrollTop=0;$('detail').querySelector('h2').tabIndex=-1;$('detail').querySelector('h2').focus({preventScroll:true});if(matchMedia('(max-width:760px)').matches)$('sidebar').scrollIntoView({block:'start'});announce('Открыта карточка: '+(record.title||record.id));
  }
  function renderToolTask(task,usage=list(task.usage_records)){
    const card=make('article','tools-task'),head=make('div','tools-task-header'),button=toolButton(task.title||task.id,()=>openToolRecord(task),'tools-task-title');button.dataset.toolRecord=task.id;head.append(button);const badges=make('div','badges');if(task.work_state)addBadge(badges,workNames[task.work_state]||task.work_state);toolStatus(badges,task);if(badges.childElementCount)head.append(badges);card.append(head);
    const excerpt=toolExcerpt(task,270);if(excerpt)card.append(make('p','tools-task-summary',excerpt));const meta=make('div','tools-task-meta');meta.append(make('span','',kindLabel(task.kind,true)));if(task.jira_key)meta.append(make('span','',task.jira_key));if(task.verified_at)meta.append(make('span','','Проверено '+dateText(task.verified_at)));card.append(meta);
    const related=usage.filter(record=>record.id!==task.id);if(related.length){const context=make('div','tools-task-context');context.append(make('div','tools-task-context-label','Применение инструмента'));for(const record of related){const link=toolButton('',()=>openToolRecord(record),'tools-record-link');link.append(make('span','tools-record-kind',kindLabel(record.kind,true)+': '),document.createTextNode(record.title||record.id));context.append(link);}card.append(context);}return card;
  }
  function renderToolProject(root,tool,project){
    toolBreadcrumbs(root,tool,project);const heading=make('div','tools-heading'),copy=make('div');copy.append(make('div','tools-eyebrow',tool.title+' / применение в проекте'),make('h2','tool-detail-heading',project.name));copy.querySelector('h2').tabIndex=-1;if(project.description)copy.append(make('p','tools-project-description',project.description));heading.append(copy);root.append(heading);
    if(project.truncated||tool.truncated||state.toolsData.truncated)toolWarning(root,'Показана часть связанных задач и применений. Отсутствие записи в этом списке не означает, что такого опыта нет.');
    const tasks=list(project.tasks);toolSection(root,'Задачи',tasks.length);const taskList=make('div','tools-task-list');for(const task of tasks)taskList.append(renderToolTask(task));if(!tasks.length)taskList.append(make('p','tools-inline-note','Связанные задачи пока не указаны. Здесь появятся задачи, в которых этот инструмент применяли напрямую или в рамках связанного рецепта.'));root.append(taskList);
    const shown=new Set(tasks.flatMap(task=>[task.id,...list(task.usage_records).map(record=>record.id)])),records=list(project.records).filter(record=>!shown.has(record.id));
    if(records.length){toolSection(root,'Другие рецепты и результаты',records.length);const rows=make('div','tools-task-list');records.forEach(record=>rows.append(renderToolTask(record,[])));root.append(rows);}root.append(toolButton('← К инструменту',()=>openTool(tool.id),'tools-back small'));root.lastElementChild.style.marginTop='24px';
  }
  function renderTools(){
    const root=$('tools-content');root.replaceChildren();$('tool-catalog').setAttribute('aria-busy',String(state.toolsLoading));
    if(!state.toolsData){toolEmpty(root,state.toolsLoading?'Загружаем инструменты':state.toolsError?'Не удалось загрузить инструменты':'Каталог инструментов',state.toolsLoading?'Собираем карточки и связанный опыт.':state.toolsError||'Откройте каталог, чтобы посмотреть сохранённые инструменты.',Boolean(state.toolsError));return;}
    if(state.toolsError)toolWarning(root,'Не удалось обновить каталог: '+state.toolsError+' Показан предыдущий снимок.');
    if(!state.toolId){renderToolCatalog(root);return;}const tool=state.toolDetails.get(state.toolId)||list(state.toolsData.tools).find(item=>item.id===state.toolId);
    if(state.toolDetailMissing===state.toolId&&!state.toolDetailLoading){toolBreadcrumbs(root);toolEmpty(root,'Инструмент не найден','В памяти нет этого инструмента при выбранных состояниях знаний. Его могли удалить или скрыть фильтром гипотез или устаревших записей.');return;}
    if(!tool&&!state.toolDetailLoading&&!state.toolDetailError){toolBreadcrumbs(root);toolEmpty(root,'Инструмент не найден в этом снимке','Он мог быть изменён, скрыт фильтром состояния или не войти в загруженную часть каталога.');return;}
    if(!tool||tool.detail!==true){toolBreadcrumbs(root,tool);toolEmpty(root,state.toolDetailLoading?'Открываем инструмент':state.toolDetailError?'Не удалось открыть инструмент':'Загружаем применение',state.toolDetailLoading?'Читаем проекты, задачи и рецепты применения.':state.toolDetailError||'Читаем подробности инструмента.');if(state.toolDetailError)root.querySelector('.tools-empty').append(toolButton('Повторить',()=>loadToolDetail(state.toolId),'primary'));return;}
    if(!state.toolProject){renderToolDetail(root,tool);return;}const project=toolProjects(tool).find(item=>item.id===state.toolProject);
    if(!project){toolBreadcrumbs(root,tool);toolEmpty(root,'Применение в проекте не найдено','В текущем снимке нет подтверждённой связи этого инструмента с выбранным проектом.');return;}renderToolProject(root,tool,project);
  }
  let toolsController=null,toolDetailController=null;
  const toolsFilterKey=()=>[$('hypotheses').checked,$('superseded').checked].join(':');
  function hydrateToolDetail(data){
    const records=data.records||{},resolve=value=>{if(typeof value==='string')return records[value]?{...records[value],ref:value}:null;if(value&&typeof value==='object')return value;return null;};
    const value=list(data.tools)[0];if(!value)return null;
    const tool={...value,detail:true};tool.records=list(value.records).map(resolve).filter(Boolean);
    tool.projects=list(value.projects).map(project=>({...project,records:list(project.records).map(resolve).filter(Boolean),tasks:list(project.tasks).map(task=>{const full=resolve(task.ref||task);return full?{...full,usage_records:list(task.usage_refs||task.usage_records).map(resolve).filter(Boolean)}:null;}).filter(Boolean)}));return tool;
  }
  function waitForToolsRetry(delay,signal){
    return new Promise((resolve,reject)=>{
      const abort=()=>{clearTimeout(timer);reject(new DOMException('Request aborted','AbortError'));};let timer;
      if(signal.aborted){abort();return;}
      timer=setTimeout(()=>{signal.removeEventListener('abort',abort);resolve();},delay);signal.addEventListener('abort',abort,{once:true});
    });
  }
  async function requestTools(params,signal){
    // Aborting fetch does not stop the server worker. Wait for its bounded lock
    // instead of turning rapid navigation or filter changes into a failed page.
    // The caller's 20-second AbortSignal bounds both requests and retry waits.
    for(let delay=250;;delay=Math.min(delay*2,2000)){
      const response=await fetch('/api/tools?'+params,{cache:'no-store',signal});if(response.ok)return response.json();
      let error=null;try{error=await response.json();}catch(cause){if(cause.name==='AbortError')throw cause;}
      if(response.status===503&&error?.code==='busy'){await waitForToolsRetry(delay,signal);continue;}
      if(response.status===404){const missing=new Error('Инструмент не найден в текущем состоянии памяти.');missing.notFound=true;throw missing;}
      throw new Error('Сервер вернул HTTP '+response.status+(typeof error?.error==='string'&&error.error?': '+error.error.slice(0,300):''));
    }
  }
  function currentToolDetail(){return state.toolDetails.get(state.toolId)||null;}
  function ensureToolDetail(){if(state.mode==='tools'&&state.toolId&&state.toolsData&&!currentToolDetail())loadToolDetail(state.toolId);}
  async function loadToolDetail(identity){
    if(!identity)return;const id=identity,flags=toolsFilterKey(),key=id+'|'+flags;
    if(state.toolDetailLoading&&toolDetailController?.key===key)return;
    if(toolDetailController)toolDetailController.abort();const request=++state.toolDetailRequest,controller=new AbortController();controller.key=key;toolDetailController=controller;
    const timeout=setTimeout(()=>controller.abort(),20000);state.toolDetailLoading=true;state.toolDetailError='';state.toolDetailMissing='';renderTools();
    try{
      const params=new URLSearchParams({tool:id,include_hypotheses:$('hypotheses').checked?'1':'0',include_superseded:$('superseded').checked?'1':'0'}),data=await requestTools(params,controller.signal);
      if(request!==state.toolDetailRequest)return;const tool=hydrateToolDetail(data);if(!tool||tool.id!==id)throw new Error('Неожиданный формат карточки инструмента');
      tool.truncated=Boolean(tool.truncated||data.truncated);state.toolDetails.set(tool.id,tool);state.toolDetailLoading=false;
      renderTools();if(state.toolRecord){const records=[...tool.records,...toolProjects(tool).flatMap(project=>[...list(project.tasks),...list(project.records)])],fresh=records.find(record=>record.id===state.toolRecord.id);if(fresh&&state.mode==='tools')openToolRecord(fresh);else closeToolRecord();}else if(state.toolFocusPending&&state.mode==='tools'){state.toolFocusPending=false;$('tools-content').querySelector('.tool-detail-heading')?.focus({preventScroll:true});}
      announce('Открыт инструмент: '+tool.title);
    }catch(error){if(request!==state.toolDetailRequest)return;state.toolDetailLoading=false;state.toolDetailMissing=error.notFound?id:'';state.toolDetailError=error.name==='AbortError'?'Сервер не ответил за 20 секунд.':error.message;closeToolRecord();renderTools();}
    finally{clearTimeout(timeout);if(request===state.toolDetailRequest){state.toolDetailLoading=false;toolDetailController=null;}}
  }
  async function loadTools(){
    if(toolsController)toolsController.abort();if(toolDetailController)toolDetailController.abort();state.toolDetailRequest++;state.toolDetailLoading=false;state.toolDetailError='';state.toolDetailMissing='';
    const flags=toolsFilterKey();if(state.toolsData&&state.toolsFlags!==flags){state.toolsData=null;state.toolDetails.clear();closeToolRecord();}
    state.toolsRequestedFlags=flags;
    const request=++state.toolsRequest;state.toolsLoading=true;state.toolsError='';renderTools();const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),20000);toolsController=controller;
    try{const params=new URLSearchParams({include_hypotheses:$('hypotheses').checked?'1':'0',include_superseded:$('superseded').checked?'1':'0'});const data=await requestTools(params,controller.signal);if(!data||!Array.isArray(data.tools))throw new Error('Неожиданный формат каталога');if(request!==state.toolsRequest)return;state.toolsData=data;state.toolsFlags=flags;state.toolsLoading=false;state.toolDetails.clear();if(state.mode==='tools')renderProjectOptions();renderTools();ensureToolDetail();if(state.mode==='tools'&&data.generated_at)$('updated-at').textContent='Снимок: '+dateText(data.generated_at);announce('Каталог инструментов загружен');}
    catch(error){if(request!==state.toolsRequest)return;state.toolsLoading=false;state.toolsError=error.name==='AbortError'?'Сервер не ответил за 20 секунд.':error.message;renderTools();}
    finally{clearTimeout(timeout);if(request===state.toolsRequest){state.toolsLoading=false;toolsController=null;$('tool-catalog').setAttribute('aria-busy','false');}}
  }
  function applyToolsLocation(){
    if(location.hash==='#tools'||location.hash.startsWith('#tools?')){const params=new URLSearchParams(location.hash.split('?')[1]||'');state.toolId=params.get('tool')||'';state.toolProject=params.get('project')||'';closeToolRecord();if(state.mode!=='tools')setMode('tools',false);else{renderTools();ensureToolDetail();}}
    else if(location.hash==='#scenarios'){if(state.mode!=='scenarios')setMode('scenarios',false);}
    else if(location.hash==='#rules'){if(state.mode!=='rules')setMode('rules',false);}
    else if(location.hash==='#agents'){if(state.mode!=='agents')setMode('agents',false);}
    else if(['tools','scenarios','rules','agents'].includes(state.mode)&&['#map','#encyclopedia',''].includes(location.hash))setMode(location.hash==='#encyclopedia'?'encyclopedia':'map',false);
  }
  function setMode(mode,updateLocation=true){
    if(!viewModes.includes(mode)||mode===state.mode)return;
    rulesState.outcome='';if(typeof scenariosState!=='undefined')scenariosState.outcome='';
    const queryKey={map:'mapQuery',encyclopedia:'conceptQuery',tools:'toolQuery',scenarios:'scenarioQuery',rules:'ruleQuery',agents:'agentQuery'};state[queryKey[state.mode]]=$('search').value;state.mode=mode;const encyclopedia=mode==='encyclopedia',tools=mode==='tools',scenariosMode=mode==='scenarios',rules=mode==='rules',agents=mode==='agents';hideSearch();
    $('canvas').hidden=mode!=='map';$('encyclopedia').hidden=!encyclopedia;$('tool-catalog').hidden=!tools;$('scenarios-panel').hidden=!scenariosMode;$('rules-panel').hidden=!rules;$('agents-panel').hidden=!agents;$('kind-filters').hidden=mode!=='map';document.querySelector('.switches').hidden=scenariosMode||rules||agents;document.querySelector('.project-field').hidden=agents;document.querySelector('.toolbar').hidden=scenariosMode&&typeof scenariosState!=='undefined'&&Boolean(scenariosState.editing);$('main').classList.toggle('tools-mode',tools||scenariosMode||rules||agents);$('main').classList.remove('tools-reading');
    for(const name of viewModes){const tab=$(name+'-tab');tab.setAttribute('aria-selected',String(name===mode));tab.tabIndex=name===mode?0:-1;}
    $('search-label').textContent=agents?'Найти агента':scenariosMode?'Найти сценарий, запуск или событие':rules?'Найти правило':tools?'Найти инструмент':encyclopedia?'Найти понятие':'Найти в памяти';$('project-label').textContent=scenariosMode||rules?'Область':tools?'Применялся в проекте':encyclopedia?'Область применения':'Вокруг проекта';
    $('search').placeholder=agents?'Имя, ID или папка…':scenariosMode?'ID, название, тег или шаг…':rules?'ID, текст или скилл…':tools?'Название, назначение, тема…':encyclopedia?'Термин, синоним, определение…':'Проект, задача, решение, тег…';$('search').value=state[queryKey[mode]]||'';
    $('view-hint').textContent=agents?'Агент → источники скиллов → правила':scenariosMode?'Перетаскивайте шаги, соединяйте порты мышкой (needs), отслеживайте запуски и журнал':rules?'Агенты загружают правила в начале чата и после сжатия контекста':tools?'Инструмент → проект → задачи':encyclopedia?'Выберите определение, чтобы открыть полную карточку':'Перетаскивайте карту и узлы · колесо — масштаб';$('data-note').textContent=agents?agentsNote():scenariosMode?scenariosNote():rules?rulesNote():state.dataNote||'Локальная память · только чтение';renderProjectOptions();
    if(tools){closeToolRecord();renderTools();if((!state.toolsData||state.toolsFlags!==toolsFilterKey())&&(!state.toolsLoading||state.toolsRequestedFlags!==toolsFilterKey()))loadTools();ensureToolDetail();if(updateLocation)writeToolsLocation();}
    else if(scenariosMode){closeToolRecord();scenariosState.query=$('search').value;renderScenarios();if(!scenariosState.loading)loadScenarios();if(!rulesState.skills&&!rulesState.skillsLoading)loadSkills();if(updateLocation&&location.hash!=='#scenarios')history.pushState(null,'','#scenarios');}
    else if(rules){closeToolRecord();rulesState.query=$('search').value;renderRules();if(!rulesState.loading)loadRules();if(updateLocation&&location.hash!=='#rules')history.pushState(null,'','#rules');}
    else if(agents){closeToolRecord();agentsState.query=$('search').value;renderAgents();if(!agentsState.loading)loadAgents();if(!rulesState.data&&!rulesState.loading)loadRules();if(updateLocation&&location.hash!=='#agents')history.pushState(null,'','#agents');}
    else{
      if(updateLocation&&(location.hash.startsWith('#tools')||location.hash==='#scenarios'||location.hash==='#rules'||location.hash==='#agents'))history.pushState(null,'','#'+mode);
      if(encyclopedia){renderEncyclopedia();if(state.selection?.type==='node'&&kindFor(state.nodeMap.get(state.selection.id))==='concept')renderNodeDetail(state.nodeMap.get(state.selection.id));else{state.selection=null;showIntro();}}
      else{renderGraph(true);if(state.selection?.type==='node')renderNodeDetail(state.nodeMap.get(state.selection.id));else if(state.selection?.type==='edge')renderEdgeDetail(state.edgeMap.get(state.selection.id));else showIntro();}
    }
    announce(agents?'Агенты и их скиллы':scenariosMode?'Сценарии и пайплайны':rules?'Правила агентов':tools?'Каталог инструментов':encyclopedia?'Энциклопедия понятий':'Карта знаний');
  }

  async function loadWorkspace(){
    const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),5000);
    try{const response=await fetch('/api/health',{cache:'no-store',signal:controller.signal});if(!response.ok)throw new Error('HTTP '+response.status);const health=await response.json();const name=typeof health.workspace==='string'&&health.workspace?health.workspace:'отдельная конфигурация';state.workspaceName=typeof health.workspace==='string'&&health.workspace?health.workspace:null;state.mapStore=list(health.store_arguments).filter(item=>typeof item==='string');if(state.mode==='rules')renderRules();if(state.mode==='scenarios'&&!scenariosState.editing)renderScenarios();$('workspace-name').textContent='Workspace: '+name;document.title='Карта знаний · '+name;}
    catch(_error){$('workspace-name').textContent='Workspace: недоступен';}
    finally{clearTimeout(timeout);}
  }

  async function loadGraph(){
    // «Обновить» answers the note a save left (rulesState.outcome); the save's own reload keeps it.
    rulesState.outcome='';if(typeof scenariosState!=='undefined')scenariosState.outcome='';
    loadWorkspace();if(state.mode==='tools'&&(!state.toolsLoading||state.toolsRequestedFlags!==toolsFilterKey()))loadTools();if(state.mode==='scenarios'){loadScenarios();loadSkills();}if(state.mode==='rules'){loadRules();loadSkills();}if(state.mode==='agents'){loadAgents();loadRules();}
    if($('git-dialog').open)closeGit();
    const request=++state.request;state.loading=true;$('refresh').disabled=true;$('refresh').textContent='Обновляем…';$('error-banner').hidden=true;if(!state.loaded)showMessage('Загружаем карту','Читаем актуальные знания из локальной памяти.');
    const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),20000);
    try{const params=new URLSearchParams({include_hypotheses:$('hypotheses').checked?'1':'0',include_superseded:$('superseded').checked?'1':'0'});const response=await fetch('/api/graph?'+params,{cache:'no-store',signal:controller.signal});if(!response.ok)throw new Error('Сервер вернул HTTP '+response.status);const data=await response.json();if(!data||!Array.isArray(data.nodes)||!Array.isArray(data.edges))throw new Error('Неожиданный формат ответа сервера');if(request!==state.request)return;
      state.nodes=data.nodes.filter(n=>n&&typeof n.id==='string');state.nodeMap=new Map(state.nodes.map(n=>[n.id,n]));state.edges=data.edges.filter(e=>e&&typeof e.id==='string'&&state.nodeMap.has(e.from_ref)&&state.nodeMap.has(e.to_ref));state.edgeMap=new Map(state.edges.map(e=>[e.id,e]));state.adjacency=new Map(state.nodes.map(n=>[n.id,[]]));for(const edge of state.edges){state.adjacency.get(edge.from_ref).push(edge);if(edge.to_ref!==edge.from_ref)state.adjacency.get(edge.to_ref).push(edge);}state.kinds=new Set(state.nodes.map(kindFor));state.loaded=true;state.loading=false;state.truncated=Boolean(data.truncated);state.lastError='';if(state.conceptProject&&!state.nodeMap.has(state.conceptProject))state.conceptProject='';
      if(state.focus&&!state.nodeMap.has(state.focus))state.focus=null;if(state.selection&&!(state.selection.type==='node'?state.nodeMap:state.edgeMap).has(state.selection.id))state.selection=null;
      $('updated-at').textContent='Снимок: '+dateText(data.generated_at);state.dataNote=data.truncated?'Локальная память · только чтение · ответ ограничен, показана часть данных':'Локальная память · только чтение';$('data-note').textContent=state.mode==='scenarios'?scenariosNote():state.mode==='rules'?rulesNote():state.mode==='agents'?agentsNote():state.dataNote;renderFilters();renderProjectOptions();renderGraph(true);renderEncyclopedia();if(state.mode==='scenarios'&&!scenariosState.editing)renderScenarios();if(state.mode==='rules')renderRules();if(state.mode==='agents')renderAgents();if(state.mode==='tools'){renderTools();}else if(state.selection?.type==='node')renderNodeDetail(state.nodeMap.get(state.selection.id));else if(state.selection?.type==='edge')renderEdgeDetail(state.edgeMap.get(state.selection.id));else showIntro();if($('search').value&&state.mode!=='tools')runSearch();announce(`Загружено ${state.nodes.length} узлов и ${state.edges.length} связей`);
    }catch(error){if(request!==state.request)return;state.loading=false;const detail=error.name==='AbortError'?'Сервер не ответил за 20 секунд.':error.message;state.lastError=detail;renderEncyclopedia();if(state.loaded){$('error-banner').textContent='Не удалось обновить: '+detail+' На карте остался предыдущий снимок.';$('error-banner').hidden=false;}else showMessage('Не удалось загрузить карту',detail+' Проверьте, что локальный сервер работает.','retry');announce('Не удалось загрузить карту');}
    finally{clearTimeout(timeout);if(request===state.request){state.loading=false;$('refresh').disabled=false;$('refresh').textContent='↻ Обновить';}}
  }

  let drag=null;
  svg.addEventListener('pointerdown',event=>{if(event.button!==0)return;const node=event.target.closest('.node');if(event.target.closest('.edge-group')&&!node)return;drag={pointerId:event.pointerId,startX:event.clientX,startY:event.clientY,lastX:event.clientX,lastY:event.clientY,nodeId:node?.dataset.id || null,moved:false,captureTarget:node || svg};drag.captureTarget.setPointerCapture(event.pointerId);if(!node)svg.classList.add('panning');});
  svg.addEventListener('pointermove',event=>{if(!drag||drag.pointerId!==event.pointerId)return;const dx=event.clientX-drag.lastX,dy=event.clientY-drag.lastY;if(Math.hypot(event.clientX-drag.startX,event.clientY-drag.startY)>4)drag.moved=true;if(drag.moved){if(drag.nodeId){const p=state.positions.get(drag.nodeId);if(p){p.x+=dx/state.camera.k;p.y+=dy/state.camera.k;drawPositions();}}else{state.camera.x+=dx;state.camera.y+=dy;applyCamera();}}drag.lastX=event.clientX;drag.lastY=event.clientY;});
  function endDrag(event){if(!drag||drag.pointerId!==event.pointerId)return;state.suppressClick=drag.moved;const captureTarget=drag.captureTarget;drag=null;svg.classList.remove('panning');if(captureTarget.hasPointerCapture(event.pointerId))captureTarget.releasePointerCapture(event.pointerId);setTimeout(()=>{state.suppressClick=false;},0);}
  svg.addEventListener('pointerup',endDrag);svg.addEventListener('pointercancel',endDrag);
  svg.addEventListener('wheel',event=>{event.preventDefault();const rect=svg.getBoundingClientRect();zoom(Math.exp(-event.deltaY*.0015),event.clientX-rect.left,event.clientY-rect.top);},{passive:false});
  $('zoom-in').addEventListener('click',()=>zoom(1.25));$('zoom-out').addEventListener('click',()=>zoom(.8));$('fit').addEventListener('click',fitGraph);$('reset').addEventListener('click',()=>resetView());$('refresh').addEventListener('click',loadGraph);$('hypotheses').addEventListener('change',loadGraph);$('superseded').addEventListener('change',loadGraph);$('message-action').addEventListener('click',()=>{$('message-action').dataset.action==='retry'?loadGraph():resetView();});
  $('project-filter').addEventListener('change',event=>{if(state.mode==='scenarios'){scenariosState.scope=event.target.value;if(!scenariosState.editing)renderScenarios();}else if(state.mode==='rules'){rulesState.scope=event.target.value;renderRules();}else if(state.mode==='tools'){state.toolFilter=event.target.value;openTool('');}else if(state.mode==='encyclopedia'){state.conceptProject=event.target.value;renderEncyclopedia();}else{event.target.value?focusNode(event.target.value):resetView(false);}});document.querySelectorAll('[data-depth]').forEach(b=>b.addEventListener('click',()=>{state.depth=Number(b.dataset.depth);renderGraph(true);}));
  $('search').addEventListener('input',runSearch);$('search').addEventListener('focus',()=>{if($('search').value&&state.mode!=='tools')runSearch();});$('search').addEventListener('keydown',event=>{if(event.key==='Escape'){hideSearch();return;}if(event.key==='ArrowDown'){event.preventDefault();$('search-results').querySelector('button')?.focus();}if(event.key==='Enter'&&state.mode==='tools'){$('tools-content').querySelector('[data-tool]')?.focus();}if(event.key==='Enter'&&state.mode==='encyclopedia'){$('concepts').querySelector('button')?.focus();}if(event.key==='Enter'&&state.mode==='map'){$('search-results').querySelector('button')?.click();}});$('search-results').addEventListener('keydown',event=>{const buttons=[...$('search-results').querySelectorAll('button')],index=buttons.indexOf(document.activeElement);if(event.key==='ArrowDown'){event.preventDefault();buttons[Math.min(index+1,buttons.length-1)]?.focus();}if(event.key==='ArrowUp'){event.preventDefault();index<=0?$('search').focus():buttons[index-1]?.focus();}if(event.key==='Escape'){hideSearch();$('search').focus();}});document.addEventListener('click',event=>{if(!event.target.closest('.search-wrap'))hideSearch();});
  $('close-details').addEventListener('click',()=>{if(state.mode==='tools'){closeToolRecord();$('tools-content').querySelector('.tool-detail-heading')?.focus({preventScroll:true});return;}state.sidebarOpen=false;$('main').classList.add('sidebar-closed');$('open-details').hidden=false;});$('open-details').addEventListener('click',openSidebar);
  let resizeTimer;new ResizeObserver(()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(state.loaded)fitGraph();},80);}).observe($('canvas'));
