const assert = require('node:assert/strict');
const V = require('../extension/pxb7-extension/verification.js');
// 2026-10-05真实页面的阿里云滑块结构（仅公开控件id，不含校验参数/TraceID）。
const host = {style:{display:'block'}};
const slider = {parentElement:host, style:{}, textContent:'', getBoundingClientRect:()=>({width:40,height:40})};
const doc = {title:'鸣潮账号',querySelectorAll:s=>s==='#aliyunCaptcha-sliding-slider' ? [slider] : []};
const win = {getComputedStyle:n=>n.style || {}};
assert.equal(V.detect(doc,win).kind,'slider');
host.style.display='none'; assert.equal(V.detect(doc,win).blocked,false,'验证后的隐藏祖先不误报');
host.style.display='block'; host.style.visibility='hidden'; assert.equal(V.detect(doc,win).blocked,false);
host.style.visibility='visible'; slider.isConnected=false; assert.equal(V.detect(doc,win).blocked,false);
slider.isConnected=true;
const emptySDK = {style:{},textContent:'',getBoundingClientRect:()=>({width:348,height:40})};
assert.equal(V.detect({title:'账号',querySelectorAll:s=>s==='.aliyunCaptcha-show' ? [emptySDK] : []},win).blocked,false);

async function sequence(states, options={}) {
  let index=0, time=0, paused=0, resumed=0;
  const page={title:'账号',querySelectorAll:s=>states[Math.min(index,states.length-1)] && s==='#aliyunCaptcha-sliding-slider' ? [slider] : []};
  const result=await V.waitForClear(page,win,{now:()=>time,sleep:async()=>{index++;time+=250;},
    stableMs:500,alive:()=>index<20,onBlocked:async()=>{paused++;},onClear:async()=>{resumed++;},...options});
  return {result,paused,resumed,index};
}
(async()=>{
  let s=await sequence([true,true,false,true,false,false,false]);
  assert.equal(s.result.ok,true);assert.equal(s.paused,1);assert.equal(s.resumed,1);
  assert.ok(s.index>=6,'短暂消失又出现不能提前续采');
  s=await sequence([true],{alive:()=>false});assert.equal(s.result.ok,false);assert.equal(s.resumed,0);
  s=await sequence([false],{alreadyPaused:true,requireObserved:true});
  assert.equal(s.result.ok,false);assert.equal(s.resumed,0,'网关拒收但未观察到验证解除时不定时重试');
  s=await sequence([false],{alreadyPaused:true,requireObserved:true,confirmed:()=>true});
  assert.equal(s.result.ok,true);assert.equal(s.resumed,1,'用户确认后仍观察稳定解除再继续');
  let attempts=0;
  s=await sequence([true],{onAttempt:async()=>{attempts++;return {ok:true,inputSent:true};}});
  assert.equal(attempts,1);assert.equal(s.resumed,0,'输入执行不代表验证通过');
  attempts=0;
  s=await sequence([true,true,false,false,false],{onAttempt:async()=>{attempts++;}});
  assert.equal(attempts,1);assert.equal(s.resumed,1,'自动尝试后仍等稳定解除');
  console.log('visible slider detection, hidden SDK, stable recovery, cancellation and explicit recovery: PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
