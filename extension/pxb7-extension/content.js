/**
 * pxb7 采集助手（前端 B）—— 内容脚本（独立扩展版，不依赖油猴）
 *
 * 在你自己浏览的 pxb7 列表/详情页上**就地读取已渲染的 DOM**，经背景服务转发到本机网关入库。
 * 背景服务仅访问回环网关；列表完整标题由 MAIN 脚本复用站点响应，缺失时限速补请求。
 * 不触发悬浮卡片；可选择列表全文或后台标签页串行详情采集，遇到验证/限流停止。
 * 「每次采集张数」调大时在页内点站点自己的「加载更多」或滚动（触发站点自身的懒加载，
 * 等同用户手动滚动），不猜 URL、不请求站外接口。
 */
"use strict";

(() => {
  const EXT_VERSION = chrome.runtime.getManifest().version;
  const SPA_SETTLE_DEFAULT_MS = 2500;
  const CONFIG_POLL_MS = 5 * 60 * 1000;

  // 远端可配置项（看板/弹窗设置区写入网关 /config，脚本定期拉取应用）
  let CONFIG = { auto_ingest: true, reingest_interval_min: 10, spa_settle_ms: 2500,
                 debug: false, cards_target: 16, title_interval_ms:3000,
                 detail_interval_ms:0, collection_mode:"list" };

  // 采集目标（网关下发）：列表页按 URL 的 game_id 预检；详情页 URL 不含游戏 → 交网关裁决
  let TARGETS = null;      // {mode, tasks:[...], games:[...]}；null=未获取（放行，最终由网关裁决）

  const isDetail = () => location.pathname.startsWith("/product/");
  const isList = () => location.pathname.startsWith("/buy/");
  if (!isDetail() && !isList()) return;

  function log(...args) {
    if (CONFIG.debug) console.log("[pxb7采集]", ...args);
  }

  function currentGameId() {
    const m = location.pathname.match(/^\/buy\/(\d+)\//);
    return m ? Number(m[1]) : null;
  }

  /** 列表页目标预检：该游戏未勾选为采集目标时，连本地网关都不必发。 */
  function targetBlocked() {
    if (!TARGETS || !isList()) return false;
    const gameId = currentGameId();
    if (gameId === null) return false;
    return !(TARGETS.games || []).map(Number).includes(gameId);
  }

  /** 经背景服务访问本机网关（SW 的 fetch 有 host_permissions，不受页面 CORS 限制）。 */
  function bg(message) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(message, (reply) => {
          if (chrome.runtime.lastError) {
            resolve({ ok: false, err: chrome.runtime.lastError.message });
            return;
          }
          resolve(reply || { ok: false, err: "后台无应答" });
        });
      } catch (e) {
        resolve({ ok: false, err: "扩展后台不可用：" + String(e) });
      }
    });
  }

  async function fetchConfig() {
    const res = await bg({ type: "config", version: EXT_VERSION });
    if (res && res.ok && res.data && res.data.ok) {
      const before = JSON.stringify(CONFIG);
      CONFIG = { ...CONFIG, ...(res.data.config || {}) };
      if (res.data.targets) TARGETS = res.data.targets;
      if (before !== JSON.stringify(CONFIG)) log("配置已更新：", CONFIG);
      log("采集目标：", TARGETS);
    }
  }

  // ---------- 去重（同一 URL 一个会话内只自动采一次） ----------
  function dedupeKey() {
    return "pxb7ext:" + location.pathname + location.search;
  }
  function shouldSkipAuto() {
    const last = Number(sessionStorage.getItem(dedupeKey()) || 0);
    return Date.now() - last < CONFIG.reingest_interval_min * 60 * 1000;
  }
  function markIngested() {
    try { sessionStorage.setItem(dedupeKey(), String(Date.now())); } catch (e) { /* 隐私模式忽略 */ }
  }

  // ---------- 页内角标 ----------
  let badge = null;
  function show(text, ok) {
    if (!badge) {
      badge = document.createElement("div");
      badge.style.cssText = [
        "position:fixed", "right:12px", "bottom:12px", "z-index:2147483647",
        "padding:6px 10px", "border-radius:8px", "font:12px/1.5 system-ui,sans-serif",
        "color:#fff", "box-shadow:0 2px 8px rgba(0,0,0,.25)", "opacity:.92",
      ].join(";");
      document.body.appendChild(badge);
    }
    badge.style.background = ok ? "rgba(22,163,74,.9)" : "rgba(220,38,38,.9)";
    badge.textContent = "pxb7采集 " + text;
    clearTimeout(show._t);
    show._t = setTimeout(() => { if (badge) badge.style.opacity = "0.35"; }, 6000);
  }

  // ---------- 目标张数：站点一页只渲染 16 张，需要更多就在页内把卡片加载出来 ----------
  const SWEEP = (typeof window !== "undefined" && window.PXB7_SWEEP) || null;
  const TITLES = (typeof window !== "undefined" && window.PXB7_TITLES) || null;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  let taskId = null, cancelled = false, workerPage = false;
  function progress(phase, extra = {}) {
    if (!taskId) return;
    bg({type:"collection-progress", taskId, phase, ...extra});
  }

  function targetCards() {
    return SWEEP ? SWEEP.clampTarget(CONFIG.cards_target) : 16;
  }

  function stepInterval() {
    return SWEEP ? SWEEP.DEFAULTS.intervalMs : 2000;
  }

  /** 滚动/点「加载更多」把卡片加载出来，直到达到目标张数或连续两轮没有新增 */
  async function expandCards(target, pageUrl) {
    const stats = { target, initial: 0, final: 0, rounds: 0, stalled: 0, stop: "no-sweep" };
    if (!SWEEP) return stats;
    let cards = SWEEP.countCards(document);
    stats.initial = cards;
    stats.final = cards;
    while (true) {
      if (cancelled || location.href !== pageUrl) { stats.stop = "done-route-change"; break; }
      const action = SWEEP.nextAction({
        cards, target, rounds: stats.rounds, maxRounds: SWEEP.DEFAULTS.maxRounds,
        stalled: stats.stalled, maxStalled: SWEEP.DEFAULTS.maxStalled,
        hasMoreControl: !!SWEEP.findMoreControl(document),
      });
      if (action.indexOf("done") === 0) { stats.stop = action; break; }
      show(`加载更多中 ${cards}/${target} 张…`, true);
      progress(`加载卡片 ${Math.min(cards, target)}/${target}`);
      const control = action === "click" ? SWEEP.findMoreControl(document) : null;
      if (control) {
        try { control.click(); } catch (e) { SWEEP.scrollForMore(window, document); }
      } else {
        SWEEP.scrollForMore(window, document);
      }
      await sleep(stepInterval());
      const next = SWEEP.countCards(document);
      stats.rounds += 1;
      if (next <= cards) stats.stalled += 1; else stats.stalled = 0;
      cards = next;
      stats.final = cards;
    }
    return stats;
  }

  /** 把当前页 DOM 发给网关（extra 并入 payload，如 sweep 统计）；返回 {ok,data,error} */
  async function sendPage(path, extra, options = {}) {
    let titleStats = null;
    if (path === "/ingest/cards" && TITLES && !options.skipTitles) {
      const pageUrl = location.href;
      show("读取账号完整标题（无需悬浮）…", true);
      titleStats = await TITLES.collect(document, window, {alive:() => !cancelled && location.href === pageUrl,
        ids:options.selected?.map(item => item.id),
        intervalMs:CONFIG.title_interval_ms,
        progress:(captured, cards, retry, processed) => {
          const phase = `${retry ? "补采缺失全文" : "读取账号全文"} ${captured}/${cards}`;
          show(phase + "…", true);
          progress(phase, {processed:processed || captured});
        }});
      if (location.href !== pageUrl) return {ok:false, error:"route-change"};
      if (titleStats.stop === "list-change") {
        show("列表已变化，请重新采集本页", false);
        return {ok:false, error:"list-change"};
      }
    }
    if (cancelled) return {ok:false, error:"已取消采集"};
    show(isDetail() ? "发送详情页…" : "发送本页卡片…", true);
    let html = document.documentElement.outerHTML;
    if (options.selected && SWEEP) {
      html = SWEEP.capture(document, options.selected, titleStats?.titles || {});
    } else if (titleStats) {
      // 只修改待上传副本：公开全文跟随明确的商品 ID 落 raw，不改站点页面或弹出层。
      const clone = document.documentElement.cloneNode(true);
      for (const card of clone.querySelectorAll(".middleCard[productid]")) {
        const id = card.getAttribute("productid"), title = titleStats.titles[id];
        if (title) {
          card.setAttribute("data-pxb7-full-title", title);
          card.setAttribute("data-pxb7-full-title-id", id);
        }
      }
      html = clone.outerHTML;
    }
    const payload = {
      url: location.href,
      html,
      page_no: 1,
      listing_id: isDetail() ? (location.pathname.match(/\/product\/(\d+)/) || [])[1] : undefined,
    };
    if (extra) Object.assign(payload, extra);
    if (titleStats) {
      const {titles, ...stats} = titleStats;
      payload.title_collection = stats;
    }
    const res = await bg({ type: "ingest", path, payload });
    if (!res.ok || !res.data || !res.data.ok) {
      const d = res.data || {};
      const reason = d.message
        || { "risk-page": "拦截页已拒收（不入库）",
             "target-not-selected": "该游戏不在采集目标（扩展弹窗可勾选）",
             "game-unresolved": "无法识别该页所属游戏（先浏览列表页再进详情）" }[d.error]
        || d.error || res.err || "未知错误";
      show("失败：" + reason, false);
      log("ingest 失败：", res);
      return { ok: false, error: reason };
    }
    if (titleStats) {
      const {titles, ...stats} = titleStats;
      titleStats = stats; // 调用方/弹窗只需完整率，公开文案已随 raw 保存。
    }
    return { ok: true, data: res.data, titleStats };
  }

  function titleStatus(stats) {
    if (!stats || !stats.cards) return "";
    const missing = stats.cards - stats.captured;
    const reason = {verification:"需完成网站验证", "rate-limit":"网站限流", "session-not-ready":"请刷新列表后重采",
      "bridge-timeout":"请应用扩展更新并刷新列表", unavailable:"连续网络失败已暂停，可稍后重采"}[stats.stop]
      || (stats.errors && stats.errors[0] && {"title-mismatch":"标题校验未通过", "incomplete-title":"接口仍返回短标题",
        "empty-title":"接口返回空标题", "network-error":"网络请求失败"}[stats.errors[0].error]);
    return `，全文 ${stats.captured}/${stats.cards}` + (missing ? `，${missing} 张待补齐${reason ? "（" + reason + "）" : ""}` : "");
  }

  let collecting = false;
  async function ingest(force) {
    if (collecting) return {ok:false, error:"本页已有采集任务进行中"};
    collecting = true; cancelled = false;
    try {
      if (force) await fetchConfig(); // 保存设置后立即点采集，使用新方式/张数
      return await ingestCurrent(force);
    } catch (e) {
      progress("采集失败：" + String(e.message || e), {status:"error"});
      return {ok:false, error:String(e.message || e)};
    } finally { collecting = false; taskId = null; }
  }

  const normalize = text => String(text || "").replace(/[^\p{L}\p{N}]/gu, "");
  async function waitDetailTitle(pageUrl, prefix = "") {
    let previous = "";
    for (let attempt = 0; attempt < 200; attempt++) {
      if (cancelled || location.href !== pageUrl) return null;
      if (/验证码|安全验证|访问受限/.test(document.title || "")) return {error:"verification"};
      let node = document.querySelector(".product-detail [data-product-title], .product-detail .product-title, .product-detail .line-clamp-5, .product-detail h1");
      // 用户已点「更多」时 line-clamp 类会消失；仍按列表短标题找到当前商品主标题。
      if (!node && prefix && document.querySelectorAll) {
        const key = normalize(prefix);
        node = Array.from(document.querySelectorAll(".product-detail div, .product-detail p"))
          .filter(el => normalize(el.textContent).includes(key))
          .sort((a,b) => a.textContent.length - b.textContent.length)[0];
      }
      const text = node ? (node.textContent || "").trim() : "";
      const full = normalize(text), key = normalize(prefix), index = key ? full.indexOf(key) : 0;
      const matches = !key || (index >= 0 && index < 32);
      const truncated = prefix.length >= 159 && full.length <= key.length + Math.max(0, index)
        && !/详情看图|官方截图/.test(text);
      if (text.length >= 6 && text === previous && matches && !truncated) return {node, text};
      previous = text;
      await sleep(100); // 只观察 DOM 就绪；没有逐商品固定等待
    }
    return null;
  }

  async function runWorker(job) {
    cancelled = false;
    const pageUrl = location.href;
    const title = await waitDetailTitle(pageUrl, job.prefix);
    let payload, error = title?.error;
    if (!title) error = "detail-not-ready";
    if (pageUrl !== job.url) error = "detail-id-mismatch";
    if (!error) {
      const clone = document.documentElement.cloneNode(true);
      // 标记上传副本中的主标题，兼容展开后的 DOM；不修改站点页面。
      const candidates = Array.from(clone.querySelectorAll(".product-detail div, .product-detail p, .product-detail h1"));
      const node = candidates.find(el => (el.textContent || "").trim() === title.text);
      if (!node) error = "detail-title-missing";
      else {
        node.setAttribute("data-product-title", "true");
        payload = {url:pageUrl, html:clone.outerHTML, listing_id:job.id};
      }
    }
    show(error ? "详情未采集：" + error : "详情已就绪，正在入库…", !error);
    await bg({type:"detail-result", taskId:job.taskId, nonce:job.nonce, payload, error});
  }

  async function ingestCurrent(force) {
    if (!isDetail() && !isList()) return {ok:true, skipped:"route"};
    const pageUrl = location.href, detailPage = isDetail();
    if (!force && !CONFIG.auto_ingest) {
      show("自动采集已关闭（扩展弹窗可开启）", true);
      return {ok:true, skipped:"auto-off"};
    }
    if (targetBlocked()) {
      show("该游戏不在采集目标（扩展弹窗可勾选）", true);
      return {ok:true, skipped:"target"};
    }
    if (!force && shouldSkipAuto()) {
      show(`本页 ${CONFIG.reingest_interval_min} 分钟内已采集`, true);
      return {ok:true, skipped:"dedupe"};
    }
    const target = detailPage ? 1 : targetCards();
    const mode = detailPage ? "detail" : CONFIG.collection_mode;
    const started = await bg({type:"collection-begin", mode, total:target});
    if (!started.ok) { show(started.err || "已有采集任务", false); return started; }
    taskId = started.taskId;
    function fail(error) {
      progress(error, {status:cancelled ? "cancelled" : "error"});
      show(error, false);
      return {ok:false, error};
    }
    if (detailPage) {
      const ready = await waitDetailTitle(pageUrl);
      if (!ready || ready.error) return fail(ready?.error || "商品详情尚未就绪，请加载完成后重采");
      const result = await sendPage("/ingest/detail", null);
      if (!result.ok) return fail(result.error);
      const weapons = result.data.attributes?.five_star_weapons;
      const incomplete = /五星角色|五星武器|光锥|音擎/.test(ready.text)
        && (weapons == null || (weapons > 0 && !result.data.weapon_details_complete));
      if (!incomplete) markIngested();
      const phase = incomplete ? "详情已入库，武器信息待补齐" : "详情已入库";
      progress(phase, {status:incomplete ? "partial" : "done", total:1, processed:1, succeeded:1, incomplete:incomplete ? 1 : 0});
      show(phase, !incomplete);
      return {ok:true, result:result.data};
    }
    // 先加载，再选前 N 张，一次提交；不会反复整页请求全文或超出设置张数。
    const stats = await expandCards(target, pageUrl);
    if (cancelled || location.href !== pageUrl) return fail("采集取消或列表已变化");
    const selected = SWEEP?.selectCards ? SWEEP.selectCards(document, target, pageUrl) : null;
    if (selected && !selected.length) return fail("未找到有效商品卡片，请等待列表加载后重采");
    const total = selected ? selected.length : Math.min(target, SWEEP ? SWEEP.countCards(document) : target);
    progress(mode === "detail" ? "保存价格和商品清单" : "开始读取列表全文", {total});
    const first = await sendPage("/ingest/cards", {sweep: stats}, {selected, skipTitles:mode === "detail"});
    if (!first.ok) return fail(first.error);
    if (cancelled || location.href !== pageUrl) return fail("采集取消或列表已变化");
    if (mode === "detail") {
      const items = (selected || []).map(item => {
        const title = item.card.querySelector(".smallCardTitle, .bigCardTitle");
        return {id:item.id, url:item.url, prefix:title ? title.getAttribute("productname") || title.textContent || "" : ""};
      });
      const result = await bg({type:"detail-start", taskId, items, round:first.data.round,
                               detail_interval_ms:CONFIG.detail_interval_ms});
      if (!result.ok) return fail(result.err || "详情队列启动失败");
      show(`详情队列已启动，共 ${total} 张；进度见扩展弹窗`, true);
      // 详情全部成功才在源列表标记去重；由下方状态轮询确认。
      return {ok:true, queued:true, taskId};
    }
    const d = first.data;
    const missing = first.titleStats ? first.titleStats.cards - first.titleStats.captured : 0;
    const complete = !missing && total >= target && d.cards_parsed === total;
    if (complete) markIngested();
    const phase = `已入库 ${d.cards_parsed}/${target} 张` + titleStatus(first.titleStats)
      + (total < target ? `；仅加载出 ${total} 张` : "");
    progress(phase, {status:complete ? "done" : "partial", total, processed:total,
      succeeded:Math.max(0, d.cards_parsed - missing), failed:Math.max(missing, total - d.cards_parsed)});
    show(phase, complete);
    return {ok:true, result:d, sweep:stats, title_collection:first.titleStats};
  }

  // ---------- SPA 路由变化（Nuxt 客户端导航不整页刷新） ----------
  const requestedDetail = () => isDetail() && location.hash === "#pxb7-collect-detail";
  let lastPath = location.pathname + location.search + location.hash;
  let routeTimer = null;
  function onRouteChange() {
    const now = location.pathname + location.search + location.hash;
    if (now === lastPath || workerPage) return;
    lastPath = now;
    if (!(location.pathname.startsWith("/buy/") || location.pathname.startsWith("/product/"))) return;
    clearTimeout(routeTimer);
    routeTimer = setTimeout(() => { ingest(requestedDetail()); }, CONFIG.spa_settle_ms);
  }
  // 隔离世界无法覆盖站点主世界的 history；URL 观察覆盖 push/replaceState。
  setInterval(onRouteChange, 500);
  window.addEventListener("popstate", onRouteChange);
  window.addEventListener("hashchange", onRouteChange);

  // ---------- 弹窗「采集本页」入口 ----------
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.type === "detail-completed") {
      if (location.href === msg.url) {markIngested(); show("详情队列全部入库完成", true);}
      sendResponse({ok:true}); return false;
    }
    if (msg && msg.type === "collect-cancel") {
      cancelled = true; sendResponse({ok:true}); return false;
    }
    if (msg && msg.type === "collect-now") {
      if (workerPage) {sendResponse({ok:false, error:"当前是详情采集工作标签页"}); return false;}
      ingest(true).then((result) => sendResponse(result || { ok: true }));
      return true;                      // 异步应答
    }
    return undefined;
  });

  // ---------- 入口 ----------
  bg({type:"detail-context"}).then(async context => {
    workerPage = !!context.worker;
    if (workerPage) {
      if (context.job) {
        try { await runWorker(context.job); }
        catch (_) { await bg({type:"detail-result", taskId:context.job.taskId, nonce:context.job.nonce, error:"detail-read-error"}); }
      }
      return;
    }
    await fetchConfig();
    // 来自看板的明确补齐操作，采集一次，不改变用户的自动采集开关。
    setTimeout(() => { ingest(requestedDetail()); }, CONFIG.spa_settle_ms || SPA_SETTLE_DEFAULT_MS);
  });
  setInterval(fetchConfig, CONFIG_POLL_MS);
})();
