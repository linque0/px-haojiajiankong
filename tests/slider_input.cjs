const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const S = require('../extension/pxb7-extension/slider-input.js');
const {create} = require('../extension/pxb7-extension/collection.js');
const url = 'https://www.pxb7.com/buy/10302/1';
function fixture(geometry, attachFails=false) {
  const calls=[];
  let tabUrl=url;
  const chrome={tabs:{async get(){return {url:tabUrl};}},debugger:{
    async attach(){calls.push('attach');if(attachFails)throw Error('private error');},
    async detach(){calls.push('detach');},
    async sendCommand(target,method,params){calls.push({method,params});
      if(method==='Runtime.evaluate')return {result:{value:geometry}};}
  }};
  return {chrome,calls,setUrl:v=>tabUrl=v};
}
const geometry={url,x:40,y:80,endX:340,width:340,height:40,viewportWidth:1280,viewportHeight:900};
(async()=>{
  // 执行传给浏览器的真实几何函数，覆盖隐藏、遮挡与非普通滑块，不仅mock其返回值。
  let hidden=false,obscured=false,prompt='请按住滑块，拖动到最右边';
  const track={style:{},get innerText(){return prompt;},getBoundingClientRect:()=>({right:360,width:340})};
  const node={parentElement:track,getBoundingClientRect:()=>({left:20,top:60,width:40,height:40}),contains:()=>false};
  const env={location:{href:url},innerWidth:1280,innerHeight:900,
    getComputedStyle:el=>({display:hidden&&el===track?'none':'block',visibility:'visible'}),
    document:{querySelector:s=>s==='#aliyunCaptcha-sliding-slider'?node:track,elementFromPoint:()=>obscured?{}:node}};
  let evaluated=fixture(geometry);
  evaluated.chrome.debugger.sendCommand=async(target,method,params)=>{
    evaluated.calls.push({method,params});
    if(method==='Runtime.evaluate')return {result:{value:vm.runInNewContext(params.expression,env)}};
  };
  assert.equal((await S.attempt(evaluated.chrome,10,url,{sleep:async()=>{}})).inputSent,true);
  hidden=true;assert.equal((await S.attempt(evaluated.chrome,10,url)).error,'challenge-not-visible');
  hidden=false;obscured=true;assert.equal((await S.attempt(evaluated.chrome,10,url)).error,'challenge-obscured');
  obscured=false;prompt='拖动拼图到缺口';assert.equal((await S.attempt(evaluated.chrome,10,url)).error,'unsupported-challenge');
  assert.equal(S.allowedPage(url),true);
  for(const invalid of ['https://evil.com/buy/10302/1','https://www.pxb7.com.evil.com/buy/10302/1','http://www.pxb7.com/buy/10302/1','https://u@www.pxb7.com/buy/10302/1','https://www.pxb7.com/login']) assert.equal(S.allowedPage(invalid),false);
  let f=fixture(geometry);
  let result=await S.attempt(f.chrome,10,url,{sleep:async()=>{}});
  assert.deepEqual(result,{ok:true,inputSent:true});
  assert.equal(f.calls.filter(c=>c.method==='Input.dispatchMouseEvent'&&c.params.type==='mousePressed').length,1);
  assert.equal(f.calls.at(-1),'detach');
  assert.equal(f.calls.at(-2).params.type,'mouseReleased');
  assert.ok(f.calls.every(c=>typeof c==='string'||['Runtime.evaluate','Input.dispatchMouseEvent'].includes(c.method)));
  for(const g of [{error:'unsupported-challenge'},{...geometry,url:'https://evil.com/'},{...geometry,endX:Infinity},{...geometry,endX:2000}]){
    f=fixture(g); result=await S.attempt(f.chrome,10,url,{sleep:async()=>{}});
    assert.equal(result.ok,false);assert.equal(f.calls.filter(c=>c.method==='Input.dispatchMouseEvent').length,0);
    assert.equal(f.calls.at(-1),'detach');
  }
  f=fixture(geometry,true);result=await S.attempt(f.chrome,10,url);
  assert.equal(result.error,'debugger-unavailable');assert.deepEqual(f.calls,['attach'],'不能detach其他调试器');
  f=fixture(geometry);let alive=true;
  result=await S.attempt(f.chrome,10,url,{alive:()=>alive,sleep:async()=>{alive=false;}});
  assert.equal(result.error,'task-changed');assert.equal(f.calls.at(-1),'detach');
  assert.equal(f.calls.at(-2).params.type,'mouseReleased','取消释放鼠标');
  f=fixture(geometry);
  result=await S.attempt(f.chrome,10,url,{sleep:async()=>{f.setUrl('https://evil.com/');}});
  assert.equal(result.error,'task-changed');assert.equal(f.calls.at(-1),'detach');
  assert.equal(f.calls.filter(c=>c.method==='Input.dispatchMouseEvent'&&c.params.type==='mouseReleased').length,0,'跳到其他网站不继续输入');

  const storage={};
  const chrome={storage:{local:{async get(k){return structuredClone({[k]:storage[k]});},async set(v){Object.assign(storage,structuredClone(v));}}},
    tabs:{async update(){},onRemoved:{addListener(){}}},alarms:{async create(){},async clear(){},onAlarm:{addListener(){}}}};
  const sender={tab:{id:10,url},url},msg={pageUrl:url};
  let queue=create(chrome,async()=>{});
  msg.taskId=(await queue.begin(sender,{mode:'list',total:1})).taskId;
  await queue.verificationPause(sender,msg);
  assert.equal((await queue.claimVerificationAttempt({...sender,tab:{id:11,url}},msg)).ok,false);
  assert.equal((await queue.claimVerificationAttempt(sender,{...msg,taskId:'old'})).ok,false);
  const claim=await queue.claimVerificationAttempt(sender,msg);assert.equal(claim.ok,true);
  queue=create(chrome,async()=>{});
  assert.equal((await queue.claimVerificationAttempt(sender,msg)).error,'attempt-used','后台重启仍只尝试一次');
  assert.equal(await queue.verificationAttemptActive(sender,msg,claim.startedAt),true);
  assert.equal((await queue.progress()).progress.processed,0);
  await queue.verificationResume(sender,msg);
  assert.equal(await queue.verificationAttemptActive(sender,msg,claim.startedAt),false);
  // 实际background消息路由：配置关闭/其他扩展/子frame不得调用输入组件。
  await queue.verificationPause(sender,msg);
  let listener,mode='manual',inputs=0;
  const backgroundChrome={runtime:{id:'test-extension',getManifest:()=>({version:'0.5.14'}),
    onMessage:{addListener:fn=>listener=fn},onInstalled:{addListener(){}},onStartup:{addListener(){}}},
    alarms:{create(){},onAlarm:{addListener(){}}}};
  const sandbox={chrome:backgroundChrome,URL,URLSearchParams,AbortSignal,Promise,Date,JSON,setTimeout,
    importScripts(){},PXB7_COLLECTION:{create:()=>queue},
    PXB7_SLIDER_INPUT:{allowedPage:S.allowedPage,async attempt(c,id,u,opts){
      assert.equal(id,10);assert.equal(u,url);assert.equal(await opts.alive(),true);
      inputs++;return {ok:true,inputSent:true};}},
    fetch:async()=>({status:200,text:async()=>JSON.stringify({config:{verification_mode:mode}})})};
  vm.runInNewContext(fs.readFileSync('extension/pxb7-extension/background.js','utf8'),sandbox);
  const route=who=>new Promise(resolve=>listener({type:'verification-attempt',...msg},who,resolve));
  const who={...sender,id:'test-extension',frameId:0};
  assert.equal((await route(who)).error,'auto-disabled');assert.equal(inputs,0);
  mode='auto_slider';
  assert.equal((await route({...who,id:'other'})).error,'unsupported-page');
  assert.equal((await route({...who,frameId:1})).error,'unsupported-page');
  assert.equal(inputs,0);
  assert.equal((await route(who)).inputSent,true);assert.equal(inputs,1);
  assert.equal((await route(who)).error,'attempt-used');assert.equal(inputs,1);
  console.log('slider input: scope, geometry, cleanup, cancellation, persistent single attempt: PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
