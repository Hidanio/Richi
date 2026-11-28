'use strict';
  for(const mode of viewModes){const tab=$(mode+'-tab');tab.addEventListener('click',()=>{
    if(mode==='scenarios'&&state.mode==='scenarios'&&scenariosState.editing){closeScenarioEditor();return;}
    setMode(mode);
  });tab.addEventListener('keydown',event=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const index=viewModes.indexOf(mode),next=event.key==='Home'?0:event.key==='End'?viewModes.length-1:(index+(event.key==='ArrowLeft'?-1:1)+viewModes.length)%viewModes.length;setMode(viewModes[next]);$(viewModes[next]+'-tab').focus();
  });}
  window.addEventListener('keydown',event=>{
    if((event.metaKey||event.ctrlKey)&&!event.shiftKey&&event.key.toLowerCase()==='z'&&state.mode==='scenarios'&&scenariosState.editing){
      const tag=document.activeElement?.tagName;
      if(tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT'||document.activeElement?.isContentEditable)return;
      if(scenariosState.editing.undoStack.length){event.preventDefault();undoScenarioEdit();}
    }
  });
  window.addEventListener('beforeunload',event=>{
    if(scenariosState.editing?.dirty){event.preventDefault();event.returnValue='';}
  });
  window.addEventListener('popstate',applyToolsLocation);
  window.addEventListener('hashchange',applyToolsLocation);
  window.addEventListener('hashchange',redeemRuleTicket);
  redeemRuleTicket();showIntro();applyToolsLocation();loadGraph();
