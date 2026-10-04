/** 隔离世界：按商品 ID 获取公开全文，不触发悬浮或详情导航。 */
(function (root) {
  "use strict";
  const normalize = text => String(text || "").replace(/[^\p{L}\p{N}]/gu, "");
  function references(doc) {
    return Array.from(doc.querySelectorAll(".middleCard[productid]")).map(card => {
      const title = card.querySelector(".smallCardTitle, .bigCardTitle");
      return {card, title, id:card.getAttribute("productid"),
        text:title ? (title.getAttribute("productname") || title.textContent || "").trim() : ""};
    }).filter(item => item.title && /^\d{6,24}$/.test(item.id));
  }
  function request(win, id, options = {}) {
    return new Promise(resolve => {
      const token = String(Date.now()) + ":" + String(Math.random());
      const finish = result => {
        win.clearTimeout(timer);
        win.removeEventListener("pxb7-title-result", receive);
        resolve(result);
      };
      const receive = event => {
        try {
          const data = JSON.parse(event.detail);
          if (data.token === token && data.id === id) finish(data);
        } catch (_) { /* 非协议事件忽略 */ }
      };
      const timer = win.setTimeout(() => finish({error:"bridge-timeout"}), 120000);
      win.addEventListener("pxb7-title-result", receive);
      win.dispatchEvent(new win.CustomEvent("pxb7-title-request", {
        detail:JSON.stringify({token, id, intervalMs:options.intervalMs, refresh:options.refresh === true})
      }));
    });
  }
  async function collect(doc, win, options = {}) {
    const refs = references(doc).filter(item => !options.ids || options.ids.includes(item.id));
    const stats = {cards:0, captured:0, requested:0, attempts:0, retried:0, failed:0, stop:"done", titles:{}, errors:[]};
    stats.interval_ms = Math.max(2000, Math.min(15000, Number(options.intervalMs) || 3000));
    const alive = options.alive || (() => true);
    const getTitle = options.request || ((id, refresh) => request(win, id, {intervalMs:options.intervalMs, refresh}));
    const pause = options.pause || (ms => new Promise(resolve => win.setTimeout(resolve, ms)));
    const pending = [], errors = new Map();
    let processed = 0;
    let failures = 0;
    for (const item of refs) if (/五星角色|五星武器|光锥|音擎/.test(item.text)) stats.cards++;
    async function read(item, refresh = false) {
      const current = () => item.card.isConnected !== false && item.card.getAttribute("productid") === item.id;
      if (!alive()) { stats.stop = "route-change"; return false; }
      if (!current()) { stats.stop = "list-change"; return false; }
      if (options.progress) options.progress(stats.captured, stats.cards, refresh, processed);
      const result = await getTitle(item.id, refresh);
      if (!refresh) processed++;
      if (!alive()) { stats.stop = "route-change"; return false; }
      if (!current()) { stats.stop = "list-change"; return false; }
      if (result.requested) stats.requested++;
      stats.attempts += Number(result.attempts) || (result.requested ? 1 : 0);
      // ID 是主关联，短标题前缀用于拒绝刷新/错号响应，不用悬浮内容猜归属。
      const full = typeof result.title === "string" ? result.title.trim() : "";
      const prefix = normalize(item.text);
      const appearsTruncated = normalize(full).length <= prefix.length && item.text.length >= 159
        && !/详情看图|官方截图/.test(full);
      if (full && prefix.length >= 24 && normalize(full).startsWith(prefix) && !appearsTruncated) {
        stats.titles[item.id] = full; stats.captured++; failures = 0;
        errors.delete(item.id);
      } else {
        const error = result.error || (appearsTruncated ? "incomplete-title" : "title-mismatch");
        errors.set(item.id, {id:item.id, error, attempts:Number(result.attempts) || 0,
          ...(result.status ? {status:result.status} : {})});
        if (!refresh) pending.push(item);
        // 网络失败才触发全局熔断，文案缺失/不匹配不能让后续所有商品永远没机会补采。
        failures = ["network-error", "http-error"].includes(error) ? failures + 1 : 0;
        if (["verification", "rate-limit", "session-not-ready", "bridge-timeout", "route-change", "list-change"].includes(result.error)) {
          stats.stop = result.error; return false;
        }
        if (failures >= 3) { stats.stop = "unavailable"; return false; }
      }
      if (options.progress) options.progress(stats.captured, stats.cards, refresh, processed);
      return true;
    }
    for (const item of refs) {
      if (!/五星角色|五星武器|光锥|音擎/.test(item.text)) continue;
      if (!(await read(item))) break;
    }
    // 正常请求仍可进行时，整页结束后仅补采失败商品一轮，绕过其不完整缓存。
    if (pending.length && stats.stop === "done") {
      await pause(Math.max(6000, Number(options.intervalMs) * 2 || 6000));
      for (const item of pending) {
        stats.retried++;
        if (!(await read(item, true))) break;
      }
    }
    stats.failed = errors.size;
    stats.errors = [...errors.values()];
    return stats;
  }
  const api = {normalize, references, request, collect};
  root.PXB7_TITLES = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
