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
                 detail_interval_ms:0, collection_mode:"list", verification_mode:"manual" };

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
  const VERIFY = (typeof window !== "undefined" && window.PXB7_VERIFICATION) || null;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  let taskId = null, cancelled = false, workerPage = false;
  let activeBatch = null, frozenConfig = null;
  const batchIdentity = () => activeBatch ? {protocol_version:1, batch_id:activeBatch.batch_id, claim_epoch:activeBatch.claim_epoch} : {};
  let manualVerificationConfirmed = false;
  async function waitVerification(pageUrl, job = null, alreadyPaused = false, requireObserved = false) {
    if (!VERIFY) return true;
    const identity = () => ({taskId:job?.taskId || taskId, nonce:job?.nonce, pageUrl});
    if (alreadyPaused) show("等待网站验证恢复；原任务进度已保留", false);
    const result = await VERIFY.waitForClear(document, window, {
      alreadyPaused, requireObserved,
      alive:() => !cancelled && location.href === pageUrl,
      confirmed:() => manualVerificationConfirmed,
      onBlocked:async () => {
        manualVerificationConfirmed = false;
        show("等待网站滑块验证；完成后自动继续，进度已保留", false);
        const reply = await bg({type:"verification-pause", ...identity()});
        if (job && reply.nonce) job.nonce = reply.nonce;
        return reply.ok;
      },
      onClear:async () => {
        const reply = await bg({type:"verification-resume", ...identity()});
        if (!reply.ok) return false;
        if (job) job.nonce = reply.nonce;
        manualVerificationConfirmed = false;
        show("验证已恢复，继续原采集任务", true);
        return true;
      },
      onAttempt:(job?.config || frozenConfig || CONFIG).verification_mode === "auto_slider" ? async () => {
        show("正在尝试普通滑块验证；原任务进度已保留", false);
        const reply = await bg({type:"verification-attempt", ...identity()});
        if (!cancelled && location.href === pageUrl) show(reply.inputSent
          ? "滑块输入已执行，等待网站确认；未通过时可人工完成"
          : "自动验证未完成（" + (reply.error || "不可用") + "）；可人工完成后续采", false);
      } : undefined,
    });
    return result.ok;
  }
  async function waitRejectedVerification(pageUrl, job = null) {
    const reply = await bg({type:"verification-pause", taskId:job?.taskId || taskId, nonce:job?.nonce, pageUrl});
    if (!reply.ok) return false;
    if (job && reply.nonce) job.nonce = reply.nonce;
    manualVerificationConfirmed = false;
    show("验证页已拒收；请完成验证，或在弹窗确认后继续", false);
    // 网关拒收但页面没有可见挑战时，不靠定时器重复提交；等待挑战出现/消失或用户明确确认。
    return waitVerification(pageUrl, job, true, true);
  }
  function progress(phase, extra = {}) {
    if (!taskId) return;
    bg({type:"collection-progress", taskId, phase, ...extra});
  }

  function targetCards() {
    if (CONFIG.target_count > 0) return CONFIG.target_count;
    return SWEEP ? SWEEP.clampTarget(CONFIG.cards_target) : 16;
  }

  function stepInterval() {
    return SWEEP ? SWEEP.DEFAULTS.intervalMs : 2000;
  }

  /** 滚动/点「加载更多」把卡片加载出来，直到达到目标张数或连续两轮没有新增 */
  async function expandCards(target, pageUrl) {
    const stats = { target, initial: 0, final: 0, rounds: 0, stalled: 0, stop: "no-sweep" };
    if (!SWEEP?.selectCards) throw new Error("采集组件不可用，请应用扩展更新后刷新页面");
    const candidates = new Map();
    let decisions = new Map(), added = 0;
    async function scan() {
      const cfg = frozenConfig || CONFIG;
      const refs = SWEEP.selectCards(document, Infinity, pageUrl);
      added = 0;
      const newRefs = [];
      for (const item of refs) {
        if (activeBatch && candidates.size >= cfg.scan_budget && !candidates.has(item.id)) break;
        if (!candidates.has(item.id)) {added++; newRefs.push(item);}
        candidates.set(item.id, item);
      }
      const refsToCheck = activeBatch ? newRefs : Array.from(candidates.values());
      const responses = [];
      for (let offset=0; offset<refsToCheck.length || !offset && !refsToCheck.length && !activeBatch; offset+=200) {
        const part = activeBatch ? refsToCheck.slice(offset,offset+200) : refsToCheck;
        const res = await bg({type:"ingest", path:"/collection/check", payload:{url:pageUrl,
          ...batchIdentity(), html:SWEEP.capture(document, part, {}, {compact:!!activeBatch}), ...(activeBatch ? {selected_ids:part.map(i=>i.id)} : {})}});
        responses.push(res);
        if (!activeBatch || !res.ok) break;
      }
      const res = responses.find(r=>!r.ok || !r.data?.ok) || {ok:true,data:{ok:true,items:responses.flatMap(r=>r.data?.items || [])}};
      if (res.data?.error === "risk-page") {
        if (!await waitRejectedVerification(pageUrl)) throw new Error("采集取消或页面已变化");
        return scan();
      }
      if (!res.ok || !res.data?.ok || !Array.isArray(res.data.items)) {
        throw new Error(res.data?.error || res.err || "账号去重查询失败，采集已停止");
      }
      if (!activeBatch) decisions.clear();
      for (const item of res.data.items) decisions.set(item.id,item);
      for (const item of candidates.values()) {
        if (!decisions.has(item.id)) throw new Error("商品解析不完整，无法确认去重状态，请刷新列表");
      }
      stats.final = candidates.size;
      const wanted = cfg.ids?.length ? new Set(cfg.ids) : null;
      return Array.from(decisions.values()).filter(item => (!wanted || wanted.has(item.id)) && (cfg.purpose === "price_refresh"
        ? item.existing === true : cfg.purpose === "detail_enrich" ? item.existing === true && item.missing?.length : item.kind === "new")).length;
    }
    let cards = await scan();
    stats.initial = candidates.size;
    while (true) {
      if (cancelled || location.href !== pageUrl) { stats.stop = "done-route-change"; break; }
      if (!await waitVerification(pageUrl)) { stats.stop = "done-route-change"; break; }
      if (activeBatch && candidates.size >= frozenConfig.scan_budget) {stats.stop="done-budget";break;}
      const action = SWEEP.nextAction({
        cards, target, rounds: stats.rounds, maxRounds:activeBatch ? frozenConfig.max_scan_rounds : SWEEP.DEFAULTS.maxRounds,
        stalled: stats.stalled, maxStalled: SWEEP.DEFAULTS.maxStalled,
        hasMoreControl: !!SWEEP.findMoreControl(document),
      });
      if (action.indexOf("done") === 0) { stats.stop = action; break; }
      show(`加载新账号 ${cards}/${target}；已检查 ${candidates.size} 个…`, true);
      progress(`加载账号 ${Math.min(cards, target)}/${target}；已检查 ${candidates.size} 个`, {scanned:candidates.size});
      if (activeBatch) {
        const access = await bg({type:"collection-access", taskId, pageUrl, action:"list"});
        if (!access.data?.ok) {stats.stop="done-access-budget";break;}
      }
      const control = action === "click" ? SWEEP.findMoreControl(document) : null;
      if (control) {
        try { control.click(); } catch (e) { SWEEP.scrollForMore(window, document); }
      } else {
        SWEEP.scrollForMore(window, document);
      }
      await sleep(stepInterval());
      if (!await waitVerification(pageUrl)) { stats.stop = "done-route-change"; break; }
      const next = await scan();
      stats.rounds += 1;
      if (!added) stats.stalled += 1; else stats.stalled = 0;
      cards = next;
    }
    if (activeBatch && frozenConfig.purpose !== "discover") {
      const wanted = frozenConfig.ids?.length ? new Set(frozenConfig.ids) : null;
      const selected = Array.from(candidates.values()).filter(item=> {
        const d = decisions.get(item.id);
        return (!wanted || wanted.has(item.id)) && d?.existing && (frozenConfig.purpose === "price_refresh" || d.missing?.length);
      }).slice(0,target);
      return {stats, selected, decisions};
    }
    const fresh = Array.from(candidates.values()).filter(item => decisions.get(item.id)?.kind === "new").slice(0,target);
    const other = Array.from(candidates.values()).filter(item => ["changed", "retry"].includes(decisions.get(item.id)?.kind));
    return {stats, selected:[...other, ...fresh], decisions};
  }

  /** 把当前页 DOM 发给网关（extra 并入 payload，如 sweep 统计）；返回 {ok,data,error} */
  async function sendPage(path, extra, options = {}) {
    const currentUrl = location.href;
    if (!await waitVerification(currentUrl)) return {ok:false, error:"采集取消或页面已变化"};
    let titleStats = null;
    if (path === "/ingest/cards" && TITLES && !options.skipTitles) {
      const pageUrl = location.href;
      show("读取账号完整标题（无需悬浮）…", true);
      titleStats = await TITLES.collect(document, window, {alive:() => !cancelled && location.href === pageUrl,
        waitUntilReady:() => waitVerification(pageUrl),
        ids:options.titleIds || options.selected?.map(item => item.id),
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
      if (titleStats.stop === "verification") return {ok:false, error:"网站要求验证", errorCode:"risk-page"};
    }
    if (cancelled) return {ok:false, error:"已取消采集"};
    if (!await waitVerification(currentUrl)) return {ok:false, error:"采集取消或页面已变化"};
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
      ...(options.selected ? {selected_ids:options.selected.map(item => item.id)} : {}),
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
      return { ok: false, error: reason, errorCode:d.error };
    }
    if (titleStats) {
      const {titles, ...stats} = titleStats;
      titleStats = stats; // 调用方/弹窗只需完整率，公开文案已随 raw 保存。
    }
    return { ok: true, data: res.data, titleStats };
  }
  async function sendVerifiedPage(path, extra, options = {}) {
    const pageUrl = location.href;
    while (!cancelled && location.href === pageUrl) {
      const result = await sendPage(path, extra, options);
      if (result.errorCode !== "risk-page") return result;
      if (!await waitRejectedVerification(pageUrl)) break;
    }
    return {ok:false, error:"采集取消或页面已变化"};
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

  let collecting = false, collectionRun = null;
  async function ingest(force, resumeTask = null) {
    // 后台取消已落盘时，页面可能仍在结束当前读取。等待旧操作退出再启动，
    // 不提前清锁/重置 cancelled，避免旧操作恢复后污染新任务。
    if (collecting && cancelled) await collectionRun;
    if (collecting) return {ok:false, error:"本页已有采集任务进行中"};
    collecting = true; cancelled = false;
    collectionRun = (async () => {
      try {
        if (force) await fetchConfig(); // 保存设置后立即点采集，使用新方式/张数
        if (cancelled) return {ok:false, error:"已取消采集"};
        return await ingestCurrent(force, resumeTask);
      } catch (e) {
        progress("采集失败：" + String(e.message || e), {status:cancelled ? "cancelled" : "error"});
        return {ok:false, error:String(e.message || e)};
      } finally { collecting = false; taskId = null; activeBatch = null; frozenConfig = null; }
    })();
    return collectionRun;
  }

  const normalize = text => String(text || "").replace(/[^\p{L}\p{N}]/gu, "");
  // 下架页特征（2026-10-06 用户指令）：商品无法补录时跳过并标记失败，不耗满超时、
  // 不计入连续失败。只在标题迟迟未就绪时观察，且要求标记连续稳定，避免误判加载中的正常页。
  const DELIST_TEXT = /商品已?下架|商品不存在|商品已删除|商品已失效|已下架或已售出|页面不存在或已删除/;
  function detectDelisted() {
    if (/下架|已售出|不存在|已删除|已失效/.test(document.title || "")) return true;
    const zone = document.querySelector(".product-detail") || document.body;
    return DELIST_TEXT.test((zone?.textContent || "").slice(0, 6000));
  }
  async function waitDetailTitle(pageUrl, prefix = "", job = null) {
    let previous = "", delistStable = 0;
    for (let attempt = 0; attempt < 200; attempt++) {
      if (cancelled || location.href !== pageUrl) return null;
      if (!await waitVerification(pageUrl, job)) return null;
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
      if (text.length >= 6 && text === previous && matches && !truncated) {
        if (job?.batch) {
          // 观察商品文字模块；不等待图片。迟到模块会改变签名并重新计稳定窗口。
          const zone=document.querySelector(".product-detail");
          const signature=(zone?.textContent || "").replace(/\s+/g," ");
          if (waitDetailTitle._signature !== signature) {
            waitDetailTitle._signature=signature;waitDetailTitle._stableAt=Date.now();
          }
          const busy=zone?.querySelector("[aria-busy='true'], .loading, .t-loading, [data-loading='true']");
          if (busy || Date.now()-waitDetailTitle._stableAt < 800) {await sleep(100);continue;}
        }
        return {node, text};
      }
      // 标题约 3 秒（30 次×100ms）仍未就绪才开始观察下架标记，连续 2 次稳定才判定。
      if (attempt >= 30) {
        delistStable = detectDelisted() ? delistStable + 1 : 0;
        if (delistStable >= 2) return {delisted: true};
      } else delistStable = 0;
      previous = text;
      await sleep(100); // 只观察 DOM 就绪；没有逐商品固定等待
    }
    return null;
  }

  async function runWorker(job) {
    cancelled = false;
    taskId = job.taskId;
    activeBatch = job.batch || null;
    frozenConfig = job.config || null;
    const pageUrl = location.href;
    if (!await waitVerification(pageUrl, job, job.waiting_verification)) return;
    while (!cancelled && location.href === pageUrl) {
      const title = await waitDetailTitle(pageUrl, job.prefix, job);
      let payload, error = title?.error;
      if (title?.delisted) error = "delisted";
      else if (!title) error = "detail-not-ready";
      if (pageUrl !== job.url) error = "detail-id-mismatch";
      if (!error) {
        if (!await waitVerification(pageUrl, job)) return;
        const clone = activeBatch && SWEEP?.publicCopy ? SWEEP.publicCopy(document.documentElement) : document.documentElement.cloneNode(true);
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
      const reply = await bg({type:"detail-result", taskId:job.taskId, nonce:job.nonce, payload, error});
      if (!reply.paused) return;
      if (!await waitVerification(pageUrl, job, true, true)) return;
    }
  }

  async function ingestCurrent(force, resumeTask = null) {
    if (!isDetail() && !isList()) return {ok:true, skipped:"route"};
    const pageUrl = location.href, detailPage = isDetail();
    if (!resumeTask && !force && !CONFIG.auto_ingest) {
      show("自动采集已关闭（扩展弹窗可开启）", true);
      return {ok:true, skipped:"auto-off"};
    }
    if (targetBlocked()) {
      show("该游戏不在采集目标（扩展弹窗可勾选）", true);
      return {ok:true, skipped:"target"};
    }
    if (!resumeTask && !force && shouldSkipAuto()) {
      show(`本页 ${CONFIG.reingest_interval_min} 分钟内已采集`, true);
      return {ok:true, skipped:"dedupe"};
    }
    const target = resumeTask?.requested || (detailPage ? 1 : targetCards());
    const mode = resumeTask?.mode || (detailPage ? "detail" : CONFIG.collection_mode);
    const protocol = !detailPage && (mode === "auto" || CONFIG.target_count > 0 || CONFIG.purpose && CONFIG.purpose !== "discover");
    const started = resumeTask ? {ok:true, taskId:resumeTask.id, batch:resumeTask.batch, config:resumeTask.config}
      : await bg({type:"collection-begin", mode, total:target, pageUrl,
        ...(protocol ? {protocol_version:1, purpose:CONFIG.purpose || "discover"} : {})});
    if (!started.ok) { show(started.err || "已有采集任务", false); return started; }
    taskId = started.taskId;
    activeBatch = started.batch || null;
    frozenConfig = started.config || null;
    function fail(error) {
      progress(error, {status:cancelled ? "cancelled" : "error"});
      show(error, false);
      return {ok:false, error};
    }
    if (!await waitVerification(pageUrl, null, !!resumeTask)) return fail("采集取消或页面已变化");
    if (activeBatch && !detailPage) return ingestBatch(pageUrl, fail);
    if (detailPage) {
      const ready = await waitDetailTitle(pageUrl);
      if (!ready || ready.error) return fail(ready?.error || "商品详情尚未就绪，请加载完成后重采");
      const result = await sendVerifiedPage("/ingest/detail", null);
      if (!result.ok) return fail(result.error);
      const weapons = result.data.attributes?.five_star_weapons;
      const incomplete = /五星角色|五星武器|光锥|音擎/.test(ready.text)
        && (result.data.weapon_information
          ? !result.data.weapon_details_complete
          : (weapons == null || (weapons > 0 && !result.data.weapon_details_complete)));
      if (!incomplete) markIngested();
      const weaponLabel = result.data.weapon_information?.state === "unreported" ? "，页面未提供武器信息"
        : result.data.weapon_information?.state === "none" ? "，已确认无五星武器" : "";
      const phase = incomplete ? "详情已入库，武器信息未采到或解析失败" : "详情已入库" + weaponLabel;
      progress(phase, {status:incomplete ? "partial" : "done", total:1, processed:1, succeeded:1, incomplete:incomplete ? 1 : 0});
      show(phase, !incomplete);
      return {ok:true, result:result.data};
    }
    // 先加载，再选前 N 张，一次提交；不会反复整页请求全文或超出设置张数。
    let expanded;
    try { expanded = await expandCards(target, pageUrl); }
    catch (error) { return fail(error.message || "去重查询失败"); }
    const {stats, selected, decisions} = expanded;
    if (cancelled || location.href !== pageUrl) return fail("采集取消或列表已变化");
    const skipped = Array.from(decisions.values()).filter(item => item.kind === "skip").length;
    const manual = Array.from(decisions.values()).filter(item => item.kind === "manual").length;
    const freshCount = selected.filter(item => decisions.get(item.id)?.kind === "new").length;
    if (!selected.length) {
      const empty = await sendVerifiedPage("/ingest/cards", {sweep:stats,dedupe_counts:{skip:skipped,manual}}, {selected:[],skipTitles:true});
      if (!empty.ok) return fail(empty.error);
      const phase = `新增 0/${target}；重复跳过 ${skipped}，待查缺补录 ${manual}；${stats.stop}`;
      progress(phase, {status:"partial", total:0, processed:0, duplicates:skipped, manual_pending:manual, new_succeeded:0});
      show(phase, true);
      return {ok:true, skipped:"no-new-accounts", sweep:stats};
    }
    const total = selected.length;
    progress(mode === "detail" ? "保存价格和商品清单" : "开始读取列表全文", {total});
    const first = await sendVerifiedPage("/ingest/cards", {sweep: stats,dedupe_counts:{skip:skipped,manual}}, {selected, skipTitles:mode === "detail",
      titleIds:selected.filter(item => decisions.get(item.id)?.kind !== "retry").map(item => item.id)});
    if (!first.ok) return fail(first.error);
    if (cancelled || location.href !== pageUrl) return fail("采集取消或列表已变化");
    const confirmed = Array.isArray(first.data.items) ? new Map(first.data.items.map(d => [d.id,d])) : null;
    if (!confirmed) return fail("网关缺少去重确认，请更新并重启网关");
    const queueRefs = selected.filter(item => ["new", "changed", "retry"].includes(confirmed.get(item.id)?.kind));
    const confirmedSkip = skipped + Array.from(confirmed.values()).filter(item => item.kind === "skip").length;
    const confirmedManual = manual + Array.from(confirmed.values()).filter(item => item.kind === "manual").length;
    if (!queueRefs.length) {
      const phase = `新增 0/${target}；重复跳过 ${confirmedSkip}，待查缺补录 ${confirmedManual}；数据已由其他批次采集`;
      progress(phase,{status:"partial",new_succeeded:0,duplicates:confirmedSkip,manual_pending:confirmedManual});
      show(phase,true);
      return {ok:true,skipped:"already-stored",result:first.data};
    }
    if (mode === "detail" || queueRefs.some(item => ["changed", "retry"].includes(confirmed.get(item.id)?.kind)
        || confirmed.get(item.id)?.missing?.length)) {
      const items = queueRefs.filter(item => mode === "detail" || confirmed.get(item.id)?.kind !== "new"
        || confirmed.get(item.id)?.missing?.length).map(item => {
        const title = item.card.querySelector(".smallCardTitle, .bigCardTitle");
        const d = confirmed.get(item.id);
        return {id:item.id, url:item.url, kind:mode === "list" && d.kind === "new" ? "retry" : d.kind, snapshot_round:d.snapshot_round,
          prefix:title ? title.getAttribute("productname") || title.textContent || "" : ""};
      });
      items.sort((a,b) => Number(b.kind === "retry") - Number(a.kind === "retry"));
      const result = await bg({type:"detail-start", taskId, items, round:first.data.round,
                               duplicates:confirmedSkip, manual_pending:confirmedManual,
                               new_succeeded:first.data.new_listings,
                               detail_interval_ms:CONFIG.detail_interval_ms});
      if (!result.ok) return fail(result.err || "详情队列启动失败");
      show(`详情队列已启动，共 ${total} 张；进度见扩展弹窗`, true);
      // 详情全部成功才在源列表标记去重；由下方状态轮询确认。
      return {ok:true, queued:true, taskId};
    }
    const d = first.data;
    const missing = first.titleStats ? first.titleStats.cards - first.titleStats.captured : 0;
    const complete = !missing && freshCount >= target && d.accepted_ids.length === total;
    if (complete) markIngested();
    const phase = `新增 ${d.new_listings}/${target}，更新 ${d.updated_ids.length}，重复跳过 ${confirmedSkip}，待补录 ${confirmedManual}`
      + titleStatus(first.titleStats) + (freshCount < target ? `；新账号不足（${stats.stop}）` : "");
    progress(phase, {status:complete ? "done" : "partial", total, processed:total,
      succeeded:d.accepted_ids.length, failed:missing, new_succeeded:d.new_listings,
      duplicates:confirmedSkip, updated:d.updated_ids.length, manual_pending:confirmedManual});
    show(phase, complete);
    return {ok:true, result:d, sweep:stats, title_collection:first.titleStats};
  }

  async function ingestBatch(pageUrl, fail) {
    const cfg = frozenConfig, startedAt = Date.now();
    const expanded = await expandCards(cfg.target_count, pageUrl);
    const selected = expanded.selected, queued = [], quality = new Map();
    const totals = {prices:0, new_succeeded:0, updated:0, text_complete:0, scanned:expanded.stats.final, blocks:0};
    let round = null, stop = expanded.stats.stop;
    async function submit(path, payload) {
      if (cancelled || location.href !== pageUrl) throw new Error("采集取消或页面已变化");
      const res = await bg({type:"ingest", path, payload:{...batchIdentity(), ...payload}});
      if (!res.ok || !res.data?.ok) throw new Error(res.data?.error || res.err || "批次提交失败");
      return res.data;
    }
    for (let offset=0; offset<selected.length; offset+=200) {
      if (!await waitVerification(pageUrl)) return fail("采集取消或页面已变化");
      const refs = selected.slice(offset,offset+200), ids = refs.map(i=>i.id);
      progress(`保存价格事实 · 执行块 ${Math.floor(offset/200)+1}`, {...totals});
      const first = await submit("/ingest/cards", {url:pageUrl, html:SWEEP.capture(document,refs,{}, {compact:true}),
        selected_ids:ids, sweep:expanded.stats});
      totals.blocks++; totals.prices += first.accepted_ids.length;
      totals.new_succeeded += first.new_listings; totals.updated += first.updated_ids.length;
      round = first.round;
      const decisions = new Map(first.items.map(item=>[item.id,item]));
      if (cfg.collection_mode !== "detail") {
        const info = refs.map(item=> {
          const node = item.card.querySelector(".smallCardTitle, .bigCardTitle");
          return {id:item.id, code:node?.getAttribute("productuniqueno"), prefix:node?.getAttribute("productname") || node?.textContent || ""};
        });
        const cached = await submit("/collection/text-cache/query", {items:info});
        const titleStarted = Date.now();
        let stats = {titles:{}, provenance:{}, stop:"done", cards:0,captured:0,requested:0,attempts:0};
        if (TITLES) stats = await TITLES.collect(document,window,{ids, refs, cached:cached.items,
          guard:true, intervalMs:cfg.title_interval_ms, forceRefresh:cfg.force_text_refresh,
          alive:()=>!cancelled && location.href===pageUrl, waitUntilReady:()=>waitVerification(pageUrl),
          progress:(captured,cards)=>progress(`完整文字 ${captured}/${cards} · 执行块 ${totals.blocks}`, totals)});
        const textItems = refs.filter(item=>stats.titles[item.id]).map(item=> {
          const d = decisions.get(item.id), source = stats.provenance?.[item.id] || cached.items[item.id]
            || {source:"title-api", acquired_at:new Date().toISOString()};
          return {id:item.id, url:item.url, snapshot_round:d.snapshot_round, title:stats.titles[item.id], ...source};
        });
        if (textItems.length) {
          const saved = await submit("/collection/text", {items:textItems});
          for (const q of saved.items) quality.set(q.id,q);
          totals.text_complete += saved.items.length;
          await submit("/collection/text-cache/store", {items:textItems.filter(i=>!i.reused).map(item=>({
            id:item.id, code:info.find(i=>i.id===item.id)?.code, title:item.title, source:item.source, acquired_at:item.acquired_at}))});
        }
        // 先保存诊断，再决定回退；访问等待跨页面与网关重启保留。
        await bg({type:"collection-progress",taskId,phase:`全文阶段 ${Date.now()-titleStarted} 毫秒；尝试 ${stats.attempts || 0}`,
          ...totals,diagnostics:stats.diagnostics || []});
        if (["rate-limit","auth-required","access-denied","session-not-ready","access-budget-exhausted","access-permit-timeout","site-access-held","unexpected-content","business-error","unavailable","bridge-timeout"].includes(stats.stop)) {
          stop = stats.stop;
          progress(`价格已保存；${stop}，未完成资料已保留`, {status:"partial", ...totals});
          return {ok:true, partial:true, stop, ...totals};
        }
      }
      for (const item of refs) {
        const d=decisions.get(item.id), q=quality.get(item.id);
        if (!d?.snapshot_round) continue;
        if (cfg.collection_mode === "detail" || !q?.complete) {
          const title=item.card.querySelector(".smallCardTitle, .bigCardTitle");
          queued.push({id:item.id, url:item.url, snapshot_round:d.snapshot_round,
            kind:cfg.purpose === "detail_enrich" ? "enrich" : d.kind === "retry" || d.kind === "manual" ? "enrich" : d.kind,
            prefix:title?.getAttribute("productname") || title?.textContent || ""});
        }
      }
      progress(`执行块 ${totals.blocks} 完成；详情待补 ${queued.length}`, {...totals, fallback:queued.length});
    }
    if (queued.length) {
      const reply=await bg({type:"detail-start", taskId, items:queued, round, ...totals, updated:totals.updated,
        detail_interval_ms:cfg.detail_interval_ms});
      if (!reply.ok) return fail(reply.err || "详情回退启动失败");
      show(`价格 ${totals.prices}、完整文字 ${totals.text_complete}；详情回退 ${queued.length}`,true);
      return {ok:true,queued:true,taskId,...totals};
    }
    const done = (cfg.purpose === "price_refresh" ? totals.prices : cfg.purpose === "detail_enrich"
      ? selected.length : totals.new_succeeded) >= cfg.target_count;
    progress(`任务${done ? "完成" : "部分完成"}；${stop}；${Date.now()-startedAt} 毫秒`,
      {status:done ? "done" : "partial", processed:selected.length,succeeded:selected.length,...totals});
    if (done) markIngested();
    return {ok:true,partial:!done,stop,...totals};
  }

  // MAIN仅获得是否授权；批次/代次与本地凭据不进入页面世界。
  window.addEventListener("pxb7-access-request", async event => {
    let msg; try {msg=JSON.parse(event.detail);} catch (_) {return;}
    if (!activeBatch || !taskId || typeof msg.token!=="string" || msg.token.length>100) return;
    const reply = await bg({type:"collection-access", taskId, pageUrl:location.href, action:"text", id:msg.id});
    window.dispatchEvent(new window.CustomEvent("pxb7-access-result", {detail:JSON.stringify({token:msg.token,
      ok:!!reply.data?.ok,error:reply.data?.error || reply.err})}));
  });

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
    if (msg?.type === "verification-confirmed") {
      if (msg.taskId !== taskId || !VERIFY || VERIFY.detect(document, window).blocked) {
        sendResponse({ok:false, err:"页面仍有验证，或任务已变化"}); return false;
      }
      manualVerificationConfirmed = true;
      sendResponse({ok:true}); return false;
    }
    if (msg && msg.type === "detail-completed") {
      if (location.href === msg.url) {markIngested(); show("详情队列全部入库完成", true);}
      sendResponse({ok:true}); return false;
    }
    if (msg && msg.type === "collect-cancel") {
      if (taskId && msg.taskId && taskId !== msg.taskId) {sendResponse({ok:false}); return false;}
      manualVerificationConfirmed = false;
      show("已取消；已入库数据保留，可重新采集本页", true);
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
  bg({type:"detail-context", pageUrl:location.href}).then(async context => {
    workerPage = !!context.worker;
    if (workerPage) {
      if (context.job) {
        await fetchConfig(); // 工作页也需要网站验证策略，不能沿用脚本默认manual。
        try { await runWorker(context.job); }
        catch (_) { await bg({type:"detail-result", taskId:context.job.taskId, nonce:context.job.nonce, error:"detail-read-error"}); }
      }
      return;
    }
    await fetchConfig();
    // 来自看板的明确补齐操作，采集一次，不改变用户的自动采集开关。
    setTimeout(() => { ingest(requestedDetail(), context.resumeTask); }, CONFIG.spa_settle_ms || SPA_SETTLE_DEFAULT_MS);
  });
  setInterval(fetchConfig, CONFIG_POLL_MS);
})();
