/**
 * pxb7 采集助手（前端 B）—— 弹窗看板（扩展 UI）
 *
 * 数据与操作全部对接本机网关的回环 HTTP 接口（host_permissions 已声明，扩展页 fetch 不受页面 CORS 限制）。
 * 说明（安全边界）：本文件**不含任何 SQL / 数据库访问**——所有持久化都在 Python 网关内
 * 以参数化 DuckDB 查询完成；标签页操作集中在背景服务，此处只有固定的 HTTP 路径与 UI 渲染。
 */
"use strict";

const GATEWAY = "http://127.0.0.1:8765";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s === null || s === undefined ? "—" : s)
  .replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (n) => (n === null || n === undefined) ? "—" : Number(n).toLocaleString("zh-CN");

async function gfetch(path, options) {
  try {
    const response = await fetch(GATEWAY + path, options);
    const text = await response.text();
    let data = null;
    try { data = JSON.parse(text); } catch (e) { /* 非 JSON 响应 */ }
    return { ok: true, status: response.status, data };
  } catch (e) {
    return { ok: false, err: "网关不可达（双击桌面「pxb7采集看板」可启动服务）" };
  }
}

function note(text, bad) {
  const el = $("note");
  el.textContent = text;
  el.className = "note" + (bad ? " bad" : "");
  setTimeout(() => { el.textContent = ""; }, 5000);
}

function errorText(result, fallback) {
  if (result && result.data && typeof result.data.error === "string") return result.data.error;
  if (result && typeof result.err === "string") return result.err;
  return fallback;
}

