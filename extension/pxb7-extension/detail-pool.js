/** One logical task, bounded reusable worker tabs; transitions run under collection.serial. */
(function(root){
  "use strict";
  function create(h) {
    const {chrome,save,view,finish,reportBatch,ingest,batchIdentity,retryAction}=h;
    const slotFor=(task,sender)=>task.slots?.find(s=>s.tabId===sender.tab?.id);
    const itemFor=(task,slot)=>slot && task.items[slot.index];
    async function arm(task) {
      const busy=task.slots.filter(s=>s.index!==null);
      if (task.status==="running" && busy.length)
        await chrome.alarms.create(h.alarm,{when:Math.max(Date.now()+1000,Math.min(...busy.map(s=>s.startedAt+60000)))});
    }
    async function fill(task) {
      if (task.status!=="running") return {ok:true,held:true,progress:view(task)};
      for(let slotNumber=0;slotNumber<task.slots.length;slotNumber++) {
        const slot=task.slots[slotNumber];
        if(slot.index!==null || task.access_exhausted)continue;
        const index=task.items.findIndex(i=>i.state==="pending");
        if(index<0)break;
        const item=task.items[index];
        if((item.kind==="retry" || item.retryQueued) && !item.retryClaimed) {
          const r=await retryAction(item,"claim",task);
          if(!(r?.ok && r.data?.ok)) {task.access_exhausted=r?.data?.error || "retry-claim-failed";break;}
          if(!r.data.claimed) {item.state="skipped";task.processed++;task.manual_pending=(task.manual_pending||0)+1;slot.index=null;slotNumber--;continue;}
          item.retryClaimed=true;task.retried++;
        }
        if(task.nextNavigationAt>Date.now())await h.pause(task.nextNavigationAt-Date.now());
        // All slots use the same budget and the same navigation cadence.
        const permit=await h.access({...batchIdentity(task),action:"detail",id:item.id});
        if(!permit?.data?.ok){task.access_exhausted=permit?.data?.error || "access-budget-unavailable";break;}
        slot.index=index;slot.permit=permit.data.permit_id;slot.startedAt=Date.now();slot.nonce=`${task.id}:${index}:${Date.now()}:${Math.random()}`;
        item.state="fetching";item.attempt=slot.nonce;task.nextNavigationAt=Date.now()+task.detail_interval_ms;
        await save(task); // persisted before navigation; results may arrive out of order
        try {await chrome.tabs.update(slot.tabId,{url:item.url});}
        catch(_) {await failItem(task,slot,"detail-tab-unavailable");}
      }
      task.in_flight=task.slots.filter(s=>s.index!==null).length;
      task.phase=`详情快速采集：完整 ${task.complete_count||0}/${task.requested} · 在途 ${task.in_flight} · 并发 ${task.concurrency}`;
      if(!task.in_flight && (task.access_exhausted || !task.items.some(i=>i.state==="pending"))) {
        const short=task.items.filter(i=>i.kind==="new" && i.state==="complete").length<task.requested && task.purpose==="discover";
        return finish(task,task.failed || task.incomplete || short || task.access_exhausted ? "partial":"done",
          `快速采集结束：完整 ${task.complete_count||0}/${task.requested}，失败 ${task.failed}，缺项 ${task.incomplete}`+(task.access_exhausted ? `；${task.access_exhausted}`:""));
      }
      await save(task);await arm(task);return {ok:true,progress:view(task)};
    }
    async function start(task) {
      const c=task.batch.config;
        task.concurrency=Number.isInteger(c.detail_concurrency) && c.detail_concurrency>=1 && c.detail_concurrency<=8 ? c.detail_concurrency:1;
      task.slots=[];task.complete_count=0;task.response_complete=0;task.dom_fallback=0;
      task.verification_count ||= 0;
      task.timings={parse_ms:0,commit_ms:0,discovery_ms:Date.now()-task.startedAt,request_ms:0,submit_ms:0,gateway_ms:0,
        verification_ms:task.timings?.verification_ms || 0};
      task.items.forEach(i=>{i.state="pending";});
      try {for(let i=0;i<Math.min(task.concurrency,task.items.length);i++)task.slots.push({tabId:(await chrome.tabs.create({url:"about:blank",active:false})).id,index:null});}
      catch(_){return finish(task,"error","快速工作标签页创建失败");}
      // sentinel keeps source lifecycle logic separate from worker slots
      task.workerTab=task.slots[0].tabId;await save(task);return fill(task);
    }
    function context(task,sender,msg) {
      const slot=slotFor(task,sender),item=itemFor(task,slot);
      if(!slot)return null;
      if(!item || !h.samePage(sender,item.url,msg.pageUrl))return {ok:true,worker:true};
      return {ok:true,worker:true,job:{...item,taskId:task.id,nonce:slot.nonce,snapshot_round:item.snapshot_round || task.round,
        batch:task.batch,config:task.batch.config,startedAt:slot.startedAt,
        waiting_verification:!!slot.verification,held:task.status==="waiting_verification",fast:true}};
    }
    function eventFor(task,sender) {return slotFor(task,sender)?.verification;}
    function owns(task,sender,reported) {
      const slot=slotFor(task,sender),item=itemFor(task,slot);
      return !!slot?.verification && !!item && h.samePage(sender,item.url,reported);
    }
    async function block(task,slot) {
      if(!slot.verification) {
        slot.verification={tabId:slot.tabId,stage:"detail",phase:task.phase,startedAt:Date.now()};
        slot.nonce=`${task.id}:${slot.index}:verification:${Date.now()}`;
        task.verification ||= slot.verification;
        task.verification_count++;task.verification_started_at ||= Date.now();task.status="waiting_verification";
        task.phase="自动处理网站验证；任务与账号进度保留，解除后续采";
        (task.events ||= []).push({type:"challenge_detected",at:Date.now(),id:itemFor(task,slot).id});
        await chrome.alarms.clear(h.alarm);await save(task);await reportBatch(task);
        for(const s of task.slots) {try{await chrome.tabs.sendMessage(s.tabId,{type:"detail-hold",taskId:task.id});}catch(_){}}
        try{await chrome.tabs.update(slot.tabId,{active:true});}catch(_){}
      }
      return {ok:true,paused:true,nonce:slot.nonce,progress:view(task)};
    }
    async function resume(task,slot) {
      (task.events ||= []).push({type:"challenge_cleared",at:Date.now(),id:itemFor(task,slot).id,
        waiting_ms:Date.now()-slot.verification.startedAt,intervention:slot.verification.autoAttempted ? "automatic_attempt":"unknown"});
      delete slot.verification;
      task.verification=task.slots.find(s=>s.verification)?.verification;
      if(!task.verification) {
        task.timings.verification_ms+=Date.now()-task.verification_started_at;delete task.verification_started_at;
        task.status="running";for(const s of task.slots)if(s.index!==null)s.startedAt=Date.now();
      }
      await save(task);await reportBatch(task);await fill(task);
      return {ok:true,nonce:slot.nonce,held:task.status!=="running",progress:view(task)};
    }
    async function failItem(task,slot,error) {
      const item=itemFor(task,slot);
      if(error==="risk-page" || error==="verification")return block(task,slot);
      // A single bounded retry for navigation/read failures. Local ingest errors never cause another site visit.
      if(["detail-timeout","detail-not-ready","detail-read-error"].includes(error)
          && ["new","changed"].includes(item.kind) && !item.retryQueued) {
        item.retryQueued=true;item.state="pending";item.error=error;
        task.retry_failures=(task.retry_failures||0)+1;slot.index=null;slot.nonce=null;
        return {ok:true};
      }
      item.state="failed";item.error=error;task.failed++;task.processed++;
      task.errors.push({id:item.id,error});
      if(item.retryClaimed)await retryAction(item,"failed",task);
      slot.index=null;slot.nonce=null;return {ok:true};
    }
    async function result(task,sender,msg) {
      const slot=slotFor(task,sender),item=itemFor(task,slot);
      if(!item || task.id!==msg.taskId || slot.nonce!==msg.nonce || item.state!=="fetching")return {ok:false,err:"过期详情结果"};
      if(msg.error==="verification" || msg.error==="risk-page")return block(task,slot);
      if(task.status==="waiting_verification")return {ok:true,held:true,nonce:slot.nonce};
      if(task.status!=="running")return {ok:false,err:"任务已变化"};
      if(msg.error){await failItem(task,slot,String(msg.error).slice(0,80));return fill(task);}
      if(!msg.payload || msg.payload.listing_id!==item.id || msg.payload.url!==item.url) {await failItem(task,slot,"detail-id-mismatch");return fill(task);}
      const structured=msg.payload.schema_version===1;
      const submitted=Date.now();
      const res=await ingest(structured ? "/ingest/detail-data":"/ingest/detail",{...msg.payload,snapshot_round:item.snapshot_round || task.round,
        ...batchIdentity(task),attempt_id:slot.nonce,access_permit:slot.permit,...(item.retryToken ? {retry_token:item.retryToken}:{})});
      task.timings.submit_ms+=Date.now()-submitted;
      task.timings.gateway_ms+=Number(res.data?.gateway_duration_ms)||0;
      task.timings.parse_ms+=Number(res.data?.parse_duration_ms)||0;
      task.timings.commit_ms+=Number(res.data?.commit_duration_ms)||0;
      if(!(res.ok && res.data?.ok && res.data.snapshot_rows_updated>0)) {
        if(structured && res.data?.fallback && !item.fallbackUsed) {
          item.fallbackUsed=true;task.dom_fallback++;await save(task);
          return {ok:true,fallback:true,reason:res.data.error};
        }
        const error=res.data?.error || res.err || "detail-not-stored";
        const failed=await failItem(task,slot,error);if(failed.paused)return failed;return fill(task);
      }
      const incomplete=Array.isArray(res.data.missing) && res.data.missing.length>0;
      item.state=incomplete ? "incomplete":"complete";item.committedAt=Date.now();
      item.complete=!incomplete;task.succeeded++;task.processed++;
      if(incomplete)task.incomplete++;else task.complete_count++;
      if(structured)task.response_complete++;else if(!item.fallbackUsed)task.dom_fallback++;
      
      if(item.kind==="new" && !task.newIds.includes(item.id)){task.newIds.push(item.id);task.new_succeeded++;}
      task.timings.request_ms+=Number(msg.timings?.request_ms)||0;
      (task.events ||= []).push({type:"item_committed",at:Date.now(),id:item.id,attempt:slot.nonce,path:structured ? "response":"dom",complete:!incomplete});
      slot.index=null;slot.nonce=null;
      // Persist acknowledgement before reporting/refilling; no repeated site access for export failures.
      await save(task);await reportBatch(task);return fill(task);
    }
    async function timeout(task) {
      for(const slot of task.slots)if(slot.index!==null && Date.now()-slot.startedAt>=60000)await failItem(task,slot,"detail-timeout");
      return fill(task);
    }
    return {start,context,slotFor,itemFor,owns,eventFor,block,resume,result,timeout};
  }
  root.PXB7_DETAIL_POOL={create};
  if(typeof module!=="undefined")module.exports=root.PXB7_DETAIL_POOL;
})(globalThis);
