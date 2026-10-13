/** MAIN 世界，document_start。观察公开商品响应；认证头仅在页面内存，不送网关。 */
(function (win) {
  "use strict";
  const API = "https://api-pc.pxb7.com";
  const ENDPOINT = API + "/api/search/product/selectTitleByCode";
  const nativeFetch = win.fetch.bind(win);
  const cache = new Map();
  const details = new Map();
  function observeDetail(data) {
    const value=win.PXB7_DETAIL_DATA?.normalize(data);
    if (value) { details.set(value.product.id,value); if(details.size>16)details.delete(details.keys().next().value); }
  }
  function detailUrl(url) {
    try {const u=new URL(url,win.location.href);return u.origin===API && u.pathname==="/api/product/web/product/detailPost";}catch(_){return false;}
  }
  win.addEventListener("pxb7-detail-request",event=>{
    try {const q=JSON.parse(event.detail);if(typeof q.token!=="string" || q.token.length>100 || !/^\d{6,24}$/.test(q.id))return;
      win.dispatchEvent(new win.CustomEvent("pxb7-detail-result",{detail:JSON.stringify({token:q.token,id:q.id,value:details.get(q.id) || null})}));
    }catch(_){}
  });
  let requestHeaders = null, active = false, nextRequest = 0, requestsInBatch = 0;
  // 全文请求间隔（毫秒）：0–10000（0–10 秒，0.1 秒步进由设置界面保证）；
  // 未提供/非法回落默认 3000；0 = 用户显式不等待，不得当缺省吞掉。
  const intervalFor = ms => {
    if (ms === undefined || ms === null || ms === "") return 3000;
    const n = Number(ms);
    return Number.isFinite(n) ? Math.max(0, Math.min(10000, n)) : 3000;
  };
  const pause = ms => new Promise(resolve => win.setTimeout(resolve, ms));
  function allowed(url) {
    try {
      const u = new URL(url, win.location.href);
      return u.origin === API && /^\/api\/(?:search\/product\/|bff\/web\/product\/search\/)/.test(u.pathname);
    } catch (_) { return false; }
  }
  function remember(id, code, title, source="list-response") {
    if (typeof id === "number" && !Number.isSafeInteger(id)) return; // 大整数 ID 必须由原接口字符串提供。
    if (!/^\d{6,24}$/.test(String(id)) || typeof title !== "string" || title.length > 50000) return;
    cache.set(String(id), {code, title, source, time:Date.now()});
    if (cache.size > 500) cache.delete(cache.keys().next().value);
  }
  function observe(data, headers) {
    if (!data || data.success !== true) return;
    requestHeaders = new Headers(headers || {});
    // 复用已观察的正常请求字段，不读取 Cookie/localStorage，不复制验证凭据。
    for (const name of [...requestHeaders.keys()]) {
      if (!/^(?:content-type|accept|client_type|os_type|px-authorization-(?:merchant|user)|device_id|gio_device|user_id)$/.test(name)) requestHeaders.delete(name);
    }
    let visited = 0;
    function walk(value, depth) {
      if (!value || typeof value !== "object" || depth > 8 || ++visited > 6000) return;
      if (!Array.isArray(value)) {
        const basic = value.productBasicInfo || value;
        // showTitle/smallImgShowTitle 可能截断；只缓存 productName 完整原文。
        if (basic.productId && basic.productName) remember(basic.productId, basic.productUniqueNo, basic.productName);
      }
      for (const child of Object.values(value)) walk(child, depth + 1);
    }
    walk(data.data, 0);
  }
  win.fetch = function (input, init) {
    const promise = nativeFetch(input, init);
    const url = typeof input === "string" ? input : input?.url;
    if (allowed(url) || detailUrl(url)) promise.then(response => {
      if (response.ok && /json/i.test(response.headers.get("content-type") || "")) {
        response.clone().json().then(data => detailUrl(url) ? observeDetail(data) : observe(data, init?.headers || input?.headers)).catch(() => {});
      }
    }, () => {});
    return promise;
  };
  const proto = win.XMLHttpRequest?.prototype;
  if (proto) {
    const open = proto.open, set = proto.setRequestHeader, send = proto.send;
    const requests = new WeakMap();
    proto.open = function (method, url, ...rest) {
      requests.set(this, {url, headers:{}});
      return open.call(this, method, url, ...rest);
    };
    proto.setRequestHeader = function (name, value) {
      const info = requests.get(this);
      if (info) info.headers[name] = value;
      return set.call(this, name, value);
    };
    proto.send = function (...args) {
      const info = requests.get(this);
      if (info && (allowed(info.url) || detailUrl(info.url))) this.addEventListener("load", () => {
        try {
          if (this.status >= 200 && this.status < 300) {
            const data=this.responseType === "json" ? this.response : JSON.parse(this.responseText);
            if(detailUrl(info.url))observeDetail(data);else observe(data,info.headers);
          }
        } catch (_) { /* 非 JSON 忽略 */ }
      }, {once:true});
      return send.apply(this, args);
    };
  }
  function cardFor(id) {
    return Array.from(win.document.querySelectorAll(".middleCard[productid]"))
      .find(card => card.getAttribute("productid") === id);
  }
  function riskVisible() {
    return Array.from(win.document.querySelectorAll("#aliyunCaptcha-window-popup, .aliyunCaptcha-sliding-body, #WAF_NC_WRAPPER"))
      .some(node => node.getClientRects().length > 0 && win.getComputedStyle(node).visibility !== "hidden");
  }
  function requestPermit(id) {
    return new Promise(resolve=> {
      const token=String(Date.now())+":"+String(Math.random());
      const finish=value=> {win.clearTimeout(timer);win.removeEventListener("pxb7-access-result",receive);resolve(value);};
      const receive=event=> {try {const result=JSON.parse(event.detail);if(result.token===token)finish(result);}catch (_) {}};
      const timer=win.setTimeout(()=>finish({ok:false,error:"access-permit-timeout"}),15000);
      win.addEventListener("pxb7-access-result",receive);
      win.dispatchEvent(new win.CustomEvent("pxb7-access-request",{detail:JSON.stringify({token,id})}));
    });
  }
  async function lookup(id, options = {}) {
    const diagnostics=[];
    const result=await lookupWithDiagnostics(id,{...options,diagnostics});
    return {...result,diagnostics};
  }
  async function lookupWithDiagnostics(id, options = {}) {
    const interval = intervalFor(options.intervalMs);
    let attempts = 0;
    const card = cardFor(id);
    if (!card) return {error:"list-change"};
    const titleNode = card.querySelector(".smallCardTitle, .bigCardTitle");
    const code = titleNode?.getAttribute("productuniqueno");
    const cached = cache.get(id);
    const normalized = text => String(text || "").replace(/[^\p{L}\p{N}]/gu, "");
    const prefix = normalized(titleNode?.getAttribute("productname") || titleNode?.textContent);
    if (!options.refresh && cached && cached.code === code && Date.now() - cached.time < 600000 && prefix.length >= 24
        && normalized(cached.title).startsWith(prefix)
        && (normalized(cached.title).length > prefix.length || /详情看图|官方截图/.test(cached.title))) {
      return {title:cached.title, source:cached.source, observed_at:cached.time, requested:false, attempts:0};
    }
    if (options.cacheOnly) return {available:false};
    if (riskVisible()) return {error:"verification"};
    if (!requestHeaders) return {error:"session-not-ready"};
    if (!code || !/^[A-Za-z0-9_-]{3,80}$/.test(code)) return {error:"missing-code"};
    const pageUrl = win.location.href;
    const current = () => win.location.href === pageUrl && card.isConnected && cardFor(id) === card;
    for (let attempt = 0; attempt < 2; attempt++) {
      if (requestsInBatch >= 8) {
        nextRequest = Math.max(nextRequest, Date.now() + Math.max(8000, interval * 2));
        requestsInBatch = 0;
      }
      await pause(Math.max(0, nextRequest - Date.now()));
      if (!current()) return {error:"route-change"};
      if (riskVisible()) return {error:"verification"};
      if (options.guard) {
        const permit = await requestPermit(id);
        if (!permit.ok) return {error:permit.error || "access-budget-exhausted", requested:attempts>0, attempts};
      }
      const controller = new AbortController();
      const timeout = win.setTimeout(() => controller.abort(), 15000);
      attempts++; requestsInBatch++;
      const started=Date.now(), diagnostic={id,attempt:attempts,stage:"fetch",content_type:"unknown"};
      const failure=(error,extra={})=> {diagnostic.error=error;return {error,requested:true,attempts,...extra};};
      try {
        const headers = new Headers(requestHeaders);
        headers.set("content-type", "application/json");
        // 使用当前站点的 fetch（保留站点随后安装的验证/请求包装），不绕开页面保护。
        const response = await win.fetch(ENDPOINT, {method:"POST", credentials:"include", headers,
          body:JSON.stringify({productUniqueNo:code}), signal:controller.signal});
        diagnostic.status=response.status;
        const contentType=response.headers.get("content-type") || "";
        diagnostic.content_type=/json/i.test(contentType) ? "json" : /html/i.test(contentType) ? "html" : "other";
        if (response.status === 429) {
          const raw = response.headers.get("retry-after"), seconds = raw && Number(raw);
          const until = raw && (Number.isFinite(seconds) ? Date.now() + Math.max(0,seconds)*1000 : Date.parse(raw));
          if (Number.isFinite(until)) nextRequest=Math.max(nextRequest,until);
          diagnostic.retry_after_until=Number.isFinite(until) ? until : null;
          return failure("rate-limit",{status:429,retry_after_until:diagnostic.retry_after_until});
        }
        if ([401,403].includes(response.status)) return failure(options.guard ? response.status===401 ? "auth-required" : "access-denied" : "verification",{status:response.status});
        if (!response.ok) {
          diagnostic.error="http-error";
          if (response.status >= 500 && attempt === 0) { nextRequest = Date.now() + Math.max(6000, interval * 2); continue; }
          return failure("http-error",{status:response.status});
        }
        if (!/json/i.test(contentType)) return failure(options.guard ? "unexpected-content" : "verification");
        diagnostic.stage="body";
        let data;
        try {data = await response.json();} catch (_) {return failure("invalid-json");}
        diagnostic.stage="validate";
        if (!current()) return failure("route-change");
        if (data?.success !== true) return failure(options.guard ? "business-error" : "verification");
        if (typeof data.data?.showTitle !== "string" || !data.data.showTitle.trim()) return failure("empty-title");
        remember(id, code, data.data.showTitle,"title-api");
        return {title:data.data.showTitle, source:"title-api", observed_at:Date.now(), requested:true, attempts};
      } catch (_) {
        diagnostic.error=options.guard ? controller.signal.aborted ? "timeout" : "fetch-rejected" : "network-error";
        if (attempt === 0) { nextRequest = Date.now() + Math.max(6000, interval * 2); continue; }
        return failure(diagnostic.error);
      } finally {
        diagnostic.duration_ms=Math.max(0,Date.now()-started);
        options.diagnostics.push(diagnostic);
        win.clearTimeout(timeout);
        // 从请求完成计时，避免慢响应之后下一卡立即请求。
        nextRequest = Math.max(nextRequest, Date.now() + interval);
      }
    }
    return {error:"network-error", requested:true};
  }
  win.addEventListener("pxb7-title-request", async event => {
    let data;
    try { data = JSON.parse(event.detail); } catch (_) { return; }
    if (typeof data.token !== "string" || data.token.length > 100 || !/^\d{6,24}$/.test(data.id)) return;
    const reply = result => win.dispatchEvent(new win.CustomEvent("pxb7-title-result", {detail:JSON.stringify({token:data.token, id:data.id, ...result})}));
    if (active) { reply({error:"busy"}); return; }
    active = true;
    try { reply(await lookup(data.id, {intervalMs:data.intervalMs, refresh:data.refresh === true, cacheOnly:data.cacheOnly === true, guard:data.guard === true})); }
    catch (_) { reply({error:"network-error"}); }
    finally { active = false; }
  });
})(window);