function renderTiles(s) {
  const st = s.stats;
  const rate = st.cards_seen ? Math.round((st.cards_parsed / st.cards_seen) * 100) + "%" : "—";
  const tile = (k, v) =>
    `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  $("tiles").innerHTML =
    tile("快照", fmt(s.db.snapshot_rows)) + tile("解析率", rate) + tile("新增", fmt(st.new_listings)) +
    tile("词表命中", fmt(s.db.keyword_hits)) + tile("详情回填", fmt(st.details_stored)) +
    tile("拦截拒收", fmt(st.risk_pages_rejected));
}

function renderBars(rows) {
  const top = Math.max(1, ...rows.map((r) => r.rows));
  const items = rows.map((r) =>
    `<div class="bar"><span class="label">${esc(r.round)}</span>` +
    `<span class="track"><span class="fill" style="width:${(r.rows / top) * 100}%"></span></span>` +
    `<span class="num">${fmt(r.rows)}</span></div>`).join("");
  $("rounds").innerHTML = items || '<div style="color:#8b93a7">暂无轮次数据——去 pxb7 逛一圈</div>';
}

function renderBatches(batches) {
  const items = batches.slice(-4).reverse().map((b) => {
    const head = esc(String(b.at).slice(11));
    const body = b.kind === "cards"
      ? `${b.cards_parsed}/${b.cards_seen} 张 → 入库 ${b.snapshots}`
      : `收藏 ${b.favorites_cnt === null ? "未公示" : b.favorites_cnt}`;
    return `<div>${head} ${b.kind === "cards" ? "列表" : "详情"} · ${body}</div>`;
  }).join("");
  $("batches").innerHTML = items || '<div style="color:#8b93a7">暂无批次</div>';
}

function renderError(text) {
  $("state").textContent = "网关未连接";
  $("state").className = "state bad";
  $("tiles").innerHTML = "";
  $("rounds").innerHTML = "";
  $("batches").innerHTML = `<div class="note bad">${esc(text)}</div>`;
}

// ---------- 采集目标（多选：只采集勾选的游戏） ----------
let targetsSig = "";

function renderTargets(t) {
  const sig = JSON.stringify(t);
  if (sig === targetsSig) return;          // 轮询刷新不重绘，避免打断勾选
  targetsSig = sig;
  const box = $("targets");
  const rows = (t.available || []).map((x) =>
    `<label class="tg${x.enabled ? "" : " disabled"}">` +
    `<input type="checkbox" data-task="${esc(x.task_id)}"${x.selected ? " checked" : ""}` +
    `${x.enabled ? "" : " disabled"}> <span>${esc(x.name)}</span>` +
    `<span class="cnt">${fmt(x.snapshots)} 行</span></label>`).join("")
    || '<div class="muted">无可用任务</div>';
  const mode = t.mode === "selected"
    ? `已选 ${(t.effective || []).length} 个目标（${esc((t.effective || []).join("、"))}）`
    : `未勾选 → 使用网关启动任务（${esc(t.default_task || "—")}）`;
  box.innerHTML = rows + `<div class="mode">${mode}；只采集勾选游戏的页面</div>`;
  box.querySelectorAll("input[data-task]").forEach((el) =>
    el.addEventListener("change", saveTargets));
}

async function saveTargets() {
  const ids = Array.from($("targets").querySelectorAll("input[data-task]:checked"))
    .map((el) => el.dataset.task);
  const res = await gfetch("/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ targets: ids }),      // 部分更新：其余配置项保持不变
  });
  if (res.ok && res.data && res.data.ok) {
    renderTargets(res.data.targets || {});
    note(ids.length ? `采集目标已保存（${ids.length} 个）✓` : "已清空目标 → 使用默认任务 ✓");
  } else {
    note("目标保存失败：" + errorText(res, "未知错误"), true);
  }
}

// ---------- 数据存放位置（展示 + 打开/复制） ----------
let pathsSig = "";

function renderPaths(p) {
  const sig = JSON.stringify(p);
  if (sig === pathsSig) return;           // 轮询刷新不重绘，避免打断点击/复制
  pathsSig = sig;
  const items = p.items || [];
  $("paths").innerHTML = items.map((it) =>
    `<div class="path-row"><span class="pl" title="${esc(it.label)}">${esc(it.label)}</span>` +
    `<code title="${esc(it.path)}">${esc(it.path)}</code>` +
    `<button class="mini" data-copy="${esc(it.path)}" title="复制路径">复制</button>` +
    `<button class="mini" data-open="${esc(it.key)}" title="在资源管理器中打开">打开</button>` +
    `</div>`).join("") +
    `<div class="muted">覆写文件：${esc(p.override_file || "—")}</div>`;
  $("paths").querySelectorAll("button[data-copy]").forEach((el) =>
    el.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(el.dataset.copy);
        note("路径已复制 ✓");
      } catch (e) {
        note("复制失败；可在完整看板手动复制", true);
      }
    }));
  $("paths").querySelectorAll("button[data-open]").forEach((el) =>
    el.addEventListener("click", async () => {
      const res = await gfetch("/open-folder", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: el.dataset.open }),
      });
      if (!(res.ok && res.data && res.data.ok)) {
        note("打开失败：" + errorText(res, "无响应"), true);
      }
    }));
}

async function refresh() {
  const res = await gfetch("/stats");
  if (!res.ok || !res.data || !res.data.ok) {
    renderError(res.err || "无响应");
    return;
  }
  const s = res.data;
  const state = $("state");
  state.textContent = s.stats.last_batch_at
    ? "已连接 · " + s.stats.last_batch_at.slice(11) : "已连接 · 暂无批次";
  state.className = "state";
  renderTiles(s);
  renderBars((s.db.rounds || []).slice(-5));
  renderBatches(s.recent_batches || []);
  if (s.targets) renderTargets(s.targets);
  if (s.paths) renderPaths(s.paths);
  const c = s.config || {};
  $("c-auto").checked = !!c.auto_ingest;
  $("c-interval").value = c.reingest_interval_min;
  $("c-cards").value = c.cards_target || 16;
  $("c-settle").value = c.spa_settle_ms;
  $("c-debug").checked = !!c.debug;
}

// ---------- 操作 ----------
$("save").addEventListener("click", async () => {
  const res = await gfetch("/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      auto_ingest: $("c-auto").checked,
      reingest_interval_min: Number($("c-interval").value),
      cards_target: Number($("c-cards").value),
      spa_settle_ms: Number($("c-settle").value),
      debug: $("c-debug").checked,
    }),
  });
  if (res.ok && res.data && res.data.ok) {
    note("设置已保存并生效 ✓");
  } else {
    note("保存失败：" + errorText(res, "未知错误"), true);
  }
});

$("grab").addEventListener("click", () => {
  // 标签页操作集中在背景服务（chrome.tabs.*），弹窗只发固定消息类型
  chrome.runtime.sendMessage({ type: "collect-active" }, (reply) => {
    if (chrome.runtime.lastError) { note(chrome.runtime.lastError.message, true); return; }
    if (reply && reply.ok) note("已请求采集当前页（看页面右下角角标）");
    else note(errorText(reply, "请求失败"), true);
  });
});

$("full").addEventListener("click", () => {
  chrome.tabs.create({ url: new URL("/", GATEWAY).href });
});

$("stop").addEventListener("click", async () => {
  if (!window.confirm("停止本机采集服务？浏览页面的自动采集将暂停，直到再次打开看板/服务。")) return;
  const res = await gfetch("/shutdown", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  if (res.ok) note("服务已停止；再次使用：双击桌面「pxb7采集看板」");
  else note("停止失败：" + errorText(res, "无响应"), true);
});

// ---------- 扩展自更新（按磁盘最新代码重载；解包扩展的「重新加载」自动化） ----------
let extUpdate = { update_available: false, latest: null };

function renderExtUpdate() {
  const own = chrome.runtime.getManifest().version;
  $("upd-ext").style.display = extUpdate.update_available ? "inline-block" : "none";
  $("ver").textContent = extUpdate.update_available
    ? `扩展 v${own} → v${extUpdate.latest}` : `扩展 v${own}`;
}

function checkExtUpdate(cb) {
  try {
    chrome.runtime.sendMessage({ type: "check-update" }, (reply) => {
      if (!chrome.runtime.lastError && reply && reply.latest) {
        extUpdate = { update_available: !!reply.update_available, latest: reply.latest };
      }
      renderExtUpdate();
      if (cb) cb();
    });
  } catch (e) {
    renderExtUpdate();
  }
}

$("upd-ext").addEventListener("click", () => {
  if (!window.confirm("重新加载扩展以应用新版本？（弹窗会关闭，属正常现象）")) return;
  chrome.runtime.sendMessage({ type: "apply-update" }, () => {
    if (chrome.runtime.lastError) note(chrome.runtime.lastError.message, true);
    else window.close();
  });
});

// ---------- 启动 ----------
checkExtUpdate();
refresh();
setInterval(refresh, 3000);
