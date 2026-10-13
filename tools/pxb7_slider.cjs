#!/usr/bin/env node
/** 独立普通滑块执行器：Node 22+，仅连接用户提供的回环 Chrome CDP。 */
'use strict';
const S = require('../extension/pxb7-extension/slider-input.js');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

// 只读公开 DOM；不访问 Cookie、认证存储、验证码实例或校验参数。
function publicState() {
  function visible(el) {
    if (!el) return false;
    for (let n=el; n; n=n.parentElement) {
      const s=getComputedStyle(n);
      if (n.hidden || s.display==='none' || /hidden|collapse/.test(s.visibility)) return false;
    }
    const r=el.getBoundingClientRect(); return r.width>0 && r.height>0;
  }
  const challenge=/验证码|安全验证|访问验证|访问受限/.test(document.title) || Array.from(document.querySelectorAll('#WAF_NC_WRAPPER,#waf_nc_block,#nocaptcha,#aliyunCaptcha-sliding-slider,.nc_iconfont.btn_slide'))
    .some(el=>visible(el) && (/请按住滑块|访问验证|安全验证|拖动.{0,12}最右/.test(el.innerText||el.textContent||'') || el.id==='aliyunCaptcha-sliding-slider'));
  const path=location.pathname;
  const cards=Array.from(document.querySelectorAll('a[href*="/product/"]'))
    .filter(el=>visible(el) && /[￥¥]\s*[0-9]/.test(el.innerText||el.textContent||''));
  const code=(document.title.match(/【([A-Za-z0-9_-]{3,80})】/)||[])[1];
  const body=document.body?.innerText||'';
  // 业务恢复探针，不是采集完整性判定；推荐卡片不能替代当前详情。
  const detailReady=!!code && body.includes('【'+code+'】') && /[￥¥]\s*[0-9]/.test(body) && /账号详情|商品描述|验号报告/.test(body);
  const ready=!challenge && (/^\/buy\/\d+(?:\/\d+)?\/?$/.test(path) ? cards.length>0 : detailReady);
  return {url:location.href,blocked:challenge,businessReady:ready};
}

async function readState(send) {
  const result=await send('Runtime.evaluate',{expression:'('+publicState.toString()+')()',returnByValue:true});
  const state=result.result?.value;
  if (!state || typeof state.url!=='string' || typeof state.blocked!=='boolean' || typeof state.businessReady!=='boolean') throw Error('state-unavailable');
  return state;
}

async function runOnce(send, url, options={}) {
  if (!S.allowedPage(url)) return {ok:false,error:'unsupported-page'};
  const pause=options.sleep||sleep, now=options.now||Date.now;
  const state=await readState(send);
  if (state.url!==url) return {ok:false,error:'page-changed'};
  if (!state.blocked) return {ok:false,error:'no-visible-challenge',businessReady:state.businessReady};
  // 适配现有扩展程序：CDP连接已由调用方取得；不附着/断开别人的调试器。
  const chrome={tabs:{get:async()=>({url:(await readState(send)).url})},debugger:{
    attach:async()=>{},detach:async()=>{},sendCommand:async(_,method,params)=>send(method,params)}};
  const input=await S.attempt(chrome,1,url,{sleep:pause,alive:()=>!options.cancelled?.()});
  if (!input.inputSent) return input;
  const until=now()+(options.timeoutMs??20000); let clearSince=null;
  while (now()<until && !options.cancelled?.()) {
    const s=await readState(send);
    if (s.url!==url) return {ok:false,inputSent:true,error:'page-changed'};
    if (!s.blocked && s.businessReady) {
      if (clearSince===null) clearSince=now();
      if (now()-clearSince>=1000) return {ok:true,inputSent:true,verificationCleared:true,businessReady:true};
    } else clearSince=null;
    await pause(250);
  }
  return {ok:false,inputSent:true,error:options.cancelled?.()?'cancelled':'verification-or-business-timeout'};
}

function endpoint(value, protocol='http:') {
  const u=new URL(value);
  if (u.protocol!==protocol || !['127.0.0.1','localhost','[::1]'].includes(u.hostname) || u.username || u.password || u.search || u.hash) throw Error('loopback-endpoint-required');
  return u;
}

