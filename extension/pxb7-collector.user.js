// ==UserScript==
// @name         pxb7 采集助手（前端 B · 本地采集通道 + 看板面板）
// @namespace    pxb7-price-monitor
// @version      0.6.0
// @description  在你自己浏览的 pxb7 页面上就地收集已渲染的卡片/详情 DOM，发送到本机采集网关（127.0.0.1:8765）入库；可按游戏勾选采集目标、调节每次采集张数（页内加载更多），内置实时看板面板。不主动请求任何 pxb7 地址（页内加载只触发站点自身懒加载）。
// @author       pxb7-price-monitor
// @match        https://www.pxb7.com/buy/*
// @match        https://www.pxb7.com/product/*
// @connect      127.0.0.1
// @connect      localhost
// @updateURL    http://127.0.0.1:8765/userscript.js
// @downloadURL  http://127.0.0.1:8765/userscript.js
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @run-at       document-idle
// ==/UserScript==

(function () {
  "use strict";

  const SCRIPT_VERSION = "0.6.0";          // 与 @version 保持一致（上报网关用于更新检测）
  const GATEWAY = "http://127.0.0.1:8765";
  const CONFIG_POLL_MS = 5 * 60 * 1000;     // 远端配置/版本轮询间隔
  const SPA_SETTLE_DEFAULT_MS = 2500;       // SPA 路由切换后的渲染等待（可被远端配置覆盖）

  // 远端可配置项（看板/面板设置区写入 /config，脚本定期拉取应用）
  let CONFIG = { auto_ingest: true, reingest_interval_min: 10, spa_settle_ms: 2500,
                 debug: false, cards_target: 16 };
  let TARGETS = null;      // 采集目标（网关下发）：{mode, tasks, games}；null=未获取（放行）
  let UPDATE_URL = GATEWAY + "/userscript.js";

  const isDetail = location.pathname.startsWith("/product/");
  const isList = location.pathname.startsWith("/buy/");
  if (!isDetail && !isList) return;

  function log(...args) {
    if (CONFIG.debug) console.log("[pxb7采集]", ...args);
  }

  /** 列表页目标预检：该游戏未勾选为采集目标时，连本地网关都不必发。 */
  function targetBlocked() {
    if (!TARGETS || !isList) return false;
    const m = location.pathname.match(/^\/buy\/(\d+)\//);
    if (!m) return false;
    return !(TARGETS.games || []).map(Number).includes(Number(m[1]));
  }

  // ---------- 网关请求（不发任何 pxb7 请求） ----------
  function callGateway(path, options) {
    options = options || {};
    return new Promise((resolve) => {
      if (typeof GM_xmlhttpRequest === "function") {
        GM_xmlhttpRequest({
          method: options.method || "GET",
          url: GATEWAY + path,
          headers: options.body ? { "Content-Type": "application/json" } : {},
          data: options.body,
          timeout: 30000,
          onload: (r) => finish(r),
          onerror: () => resolve({ ok: false, err: "网关不可达（请先打开采集看板/启动服务）" }),
          ontimeout: () => resolve({ ok: false, err: "网关超时" }),
        });
      } else {
        fetch(GATEWAY + path, {
          method: options.method || "GET",
          headers: options.body ? { "Content-Type": "application/json" } : {},
          body: options.body,
        }).then(finish).catch(() => resolve({ ok: false, err: "网关不可达（请先打开采集看板/启动服务）" }));
      }
      function finish(r) {
        // GM_xmlhttpRequest 响应带 responseText；fetch 回退分支是 Response 对象（用 text()）
        if (typeof r.responseText === "string") {
          try { resolve({ ok: true, status: r.status, data: JSON.parse(r.responseText) }); }
          catch (e) { resolve({ ok: false, err: "网关响应异常: " + r.status }); }
          return;
        }
        r.text().then((text) => {
          try { resolve({ ok: true, status: r.status, data: JSON.parse(text) }); }
          catch (e) { resolve({ ok: false, err: "网关响应异常: " + r.status }); }
        }).catch(() => resolve({ ok: false, err: "网关响应读取失败" }));
      }
    });
  }

  async function fetchConfig() {
    const res = await callGateway(
      "/config?channel=userscript&version=" + encodeURIComponent(SCRIPT_VERSION), {});
    if (res.ok && res.data && res.data.ok) {
      const before = JSON.stringify(CONFIG);
      CONFIG = { ...CONFIG, ...res.data.config };
      if (res.data.targets) TARGETS = res.data.targets;
      if (before !== JSON.stringify(CONFIG)) log("配置已更新：", CONFIG);
      const s = res.data.script || {};
      if (s.update_available) {
        UPDATE_URL = GATEWAY + (s.update_url || "/userscript.js");
        show(`插件有新版 v${s.latest_version}（当前 v${s.installed_version}，点击更新）`, true,
             () => window.open(UPDATE_URL, "_blank"));
        log("有新版本：", s);
      }
      if (panel && panel.style.display !== "none") refreshPanel();
    }
  }

  // ---------- 去重（同一 URL 一个会话内只自动采一次；菜单可强制重采） ----------
  function dedupeKey() {
    return "pxb7plug:" + location.pathname + location.search;
  }
  function shouldSkipAuto() {
    const last = Number(sessionStorage.getItem(dedupeKey()) || 0);
    return Date.now() - last < CONFIG.reingest_interval_min * 60 * 1000;
  }
  function markIngested() {
    try { sessionStorage.setItem(dedupeKey(), String(Date.now())); } catch (e) { /* 隐私模式忽略 */ }
  }

  // ---------- 角标（可点击：用于「点击更新插件」） ----------
  let badge = null;
  function show(text, ok, onClick) {
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
    badge.style.cursor = onClick ? "pointer" : "default";
    badge.onclick = onClick || null;
    badge.textContent = "pxb7采集 " + text;
    clearTimeout(show._t);
    show._t = setTimeout(() => { if (badge) badge.style.opacity = "0.35"; }, onClick ? 20000 : 6000);
  }

  // ---------- 目标张数：站点一页只渲染 16 张，需要更多就在页内把卡片加载出来 ----------
  // （与扩展版 sweep.js 同一套判定；本脚本独立发布，故内联一份）
  const SWEEP = Object.freeze({
    maxCards: 200, maxRounds: 12, maxStalled: 2, intervalMs: 2000,
  });

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function clampTarget(value) {
    const n = Number(value);
    if (!Number.isFinite(n) || n <= 0) return 16;
    return Math.min(SWEEP.maxCards, Math.max(16, Math.round(n)));
  }

  /** 当前 DOM 卡片数（与 parser 的 CARD_SELECTORS 同源） */
  function countCards() {
    const byAttr = document.querySelectorAll("[productid]").length;
    if (byAttr > 0) return byAttr;
    const byClass = document.querySelectorAll(".middleCard").length;
    if (byClass > 0) return byClass;
    return document.querySelectorAll("a[href*='/product/']").length;
  }

  /** 站点若渲染了「加载更多/下一页」控件就点它；找不到就滚动触发懒加载 */
  function findMoreControl() {
    for (const selector of [".t-pagination__btn-next", "[class*='load-more']",
                            "[class*='loadMore']", "[class*='next-page']"]) {
      const el = document.querySelector(selector);
      if (el && !el.disabled && el.getAttribute("aria-disabled") !== "true"
          && !/disabled/.test(String(el.className || ""))) return el;
    }
    for (const el of document.querySelectorAll("button, a, [role='button'], .t-button")) {
      const text = String(el.textContent || "").trim();
      if (["下一页", "加载更多", "查看更多", "更多商品"].indexOf(text) >= 0
          && !el.disabled) return el;
    }
    return null;
  }

  function scrollForMore() {
    window.scrollTo(0, (document.documentElement || document.body || {}).scrollHeight || 0);
    let node = document.querySelector(".product-list, [class*='product-list'], main");
    while (node && node !== document.body) {
      const style = window.getComputedStyle(node);
      if ((style.overflowY === "auto" || style.overflowY === "scroll")
          && node.scrollHeight > node.clientHeight) {
        node.scrollTop = node.scrollHeight;
        break;
      }
      node = node.parentElement;
    }
  }

  async function expandCards(target) {
    const stats = { target, initial: 0, final: 0, rounds: 0, stalled: 0, stop: "done-target" };
    let cards = countCards();
    stats.initial = cards;
    stats.final = cards;
    while (cards < target) {
      if (stats.rounds >= SWEEP.maxRounds) { stats.stop = "done-rounds"; break; }
      if (stats.stalled >= SWEEP.maxStalled) { stats.stop = "done-stalled"; break; }
      show(`加载更多中 ${cards}/${target} 张…`, true);
      const control = findMoreControl();
      if (control) {
        try { control.click(); } catch (e) { scrollForMore(); }
      } else {
        scrollForMore();
      }
      await sleep(SWEEP.intervalMs);
      const next = countCards();
      stats.rounds += 1;
      if (next <= cards) stats.stalled += 1; else stats.stalled = 0;
      cards = next;
      stats.final = cards;
    }
    return stats;
  }

  async function sendPage(path, extra) {
    show(isDetail ? "发送详情页…" : "发送本页卡片…", true);
    const payload = {
      url: location.href,
      html: document.documentElement.outerHTML,   // 就地收集已渲染 DOM
      page_no: 1,
      listing_id: isDetail ? (location.pathname.match(/\/product\/(\d+)/) || [])[1] : undefined,
    };
    if (extra) Object.assign(payload, extra);
    const res = await callGateway(path, { method: "POST", body: JSON.stringify(payload) });
    if (!res.ok || !res.data || !res.data.ok) {
      const d = res.data || {};
      const reason = d.message
        || { "risk-page": "拦截页已拒收（不入库）",
             "target-not-selected": "该游戏不在采集目标（看板「采集目标」可勾选）",
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
      show("自动采集已关闭（面板可开启）", true);
      return;
    }
    if (targetBlocked()) {
      // 目标采集：只采勾选的游戏；手动「采集本页」同样受目标约束
      show("该游戏不在采集目标（看板「采集目标」可勾选）", true);
      return;
    }
    if (!force && shouldSkipAuto()) {
      show(`本页 ${CONFIG.reingest_interval_min} 分钟内已采集（菜单可强制重采）`, true);
      return;
    }
    const path = isDetail ? "/ingest/detail" : "/ingest/cards";
    const first = await sendPage(path, null);
    if (!first.ok) return;
    markIngested();
    if (isDetail) {
      const d = first.data;
      show(`详情已入库：正在浏览=${d.viewers_masked === null ? "未公示" : d.viewers_masked}`
        + ` 收藏=${d.favorites_cnt === null ? "未公示" : d.favorites_cnt}`, true);
    } else {
      const d = first.data;
      show(`已入库 ${d.cards_parsed}/${d.cards_seen} 张`
        + `（解析率 ${(d.parse_success_rate * 100).toFixed(0)}%，新增 ${d.new_listings}）`, true);
      // 目标张数（面板可调）：一页 16 张，需要更多就在页内加载出更多卡片再发一次
      const target = clampTarget(CONFIG.cards_target);
      if (target > countCards()) {
        const stats = await expandCards(target);
        log("加载更多：", stats);
        if (stats.final > stats.initial) {
          const again = await sendPage(path, { sweep: stats });
          if (again.ok) {
            const n = again.data;
            show(`已入库 ${n.cards_parsed}/${n.cards_seen} 张`
              + `（页内加载 ${stats.rounds} 轮，目标 ${target} 张）`, true);
          } else {
            show("加载更多后的入库失败：" + again.error, false);
          }
        } else {
          show(`本列表加载不出更多（仍为 ${stats.initial} 张）`, true);
        }
      }
    }
    if (panel && panel.style.display !== "none") refreshPanel();
  }

  // ---------- 看板面板（浏览器插件形态：与采集同页联动） ----------
  let panel = null;
  let panelTimer = null;

  function esc(s) {
    return String(s === null || s === undefined ? "—" : s)
      .replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
  const fmt = (n) => (n === null || n === undefined) ? "—" : Number(n).toLocaleString("zh-CN");

  function ensureToggle() {
    if (document.getElementById("pxb7-panel-toggle")) return;
    const btn = document.createElement("button");
    btn.id = "pxb7-panel-toggle";
    btn.title = "pxb7 采集看板（点击开关）";
    btn.textContent = "📊";
    btn.style.cssText = [
      "position:fixed", "left:12px", "bottom:12px", "z-index:2147483646",
      "width:38px", "height:38px", "border-radius:50%", "border:1px solid #3a4152",
      "background:#171a21", "color:#e6e9f0", "font-size:18px", "cursor:pointer",
      "box-shadow:0 2px 8px rgba(0,0,0,.35)", "opacity:.9",
    ].join(";");
    btn.addEventListener("click", togglePanel);
    document.body.appendChild(btn);
  }

  function buildPanel() {
    panel = document.createElement("div");
    panel.id = "pxb7-panel";
    panel.style.cssText = [
      "position:fixed", "left:12px", "bottom:58px", "z-index:2147483646",
      "width:400px", "max-height:78vh", "overflow:auto", "display:none",
      "background:#12151c", "color:#e6e9f0", "border:1px solid #2a2f3c",
      "border-radius:12px", "box-shadow:0 8px 28px rgba(0,0,0,.5)",
      "font:12px/1.55 system-ui,'Microsoft YaHei',sans-serif", "padding:12px 14px",
    ].join(";");
    panel.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <b style="font-size:13px">🦀 pxb7 采集看板</b>
        <span id="pxb7-p-state" style="color:#8b93a7">连接中…</span>
        <span style="flex:1"></span>
        <a href="#" id="pxb7-p-full" style="color:#60a5fa">完整看板</a>
        <a href="#" id="pxb7-p-close" style="color:#8b93a7;margin-left:8px">✕</a>
      </div>
      <div id="pxb7-p-tiles" style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px"></div>
      <div style="color:#7c9eff;margin:10px 0 4px">采集轮次（最近 8 轮）</div>
      <div id="pxb7-p-rounds"></div>
      <div style="color:#7c9eff;margin:10px 0 4px">最近批次</div>
      <div id="pxb7-p-batches"></div>
      <div style="color:#7c9eff;margin:10px 0 4px">采集目标（只采勾选的游戏）</div>
      <div id="pxb7-p-targets" style="display:flex;flex-direction:column;gap:2px"></div>
      <div style="color:#7c9eff;margin:10px 0 4px">设置（脚本自动应用）</div>
      <label style="display:block;margin:4px 0"><input type="checkbox" id="pxb7-p-auto"> 自动采集</label>
      <label style="display:block;margin:4px 0">去重间隔(分钟)
        <input type="number" id="pxb7-p-interval" min="1" max="1440"
               style="width:70px;background:#1d212b;border:1px solid #2a2f3c;color:#e6e9f0;border-radius:6px;padding:2px 6px">
      </label>
      <label style="display:block;margin:4px 0">每次采集张数
        <input type="number" id="pxb7-p-cards" min="16" max="200" step="16"
               style="width:70px;background:#1d212b;border:1px solid #2a2f3c;color:#e6e9f0;border-radius:6px;padding:2px 6px">
      </label>
      <label style="display:block;margin:4px 0">SPA 等待(ms)
        <input type="number" id="pxb7-p-settle" min="500" max="30000" step="100"
               style="width:70px;background:#1d212b;border:1px solid #2a2f3c;color:#e6e9f0;border-radius:6px;padding:2px 6px">
      </label>
      <label style="display:block;margin:4px 0"><input type="checkbox" id="pxb7-p-debug"> 调试日志</label>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">
        <button id="pxb7-p-save" style="background:#7c9eff;border:none;color:#0d1220;border-radius:6px;padding:4px 10px;cursor:pointer">保存设置</button>
        <button id="pxb7-p-save-targets" style="background:#7c9eff;border:none;color:#0d1220;border-radius:6px;padding:4px 10px;cursor:pointer">保存目标</button>
        <button id="pxb7-p-grab" style="background:#34d399;border:none;color:#0d1220;border-radius:6px;padding:4px 10px;cursor:pointer">采集本页</button>
        <button id="pxb7-p-upd" style="background:#fbbf24;border:none;color:#0d1220;border-radius:6px;padding:4px 10px;cursor:pointer;display:none">更新插件</button>
        <button id="pxb7-p-stop" style="background:transparent;border:1px solid #2a2f3c;color:#e6e9f0;border-radius:6px;padding:4px 10px;cursor:pointer">停止服务</button>
      </div>
      <div id="pxb7-p-note" style="color:#34d399;margin-top:6px"></div>
    `;
    document.body.appendChild(panel);
    panel.querySelector("#pxb7-p-close").addEventListener("click", (e) => { e.preventDefault(); togglePanel(false); });
    panel.querySelector("#pxb7-p-full").addEventListener("click", (e) => { e.preventDefault(); window.open(GATEWAY + "/", "_blank"); });
    panel.querySelector("#pxb7-p-save").addEventListener("click", savePanelConfig);
    panel.querySelector("#pxb7-p-save-targets").addEventListener("click", savePanelTargets);
    panel.querySelector("#pxb7-p-grab").addEventListener("click", () => ingest(true));
    panel.querySelector("#pxb7-p-upd").addEventListener("click", () => window.open(UPDATE_URL, "_blank"));
    panel.querySelector("#pxb7-p-stop").addEventListener("click", async () => {
      if (!window.confirm("停止本机采集服务？浏览页面的自动采集将暂停，直到再次打开看板/服务。")) return;
      await callGateway("/shutdown", { method: "POST", body: "{}" });
      panelNote("服务已停止；再次使用：双击桌面「pxb7采集看板」");
    });
  }

  function panelNote(text, color) {
    const el = panel && panel.querySelector("#pxb7-p-note");
    if (!el) return;
    el.textContent = text;
    el.style.color = color || "#34d399";
    setTimeout(() => { el.textContent = ""; }, 5000);
  }

  function togglePanel(force) {
    if (!panel) buildPanel();
    const open = force !== undefined ? force : panel.style.display === "none";
    panel.style.display = open ? "block" : "none";
    if (open) {
      refreshPanel();
      if (!panelTimer) panelTimer = setInterval(() => {
        if (panel && panel.style.display !== "none") refreshPanel();
      }, 5000);
    }
  }

  function bars(rows, labelFn, valueFn) {
    const top = Math.max(1, ...rows.map(valueFn));
    return rows.map((r) => {
      const v = valueFn(r);
      return `<div style="display:flex;align-items:center;gap:6px;margin:2px 0">` +
        `<span style="width:96px;color:#8b93a7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(labelFn(r))}</span>` +
        `<span style="flex:1;background:#1d212b;border-radius:5px;height:12px;display:inline-block">` +
        `<span style="display:block;height:100%;width:${(v / top) * 100}%;border-radius:5px;background:linear-gradient(90deg,#2f6feb,#7c9eff)"></span></span>` +
        `<span style="width:40px;text-align:right">${fmt(v)}</span></div>`;
    }).join("") || '<div style="color:#8b93a7">暂无数据</div>';
  }

  async function refreshPanel() {
    const res = await callGateway("/stats", {});
    const state = panel.querySelector("#pxb7-p-state");
    if (!res.ok || !res.data || !res.data.ok) {
      state.textContent = "网关未连接（双击桌面看板可启动服务）";
      state.style.color = "#f87171";
      return;
    }
    const s = res.data;
    state.textContent = `已连接 · ${s.stats.last_batch_at ? "最近 " + s.stats.last_batch_at : "暂无批次"}`;
    state.style.color = "#34d399";
    const st = s.stats;
    const rate = st.cards_seen ? Math.round(st.cards_parsed / st.cards_seen * 100) + "%" : "—";
    const tile = (k, v) => `<div style="background:#1d212b;border-radius:8px;padding:6px 8px">` +
      `<div style="color:#8b93a7">${k}</div><b style="font-size:14px">${v}</b></div>`;
    panel.querySelector("#pxb7-p-tiles").innerHTML =
      tile("快照", fmt(s.db.snapshot_rows)) + tile("解析率", rate) + tile("新增", fmt(st.new_listings)) +
      tile("词表命中", fmt(s.db.keyword_hits)) + tile("详情", fmt(st.details_stored)) + tile("拦截拒收", fmt(st.risk_pages_rejected));
    panel.querySelector("#pxb7-p-rounds").innerHTML =
      bars((s.db.rounds || []).slice(-8), (r) => r.round, (r) => r.rows);
    panel.querySelector("#pxb7-p-batches").innerHTML = (s.recent_batches || []).slice(-6).reverse()
      .map((b) => `<div style="color:#8b93a7">${esc(b.at.slice(11))} ${b.kind === "cards" ? "列表" : "详情"} · ` +
        `${b.kind === "cards" ? `${b.cards_parsed}/${b.cards_seen} 张 → 入库 ${b.snapshots}` : `收藏 ${b.favorites_cnt === null ? "未公示" : b.favorites_cnt}`}</div>`)
      .join("") || '<div style="color:#8b93a7">暂无批次</div>';
    const c = s.config || {};
    panel.querySelector("#pxb7-p-auto").checked = !!c.auto_ingest;
    panel.querySelector("#pxb7-p-interval").value = c.reingest_interval_min;
    panel.querySelector("#pxb7-p-cards").value = c.cards_target || 16;
    panel.querySelector("#pxb7-p-settle").value = c.spa_settle_ms;
    panel.querySelector("#pxb7-p-debug").checked = !!c.debug;
    // 采集目标：数据未变时不重绘（避免打断勾选）
    const tg = s.targets || {};
    const sig = JSON.stringify(tg);
    if (sig !== targetsSig) {
      targetsSig = sig;
      panel.querySelector("#pxb7-p-targets").innerHTML = (tg.available || []).map((x) =>
        `<label style="display:flex;gap:6px;align-items:center">` +
        `<input type="checkbox" data-task="${esc(x.task_id)}"${x.selected ? " checked" : ""}` +
        `${x.enabled ? "" : " disabled"}> ${esc(x.name)}` +
        `<span style="color:#8b93a7;margin-left:auto">${fmt(x.snapshots)} 行</span></label>`
      ).join("") + `<div style="color:#8b93a7">${tg.mode === "selected"
        ? `已选 ${(tg.effective || []).length} 个：${esc((tg.effective || []).join("、"))}`
        : `未勾选 → 使用网关启动任务 ${esc(tg.default_task || "—")}`}</div>`;
    }
    const sc = s.script || {};
    const upd = panel.querySelector("#pxb7-p-upd");
    if (sc.update_available) { upd.style.display = "inline-block"; upd.textContent = `更新到 v${sc.latest_version}`; }
    else { upd.style.display = "none"; }
  }

  let targetsSig = "";

  async function savePanelTargets() {
    const ids = Array.from(panel.querySelectorAll("#pxb7-p-targets input[data-task]:checked"))
      .map((el) => el.dataset.task);
    const res = await callGateway("/config", { method: "POST", body: JSON.stringify({ targets: ids }) });
    if (res.ok && res.data && res.data.ok) {
      TARGETS = res.data.targets || TARGETS;
      targetsSig = "";
      panelNote(ids.length ? `采集目标已保存（${ids.length} 个）✓` : "已清空目标 → 使用网关启动任务 ✓");
      refreshPanel();
    } else {
      panelNote("目标保存失败：" + ((res.data && res.data.error) || res.err || "未知错误"), "#f87171");
    }
  }

  async function savePanelConfig() {
    const payload = {
      auto_ingest: panel.querySelector("#pxb7-p-auto").checked,
      reingest_interval_min: Number(panel.querySelector("#pxb7-p-interval").value),
      cards_target: Number(panel.querySelector("#pxb7-p-cards").value),
      spa_settle_ms: Number(panel.querySelector("#pxb7-p-settle").value),
      debug: panel.querySelector("#pxb7-p-debug").checked,
    };
    const res = await callGateway("/config", { method: "POST", body: JSON.stringify(payload) });
    if (res.ok && res.data && res.data.ok) {
      CONFIG = { ...CONFIG, ...res.data.config };
      panelNote("设置已保存并立即生效 ✓");
    } else {
      panelNote("保存失败：" + ((res.data && res.data.error) || res.err || "未知错误"), "#f87171");
    }
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

  // ---------- 入口 ----------
  GM_registerMenuCommand("pxb7采集：打开看板面板", () => togglePanel(true));
  GM_registerMenuCommand("pxb7采集：打开完整看板", () => window.open(GATEWAY + "/", "_blank"));
  GM_registerMenuCommand("pxb7采集：手动采集本页", () => ingest(true));
  GM_registerMenuCommand("pxb7采集：立即刷新配置/检查更新",
    async () => { await fetchConfig(); show("配置已刷新（如有新版会提示）", true); });
  GM_registerMenuCommand("pxb7采集：更新插件到最新版", () => window.open(UPDATE_URL, "_blank"));

  fetchConfig();
  setInterval(fetchConfig, CONFIG_POLL_MS);
  // 等卡片渲染完成再采（Nuxt 客户端渲染）
  setTimeout(() => { ensureToggle(); ingest(false); }, CONFIG.spa_settle_ms || SPA_SETTLE_DEFAULT_MS);
})();
