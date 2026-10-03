/**
 * pxb7 采集助手（前端 B）—— 内容脚本（独立扩展版，不依赖油猴）
 *
 * 在你自己浏览的 pxb7 列表/详情页上**就地读取已渲染的 DOM**，经背景服务转发到本机网关入库。
 * 唯一网络目标是本机回环网关（经背景服务代理）；本脚本不主动请求任何 pxb7 地址——
 * 「每次采集张数」调大时只在页内点站点自己的「加载更多」或滚动（触发站点自身的懒加载，
 * 等同用户手动滚动），不猜 URL、不请求站外接口。
 */
"use strict";

(() => {
  const EXT_VERSION = chrome.runtime.getManifest().version;
  const SPA_SETTLE_DEFAULT_MS = 2500;
  const CONFIG_POLL_MS = 5 * 60 * 1000;

  // 远端可配置项（看板/弹窗设置区写入网关 /config，脚本定期拉取应用）
  let CONFIG = { auto_ingest: true, reingest_interval_min: 10, spa_settle_ms: 2500,
                 debug: false, cards_target: 16 };

  // 采集目标（网关下发）：列表页按 URL 的 game_id 预检；详情页 URL 不含游戏 → 交网关裁决
  let TARGETS = null;      // {mode, tasks:[...], games:[...]}；null=未获取（放行，最终由网关裁决）

  const isDetail = location.pathname.startsWith("/product/");
  const isList = location.pathname.startsWith("/buy/");
  if (!isDetail && !isList) return;

  function log(...args) {
    if (CONFIG.debug) console.log("[pxb7采集]", ...args);
  }

  function currentGameId() {
    const m = location.pathname.match(/^\/buy\/(\d+)\//);
    return m ? Number(m[1]) : null;
  }

  /** 列表页目标预检：该游戏未勾选为采集目标时，连本地网关都不必发。 */
  function targetBlocked() {
    if (!TARGETS || !isList) return false;
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
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function targetCards() {
    return SWEEP ? SWEEP.clampTarget(CONFIG.cards_target) : 16;
  }

  function stepInterval() {
    return SWEEP ? SWEEP.DEFAULTS.intervalMs : 2000;
  }

  /** 滚动/点「加载更多」把卡片加载出来，直到达到目标张数或连续两轮没有新增 */
  async function expandCards(target) {
    const stats = { target, initial: 0, final: 0, rounds: 0, stalled: 0, stop: "no-sweep" };
    if (!SWEEP) return stats;
    let cards = SWEEP.countCards(document);
    stats.initial = cards;
    stats.final = cards;
    while (true) {
      const action = SWEEP.nextAction({
        cards, target, rounds: stats.rounds, maxRounds: SWEEP.DEFAULTS.maxRounds,
        stalled: stats.stalled, maxStalled: SWEEP.DEFAULTS.maxStalled,
        hasMoreControl: !!SWEEP.findMoreControl(document),
      });
      if (action.indexOf("done") === 0) { stats.stop = action; break; }
      show(`加载更多中 ${cards}/${target} 张…`, true);
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
  async function sendPage(path, extra) {
    show(isDetail ? "发送详情页…" : "发送本页卡片…", true);
    const payload = {
      url: location.href,
      html: document.documentElement.outerHTML,     // 就地读取已渲染 DOM
      page_no: 1,
      listing_id: isDetail ? (location.pathname.match(/\/product\/(\d+)/) || [])[1] : undefined,
    };
    if (extra) Object.assign(payload, extra);
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
    return { ok: true, data: res.data };
  }

  async function ingest(force) {
    if (!force && !CONFIG.auto_ingest) {
      show("自动采集已关闭（扩展弹窗可开启）", true);
      return { ok: true, skipped: "auto-off" };
    }
    if (targetBlocked()) {
      // 目标采集：只采勾选的游戏；采集本页（force）同样受目标约束
      show("该游戏不在采集目标（扩展弹窗可勾选）", true);
      return { ok: true, skipped: "target" };
    }
    if (!force && shouldSkipAuto()) {
      show(`本页 ${CONFIG.reingest_interval_min} 分钟内已采集`, true);
      return { ok: true, skipped: "dedupe" };
    }
    const path = isDetail ? "/ingest/detail" : "/ingest/cards";
    const first = await sendPage(path, null);
    if (!first.ok) return { ok: false, error: first.error };
    markIngested();
    if (isDetail) {
      const d = first.data;
      show(`详情已入库：正在浏览=${d.viewers_masked === null ? "未公示" : d.viewers_masked}`
        + ` 收藏=${d.favorites_cnt === null ? "未公示" : d.favorites_cnt}`, true);
      return { ok: true, result: d };
    }
    const d = first.data;
    show(`已入库 ${d.cards_parsed}/${d.cards_seen} 张`
      + `（解析率 ${(d.parse_success_rate * 100).toFixed(0)}%，新增 ${d.new_listings}）`, true);

    // 目标张数（弹窗可调）：站点一页只渲染 16 张，需要更多就在页内加载出更多卡片，
    // 再把加载后的整页 DOM 发一次——网关按「轮次+listing」幂等合并，不会产生重复行。
    const target = targetCards();
    const before = SWEEP ? SWEEP.countCards(document) : d.cards_seen;
    if (SWEEP && target > before) {
      const stats = await expandCards(target);
      log("加载更多：", stats);
      if (stats.final > stats.initial) {
        const again = await sendPage(path, { sweep: stats });
        if (again.ok) {
          const n = again.data;
          show(`已入库 ${n.cards_parsed}/${n.cards_seen} 张`
            + `（页内加载 ${stats.rounds} 轮，目标 ${target} 张）`, true);
          return { ok: true, result: n, sweep: stats };
        }
        show("加载更多后的入库失败：" + again.error, false);
      } else {
        show(`本列表加载不出更多（仍为 ${stats.initial} 张）`, true);
      }
    }
    return { ok: true, result: d };
  }

  // ---------- SPA 路由变化（Nuxt 客户端导航不整页刷新） ----------
  let lastPath = location.pathname + location.search;
  function onRouteChange() {
    const now = location.pathname + location.search;
    if (now === lastPath) return;
    lastPath = now;
    if (!(location.pathname.startsWith("/buy/") || location.pathname.startsWith("/product/"))) return;
    setTimeout(() => { ingest(false); }, CONFIG.spa_settle_ms);
  }
  const _push = history.pushState;
  history.pushState = function () {
    const r = _push.apply(this, arguments);
    onRouteChange();
    return r;
  };
  window.addEventListener("popstate", onRouteChange);

  // ---------- 弹窗「采集本页」入口 ----------
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.type === "collect-now") {
      ingest(true).then((result) => sendResponse(result || { ok: true }));
      return true;                      // 异步应答
    }
    return undefined;
  });

  // ---------- 入口 ----------
  fetchConfig().then(() => {
    setTimeout(() => { ingest(false); }, CONFIG.spa_settle_ms || SPA_SETTLE_DEFAULT_MS);
  });
  setInterval(fetchConfig, CONFIG_POLL_MS);
})();