async function connect(wsUrl) {
  const url=endpoint(wsUrl,'ws:');
  const ws=new WebSocket(url); let id=0; const pending=new Map();
  function failAll() { for (const p of pending.values()) {clearTimeout(p.timer);p.reject(Error('cdp-disconnected'));} pending.clear(); }
  ws.addEventListener('close',failAll); ws.addEventListener('error',failAll);
  ws.addEventListener('message',event=>{
    let msg; try {msg=JSON.parse(event.data);} catch (_) {return;}
    const p=pending.get(msg.id); if (!p) return;
    pending.delete(msg.id); clearTimeout(p.timer);
    if (msg.error) p.reject(Error('cdp-command-failed')); else p.resolve(msg.result);
  });
  await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{ws.close();reject(Error('cdp-connect-timeout'));},5000);
    ws.addEventListener('open',()=>{clearTimeout(timer);resolve();},{once:true});
    ws.addEventListener('error',()=>{clearTimeout(timer);reject(Error('cdp-connect-failed'));},{once:true});
  });
  return {send:(method,params)=>new Promise((resolve,reject)=>{
    if (!['Runtime.evaluate','Input.dispatchMouseEvent'].includes(method)) return reject(Error('unsupported-command'));
    const seq=++id, timer=setTimeout(()=>{pending.delete(seq);reject(Error('cdp-command-timeout'));},5000);
    pending.set(seq,{resolve,reject,timer});
    try {ws.send(JSON.stringify({id:seq,method,params}));} catch (_) {clearTimeout(timer);pending.delete(seq);reject(Error('cdp-disconnected'));}
  }),close:()=>{failAll();ws.close();}};
}

function parseArgs(argv) {
  const args={watchSeconds:0};
  for (let i=0;i<argv.length;i++) {
    const k=argv[i];
    if (k==='--help') return {help:true};
    if (!['--cdp','--target','--watch-seconds'].includes(k) || i+1>=argv.length) throw Error('invalid-arguments');
    const value=argv[++i];
    if (k==='--cdp') args.cdp=endpoint(value).origin;
    else if (k==='--target') args.target=value;
    else {args.watchSeconds=Number(value);if (!Number.isInteger(args.watchSeconds)||args.watchSeconds<1||args.watchSeconds>86400) throw Error('invalid-watch-seconds');}
  }
  if (!args.cdp || !args.target || !/^[A-Za-z0-9_-]{1,100}$/.test(args.target)) throw Error('cdp-and-target-required');
  return args;
}

async function main(argv) {
  const args=parseArgs(argv);
  if (args.help) {
    console.log('用法: node tools/pxb7_slider.cjs --cdp http://127.0.0.1:9222 --target <页面targetId> [--watch-seconds 3600]\n仅连接已启用CDP的专用Chrome；不启动/重启浏览器。每次可见普通滑块尝试一次，失败停止。');
    return;
  }
  const response=await fetch(args.cdp+'/json/list',{signal:AbortSignal.timeout(5000),redirect:'error'});
  if (!response.ok) throw Error('cdp-discovery-failed');
  const targets=await response.json();
  const target=targets.find(t=>t.id===args.target && t.type==='page');
  if (!target || !S.allowedPage(target.url)) throw Error('supported-target-required');
  const wsUrl=endpoint(target.webSocketDebuggerUrl,'ws:');
  if (wsUrl.port!==new URL(args.cdp).port) throw Error('unexpected-cdp-port');
  const conn=await connect(wsUrl.href); let cancelled=false, armed=true, readySince=null;
  const stop=()=>{cancelled=true;}; process.once('SIGINT',stop); process.once('SIGTERM',stop);
  const deadline=Date.now()+args.watchSeconds*1000;
  try {
    do {
      const state=await readState(conn.send);
      if (!S.allowedPage(state.url)) throw Error('page-left-supported-site');
      if (state.blocked && armed) {
        armed=false; readySince=null;
        const result=await runOnce(conn.send,state.url,{cancelled:()=>cancelled});
        console.log(JSON.stringify({at:new Date().toISOString(),...result}));
        if (!result.ok) {process.exitCode=2;return;}
        // runOnce 已确认业务稳定恢复1秒；下一次阻断才是新的等待事件。
        armed=true; readySince=null;
        if (!args.watchSeconds) return;
      } else if (!state.blocked && state.businessReady) {
        if (readySince===null) readySince=Date.now();
        if (Date.now()-readySince>=1000) armed=true;
      } else readySince=null;
      if (!args.watchSeconds) {
        console.log(JSON.stringify({ok:state.businessReady,inputSent:false,businessReady:state.businessReady,
          ...(state.businessReady?{}:{error:'business-not-ready'})}));
        if (!state.businessReady) process.exitCode=2;
        return;
      }
      await sleep(500);
    } while (!cancelled && Date.now()<deadline);
  } finally {process.removeListener('SIGINT',stop);process.removeListener('SIGTERM',stop);conn.close();}
}

module.exports={publicState,readState,runOnce,endpoint,parseArgs,connect};
if (require.main===module) main(process.argv.slice(2)).catch(()=>{console.error('运行失败：请核对 Node 22+、回环 CDP 地址、目标页面及调试器连接。');process.exitCode=1;});
