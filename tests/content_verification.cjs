/** 执行真实content/verification/collection模块：验证打断扩页及详情，解除后原任务续采。 */
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const {create}=require('../extension/pxb7-extension/collection.js');
const content=fs.readFileSync('extension/pxb7-extension/content.js','utf8');
const verification=fs.readFileSync('extension/pxb7-extension/verification.js','utf8');
const sourceUrl='https://www.pxb7.com/buy/10302/1', itemUrl='https://www.pxb7.com/product/123456/1';
async function scenario(worker=false,reloaded=false,auto=null) {
  let blocked=worker||reloaded,loaded=reloaded?1:0,clicks=0,pauses=0,resumes=0,ingests=0,begins=0;
  let automaticAttempts=0;
  const storage={},handlers=[];
  const sender={tab:{id:worker?20:10,url:worker?itemUrl:sourceUrl},url:worker?itemUrl:sourceUrl};
  const text='80级，1个五星角色：安可；1个五星武器：精1千古洑流；详情看图';
  const slider={style:{},textContent:'',getBoundingClientRect:()=>({width:40,height:40})};
  const title={textContent:text};
  const chrome={storage:{local:{async get(k){return structuredClone({[k]:storage[k]});},async set(v){Object.assign(storage,structuredClone(v));}}},
    alarms:{async create(){},async clear(){},onAlarm:{addListener(){}}},
    tabs:{async create(){return{id:20};},async update(){},async remove(){},onRemoved:{addListener(){}},
      async sendMessage(id,msg){for(const handler of handlers)handler(msg,null,()=>{});return{ok:true};}}};
  const queue=create(chrome,async(path,payload)=>{ingests++;return{ok:true,data:{ok:true,snapshot_rows_updated:1}};});
  if(worker||reloaded){
    const begin=await queue.begin({tab:{id:10,url:sourceUrl},url:sourceUrl},{mode:worker?'detail':'list',total:1});
    if(worker)await queue.start({tab:{id:10},url:sourceUrl},{taskId:begin.taskId,items:[{id:'123456',url:itemUrl,prefix:'80级'}],round:'2026-10-05T18:30:00'});
    if(reloaded)await queue.verificationPause(sender,{taskId:begin.taskId});
  }
  function completeManually(){
    setTimeout(async()=>{
      const p=(await queue.progress()).progress;
      assert.equal(p.status,'waiting_verification');assert.equal(p.processed,0);assert.equal(p.failed,0);
      assert.equal(ingests,0,'验证未完成前不得入库');assert.ok(clicks<=1,'暂停期间不得继续扩页');
      blocked=false;loaded=1;
    },30);
  }
  chrome.runtime={getManifest:()=>({version:'0.5.4'}),onMessage:{addListener:fn=>handlers.push(fn)},sendMessage(msg,cb){
    let promise;
    if(msg.type==='config')return cb({ok:true,data:{ok:true,config:{auto_ingest:!reloaded,collection_mode:'list',cards_target:1,spa_settle_ms:1,verification_mode:auto?'auto_slider':'manual'}}});
    if(msg.type==='detail-context')promise=queue.context(sender);
    else if(msg.type==='collection-begin'){begins++;promise=queue.begin(sender,msg);}
    else if(msg.type==='verification-pause'){
      pauses++;promise=queue.verificationPause(sender,msg).then(reply=>{if(!auto)completeManually();return reply;});
    }else if(msg.type==='verification-attempt'){
      automaticAttempts++;promise=queue.claimVerificationAttempt(sender,msg).then(reply=>{
        assert.equal(reply.ok,true,'工作页必须使用暂停后的nonce');
        if(auto==='success'){blocked=false;loaded=1;}
        else completeManually();
        return {ok:true,inputSent:true};
      });
    }else if(msg.type==='verification-resume'){resumes++;promise=queue.verificationResume(sender,msg);}
    else if(msg.type==='collection-progress')promise=queue.update(sender,msg);
    else if(msg.type==='detail-result')promise=queue.result(sender,msg);
    else if(msg.type==='ingest'&&msg.path==='/collection/check'){
      return cb({ok:true,data:{ok:true,items:loaded?[{id:'123456',kind:'new'}]:[]}});
    }else if(msg.type==='ingest'){
      assert.equal(blocked,false);ingests++;return cb({ok:true,data:{ok:true,cards_parsed:1,new_listings:1,accepted_ids:['123456'],skipped_ids:[],updated_ids:[],
        items:[{id:'123456',kind:'new',missing:[],snapshot_round:'2026-10-05T18:30:00'}],round:'2026-10-05T18:30:00'}});
    }else return cb({ok:true});
    promise.then(cb).catch(e=>{throw e;});
  }};
  const doc={title:'鸣潮账号',body:{appendChild(){}},createElement:()=>({style:{}}),
    querySelectorAll:s=>s==='#aliyunCaptcha-sliding-slider'&&blocked?[slider]:[],
    querySelector:()=>worker?title:null,
    documentElement:{outerHTML:'list',cloneNode:()=>({outerHTML:'detail',querySelectorAll:()=>[{textContent:text,setAttribute(){}}]})}};
  const sandbox={console,Date,Promise,JSON,Number,String,document:doc,chrome,
    getComputedStyle:n=>n.style||{},sessionStorage:{getItem(){return null;},setItem(){}},
    location:{pathname:worker?'/product/123456/1':'/buy/10302/1',search:'',hash:'',href:sender.url},
    setTimeout:(fn,ms)=>{const t=setTimeout(fn,ms);t.unref();return t;},clearTimeout,setInterval(){},addEventListener(){},
    PXB7_SWEEP:{DEFAULTS:{intervalMs:0},clampTarget:n=>n,countCards:()=>loaded,
      findMoreControl:()=>({click(){clicks++;blocked=true;}}),nextAction:s=>s.cards>=s.target?'done-target':'click',
      selectCards:()=>loaded?[{id:'123456',url:itemUrl,card:{querySelector:()=>({getAttribute:()=>null,textContent:text})}}]:[],capture:()=>'<div>selected</div>'}};
  sandbox.window=sandbox;
  vm.runInNewContext(verification,sandbox);vm.runInNewContext(content,sandbox);
  if(reloaded)completeManually();
  for(let i=0;i<120;i++){
    await new Promise(r=>setTimeout(r,25));
    if((await queue.progress()).progress?.status==='done')break;
  }
  const progress=(await queue.progress()).progress;
  assert.equal(progress.status,'done');assert.equal(progress.succeeded,1);assert.equal(progress.failed,0);
  assert.equal(ingests,1,'恢复后不重复提交已成功账号');assert.equal(resumes,1);
  assert.equal(begins,worker||reloaded?0:1,'重载后的恢复复用原任务');
  assert.equal(pauses,reloaded?0:1);
  assert.equal(automaticAttempts,auto?1:0);
}
(async()=>{await scenario();await scenario(true);await scenario(false,true);
  await scenario(false,false,'success');await scenario(true,false,'success');await scenario(true,false,'failure');
  console.log('actual modules: manual/automatic list/worker recovery, failed input waits, source reload: PASS');})()
.catch(e=>{console.error(e);process.exitCode=1;});
