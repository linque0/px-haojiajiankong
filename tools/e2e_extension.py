"""浏览器自动化测试：Playwright 启动 Edge 加载**未打包扩展**，验证独立插件的采集与看板。

前置：网关在跑（未跑会用分离进程自动拉起）。
运行：pxb7-price-monitor/.venv/Scripts/python.exe tools/e2e_extension.py
说明：会短暂打开一个 Edge 窗口（临时配置文件，测完自动关闭）。

验证点：
1. MV3 扩展被浏览器加载（Service Worker 出现，可解析扩展 ID）；
2. 内容脚本在真实 pxb7 列表页生效：日志角标出现，网关收到采集（新批次或拦截页拒收计数）；
3. sweep.js（每次采集张数的页内加载决策）在浏览器里逐条通过决策表断言；
4. 弹窗看板（popup.html）渲染：连接状态 + 状态卡 + 采集目标多选 + 数据路径区 + 每次采集张数；
5. 完整看板（http://127.0.0.1:8765/）：勾选/取消采集目标即时生效、数据路径清单与根目录填充表单。

用户配置尊重：E2E 需要 auto_ingest=true 且目标放开才可验证链路，因此只在**需要时**临时调整
（走 desktop 的回环白名单守卫），结束时按原值恢复——不会覆盖用户在弹窗里的设置。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pxb7 import config as cfg                      # noqa: E402
from pxb7 import desktop as D                       # noqa: E402

EXT_DIR = PROJECT_ROOT / "extension" / "pxb7-extension"
LIST_URL = "https://www.pxb7.com/buy/10026/1"


def _ensure_gateway(settings) -> str:
    if D.gateway_alive():
        return "already-running"
    D.spawn_gateway(settings)
    if not D.wait_gateway(seconds=12):
        raise RuntimeError("网关拉起超时，请查看 data/logs/gateway.log")
    return "spawned"


def _post_config(**fields) -> None:
    """写网关 /config（部分键更新）——走 desktop 的回环白名单 + 禁重定向守卫。"""
    D.gateway_post_json("/config", fields)


def _eval_in_isolated_world(context, page, extension_id: str, expression: str,
                            timeout_s: float = 15.0):
    """在扩展内容脚本的**隔离世界**里执行表达式并取回值。

    内容脚本（含 sweep.js）运行在 isolated world，页面主世界看不到它的全局对象，
    因此普通的 page.evaluate 断言不了——用 CDP 的 Runtime 域定位扩展的隔离上下文
    再求值。Chromium 给隔离世界起的名字是**扩展名**（本机实测），少数版本用扩展 ID，
    两个都匹配；同时排除 Playwright 自己的 utility world。
    """
    manifest = json.loads((EXT_DIR / "manifest.json").read_text(encoding="utf-8"))
    wanted = {extension_id, str(manifest.get("name") or "")}

    session = context.new_cdp_session(page)
    contexts: list = []
    session.on("Runtime.executionContextCreated",
               lambda event: contexts.append(event["context"]))
    session.send("Runtime.enable")

    def _match(items: list):
        return next((c for c in items
                     if c.get("auxData", {}).get("isDefault") is False
                     and c.get("name") in wanted), None)

    deadline = time.time() + timeout_s
    target = _match(contexts)
    while time.time() < deadline and target is None:
        time.sleep(0.3)
        target = _match(contexts)
    if target is None:
        seen = [c.get("name") for c in contexts]
        raise AssertionError(f"未找到扩展的内容脚本隔离上下文（已见上下文：{seen}）")
    reply = session.send("Runtime.evaluate", {
        "expression": expression, "contextId": target["id"],   # CDP 上下文描述里字段名是 id
        "returnByValue": True, "awaitPromise": False})
    if "exceptionDetails" in reply:
        raise AssertionError(f"隔离世界求值异常：{reply['exceptionDetails']}")
    return reply["result"].get("value")


def _browser_checks(before: dict) -> dict:
    """浏览器侧全部断言；返回结果字典。"""
    from playwright.sync_api import sync_playwright

    result: dict = {"baseline": {
        "batches": before["stats"]["batches"],
        "risk_pages_rejected": before["stats"]["risk_pages_rejected"],
        "snapshot_rows": before["db"]["snapshot_rows"]}}

    with sync_playwright() as p:
        profile = tempfile.mkdtemp(prefix="pxb7-e2e-profile-")
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile, channel="msedge", headless=False,
            viewport={"width": 1280, "height": 900},
            args=[f"--disable-extensions-except={EXT_DIR}",
                  f"--load-extension={EXT_DIR}",
                  "--enable-unsafe-extension-debugging",   # Chromium 137+ 恢复命令行加载的门
                  "--no-first-run", "--no-default-browser-check"])
        try:
            # 1) 加载扩展：新版 Chromium 系已弱化 --load-extension，优先用 CDP 官方通道
            page = context.new_page()
            page.goto("about:blank")
            extension_id = None
            try:
                cdp = context.new_cdp_session(page)
                loaded = cdp.send("Extensions.loadUnpacked", {"path": str(EXT_DIR)})
                extension_id = loaded.get("id") if isinstance(loaded, dict) else None
                print(f"[e2e] CDP Extensions.loadUnpacked → {extension_id}")
            except Exception as exc:                     # 旧版浏览器：退回启动参数加载
                print(f"[e2e] CDP 加载不可用（{exc}），使用 --load-extension 参数")

            sw_url = None
            deadline = time.time() + 20
            while time.time() < deadline:
                for worker in context.service_workers:
                    if "chrome-extension://" in worker.url:
                        sw_url = worker.url
                        extension_id = extension_id or sw_url.split("/")[2]
                        break
                if extension_id:
                    break
                time.sleep(0.5)
            if not extension_id:
                raise RuntimeError("扩展未能加载（CDP 与命令行两种方式均失败）")
            result["extension_id"] = extension_id
            result["service_worker"] = sw_url
            print(f"[e2e] 扩展已加载：{extension_id}（SW={sw_url or '未观察到，见角标验证'}）")

            # 2) 真实 pxb7 列表页：内容脚本采集
            page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(10)                      # settle + 采集 + 角标
            badge_text = page.evaluate("""() => {
                const nodes = Array.from(document.querySelectorAll('div'));
                const badge = nodes.find((d) => (d.textContent || '').startsWith('pxb7采集'));
                return badge ? badge.textContent : null;
            }""")
            result["badge"] = badge_text
            result["page_url"] = page.url
            print(f"[e2e] 页内角标：{badge_text}")

            after = D.gateway_json("/stats")
            delta_batches = after["stats"]["batches"] - before["stats"]["batches"]
            delta_rejected = (after["stats"]["risk_pages_rejected"]
                              - before["stats"]["risk_pages_rejected"])
            delta_snapshots = after["db"]["snapshot_rows"] - before["db"]["snapshot_rows"]
            result["collect"] = {"delta_batches": delta_batches,
                                 "delta_risk_rejected": delta_rejected,
                                 "delta_snapshots": delta_snapshots}
            assert badge_text is not None, "内容脚本未运行（无角标）"
            assert delta_batches >= 1 or delta_rejected >= 1, \
                "网关未收到任何采集请求（内容脚本→SW→网关链路断了）"

            # 2b) sweep.js 决策表：内容脚本在隔离世界里执行，用 CDP 定位其上下文后逐条断言
            sweep_checks = _eval_in_isolated_world(context, page, extension_id, """(() => {
                const S = window.PXB7_SWEEP;
                if (!S) return { loaded: false };
                const base = { cards: 16, target: 48, rounds: 0, maxRounds: 12,
                               stalled: 0, maxStalled: 2 };
                return {
                    loaded: true,
                    cards: S.countCards(document),
                    clamp: [S.clampTarget(0), S.clampTarget(48), S.clampTarget(999)],
                    actions: [
                        S.nextAction({ ...base, cards: 48 }),
                        S.nextAction({ ...base, rounds: 12 }),
                        S.nextAction({ ...base, stalled: 2 }),
                        S.nextAction({ ...base, hasMoreControl: true }),
                        S.nextAction({ ...base, hasMoreControl: false }),
                    ],
                    fakeCounts: [
                        S.countCards({ querySelectorAll: (s) =>
                            new Array(s === "[productid]" ? 32 : 0).fill(0) }),
                        S.countCards({ querySelectorAll: () => [] }),
                    ],
                };
            })()""")
            result["sweep"] = sweep_checks
            print(f"[e2e] sweep.js：{json.dumps(sweep_checks, ensure_ascii=False)}")
            assert sweep_checks["loaded"], "sweep.js 未注入（content_scripts 顺序或文件缺失）"
            assert sweep_checks["clamp"] == [16, 48, 200], "目标张数钳制不符"
            assert sweep_checks["actions"] == ["done-target", "done-rounds", "done-stalled",
                                               "click", "scroll"], "决策表不符"
            assert sweep_checks["fakeCounts"] == [32, 0], "卡片计数兜底不符"

            # 3) 弹窗看板渲染（chrome-extension://<id>/popup.html）
            popup = context.new_page()
            popup.goto(f"chrome-extension://{extension_id}/popup.html",
                       wait_until="domcontentloaded", timeout=30000)
            time.sleep(2.5)
            popup_state = popup.evaluate("""() => ({
                state: document.getElementById('state').textContent,
                tiles: Array.from(document.querySelectorAll('#tiles .tile'))
                    .map((t) => t.textContent.trim()),
                rounds_children: document.querySelectorAll('#rounds > *').length,
                version: document.getElementById('ver').textContent,
                targets: document.querySelectorAll('#targets input[data-task]').length,
                paths_rows: document.querySelectorAll('#paths .path-row').length,
                cards_target: (document.getElementById('c-cards') || {}).value || null,
                hasButtons: ['save', 'grab', 'full', 'stop']
                    .every((id) => !!document.getElementById(id)),
            })""")
            result["popup"] = popup_state
            print(f"[e2e] 弹窗：{json.dumps(popup_state, ensure_ascii=False)}")
            assert popup_state["state"].startswith("已连接"), "弹窗未连上网关"
            assert len(popup_state["tiles"]) == 6, "弹窗状态卡未渲染"
            assert popup_state["hasButtons"], "弹窗按钮缺失"
            assert popup_state["targets"] >= 4, "弹窗采集目标多选未渲染"
            assert popup_state["paths_rows"] >= 9, "弹窗数据路径区未渲染"
            assert popup_state["cards_target"] == "16", \
                f"「每次采集张数」默认应为 16，实际 {popup_state['cards_target']}"

            # 3b) 扩展自更新通道（v0.3.0+）：check-update 消息 → 同版本不误报更新
            self_update = popup.evaluate(
                """() => new Promise((resolve) => {
                    chrome.runtime.sendMessage({type: 'check-update'},
                                               (reply) => resolve(reply || null));
                })""")
            result["self_update"] = self_update
            print(f"[e2e] 自更新检查：{json.dumps(self_update, ensure_ascii=False)}")
            assert self_update and self_update.get("latest"), "自更新检查未返回磁盘最新版本"
            assert self_update.get("update_available") is False, "同版本不得误报更新"
            assert popup.evaluate(
                "() => document.getElementById('upd-ext').style.display") == "none", \
                "无更新时不应显示「应用扩展更新」按钮"

            # 4) 完整看板（网关伺服页）：采集目标即时生效 + 数据路径展示/表单
            dash = context.new_page()
            dash.goto("http://127.0.0.1:8765/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(2.5)
            dash_state = dash.evaluate("""() => ({
                targets: document.querySelectorAll('#targets input[data-task]').length,
                mode: (document.querySelector('#targets .mode') || {}).textContent || '',
                paths: Array.from(document.querySelectorAll('#paths tbody tr'))
                    .map((tr) => tr.cells[1].textContent.trim()),
                has_form: ['p-db', 'p-raw_root', 'p-runs', 'p-log_dir']
                    .every((id) => !!document.getElementById(id)),
            })""")
            result["dashboard"] = dash_state
            print(f"[e2e] 看板：目标 {dash_state['targets']} 项、路径 {len(dash_state['paths'])} 条、"
                  f"表单={'有' if dash_state['has_form'] else '缺'}")
            assert dash_state["targets"] >= 4, "看板采集目标未渲染"
            assert len(dash_state["paths"]) >= 9, "看板数据路径未渲染"
            assert dash_state["has_form"], "看板路径自定义表单缺失"

            # 勾选鸣潮 → 网关 targets 立即生效
            dash.locator('#targets input[data-task="wuwa_official"]').check()
            deadline = time.time() + 8
            effective: list = []
            while time.time() < deadline:
                effective = D.gateway_json("/stats")["targets"]["effective"]
                if "wuwa_official" in effective:
                    break
                time.sleep(0.5)
            assert "wuwa_official" in effective, f"看板勾选未生效：{effective}"
            result["targets_after_check"] = effective
            print(f"[e2e] 勾选鸣潮后生效目标：{effective}")
            # 取消勾选 → 回默认任务（E2E 临时配置下即空 targets，收尾由 main 恢复原值）
            dash.locator('#targets input[data-task="wuwa_official"]').uncheck()
            deadline = time.time() + 8
            targets_now: dict = {}
            while time.time() < deadline:
                targets_now = D.gateway_json("/stats")["targets"]
                if targets_now["mode"] == "default":
                    break
                time.sleep(0.5)
            assert targets_now["mode"] == "default", f"取消勾选未回默认：{targets_now}"
            print(f"[e2e] 取消勾选后回默认目标：{targets_now['effective']}")
            # 根目录填充（只填表单不应用：避免动真实数据位置）
            dash.fill("#p-root", r"D:\pxb7-data")
            dash.click("#p-fill-root")
            result["root_fill_db"] = dash.input_value("#p-db")
            assert result["root_fill_db"].endswith("pxb7.duckdb"), result["root_fill_db"]
            print(f"[e2e] 根目录填充预览：{result['root_fill_db']}")
        finally:
            context.close()
    return result


def main() -> int:
    settings = cfg.load_settings()
    started = _ensure_gateway(settings)
    before = D.gateway_json("/stats")
    print(f"[e2e] 网关：{started}；基线：批次={before['stats']['batches']} "
          f"拦截拒收={before['stats']['risk_pages_rejected']} 快照={before['db']['snapshot_rows']}")

    live_config = before.get("config") or {}
    saved_auto = bool(live_config.get("auto_ingest", True))
    saved_targets = list(live_config.get("targets") or [])
    need_config_tweak = (not saved_auto) or bool(saved_targets)
    if need_config_tweak:
        _post_config(auto_ingest=True, targets=[])
        print(f"[e2e] 临时放开采集配置用于链路验证（原 auto_ingest={saved_auto}、"
              f"targets={saved_targets}），结束按原值恢复")
        before = D.gateway_json("/stats")

    try:
        result = _browser_checks(before)
    finally:
        if need_config_tweak:
            _post_config(auto_ingest=saved_auto, targets=saved_targets)
            print(f"[e2e] 已恢复原配置：auto_ingest={saved_auto}、targets={saved_targets}")

    print("\n[e2e] 结果：")
    print(json.dumps({"gateway": started, **result}, ensure_ascii=False, indent=2))
    print("\n[e2e] 通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
