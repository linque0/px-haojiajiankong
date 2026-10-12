/** 运行真实 content.js，检查详情空值状态不再被当成补齐失败。 */
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync('extension/pxb7-extension/content.js','utf8');
async function scenario(state,complete,expected,legacy=false) {
  const timers=[],sent=[],storage=new Map(),badges=[];
  const title='48级，1黄，星声67696，1个五星角色：维里奈；详情看图【官方截图】';
  const sandbox={console,Date,Promise,JSON,Number,String,
    location:{pathname:'/product/123456/1',href:'https://www.pxb7.com/product/123456/1',search:'',hash:''},
    document:{title:'鸣潮账号',querySelector:()=>({textContent:title}),
      documentElement:{outerHTML:'offline detail'},body:{appendChild(){}},
      createElement(){const badge={style:{}};badges.push(badge);return badge;}},
    sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
    setTimeout(fn){timers.push(fn);return timers.length;},clearTimeout(){},setInterval(){},addEventListener(){},
    chrome:{runtime:{getManifest:()=>({version:'0.5.13'}),onMessage:{addListener(){}},sendMessage(msg,cb){
      sent.push(msg);
      if(msg.type==='detail-context')return cb({ok:true,worker:false});
      if(msg.type==='config')return cb({ok:true,data:{ok:true,config:{auto_ingest:true,spa_settle_ms:1}}});
      if(msg.type==='collection-begin')return cb({ok:true,taskId:'details'});
      if(msg.type==='ingest')return cb({ok:true,data:{ok:true,attributes:state==='none'?{five_star_weapons:0}:{},
        weapon_details_complete:complete,...(legacy?{}:{weapon_information:{state}})}});
      cb({ok:true});
    }}}};
  sandbox.window=sandbox;vm.runInNewContext(source,sandbox);
  await new Promise(setImmediate);
  while(timers.length){timers.shift()();await new Promise(setImmediate);}
  const progress=sent.filter(msg=>msg.type==='collection-progress').at(-1);
  assert.equal(progress.status,expected);
  assert.equal(storage.size,expected==='done'?1:0);
  if(state==='none')assert.ok(badges[0].textContent.includes('已确认无五星武器'));
  if(state==='unreported')assert.ok(badges[0].textContent.includes('页面未提供武器信息'));
}
(async()=>{await scenario('none',true,'done');await scenario('unreported',true,'done');
  await scenario('unknown',false,'partial');await scenario('present',false,'partial');
  await scenario('unknown',false,'partial',true);
  console.log('content.js weapon none/unreported/unknown and legacy gateway: PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
