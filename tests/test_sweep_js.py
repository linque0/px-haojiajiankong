"""sweep.js（页内"加载更多"决策）的接线检查。

背景：pxb7 列表页一次只渲染 16 张卡片（2026-10-03 实测：原神/鸣潮/三角洲/火影四个列表页的
raw dump 都恰好 16 张，且页面没有分页控件），所以"每次采集张数"要靠页内滚动/点站点自己的
加载控件把更多卡片渲染出来。sweep.js 承载这套判定（纯逻辑 + DOM 只读辅助）。

行为验证（决策表、计数兜底、控件识别）在真实浏览器里对**已加载的 sweep.js** 逐条断言，
见 tools/e2e_extension.py 的「sweep 决策表」段——那里跑的是浏览器实际注入的同一份文件，
比离线复刻更可信。本文件只做静态接线检查。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

EXT = PROJECT_ROOT / "extension" / "pxb7-extension"
SWEEP_JS = EXT / "sweep.js"


def test_sweep_js_exists_and_is_wired_into_manifest() -> None:
    assert SWEEP_JS.is_file(), "缺少 sweep.js"
    manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "0.5.3"
    js = manifest["content_scripts"][0]["js"]
    assert js == ["sweep.js", "titles.js", "content.js"], "辅助脚本必须先于 content.js 注入"
    text = SWEEP_JS.read_text(encoding="utf-8")
    for marker in ("countCards", "findMoreControl", "nextAction", "scrollForMore",
                   "clampTarget", "maxCards: 200", "intervalMs: 2000"):
        assert marker in text, f"sweep.js 缺少要素：{marker}"
    assert "Math.random" not in text, "节奏用固定间隔，不需要随机数"


def test_content_script_uses_sweep_and_keeps_single_page_default() -> None:
    content = (EXT / "content.js").read_text(encoding="utf-8")
    for marker in ("PXB7_SWEEP", "expandCards", "sendPage", "cards_target",
                   "countCards", "clampTarget", "stepInterval"):
        assert marker in content, f"内容脚本缺少要素：{marker}"
    assert "cards_target: 16" in content, "默认一次一页（16 张），不改变既有行为"
    assert "sweep: stats" in content, "加载更多后的第二次入库须带 sweep 统计（透明记录）"
    assert "SWEEP.countCards(document)" in content, "卡片计数统一走 sweep.js"


def test_userscript_has_same_sweep_contract() -> None:
    text = (PROJECT_ROOT / "extension" / "pxb7-collector.user.js").read_text(encoding="utf-8")
    for marker in ("clampTarget", "expandCards", "findMoreControl", "cards_target",
                   "pxb7-p-cards", "maxRounds: 12", "sweep: stats"):
        assert marker in text, f"油猴脚本缺少要素：{marker}"
    assert "@version      0.6.0" in text and 'SCRIPT_VERSION = "0.6.0"' in text
    # 双通道版本上报分开（看板按通道分别显示更新状态，不得混写）
    assert "channel=userscript" in text, "油猴脚本须以 userscript 通道上报版本"
    background = (EXT / "background.js").read_text(encoding="utf-8")
    assert 'params.set("channel", "extension")' in background, \
        "扩展须以 extension 通道上报版本（与油猴分开）"


def test_dashboard_tells_channels_apart() -> None:
    """看板更新提示按通道区分：扩展是最近使用的通道时，油猴旧脚本按「备用通道」提示而非催更。

    背景（2026-10-04 用户反馈）：看板曾显示「用户脚本 v0.1.0 → 最新 v0.6.0（建议更新）」，
    被误读成「扩展里没整合采集脚本」。实际扩展自带 sweep.js+content.js；横幅说的是
    Tampermonkey 里还装着的旧版油猴脚本。
    """
    dash = (PROJECT_ROOT / "extension" / "dashboard.html").read_text(encoding="utf-8")
    for marker in ("油猴备用通道", "停用旧脚本", "extRecentlyUsed", "油猴通道）v"):
        assert marker in dash, f"看板缺少通道区分要素：{marker}"
    assert "一键更新油猴脚本" in dash, "更新按钮须写明是油猴脚本通道（扩展更新另有入口）"


def test_ui_exposes_cards_target() -> None:
    popup_html = (EXT / "popup.html").read_text(encoding="utf-8")
    popup_js = (EXT / "popup.js").read_text(encoding="utf-8")
    dash = (PROJECT_ROOT / "extension" / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="c-cards"' in popup_html, "弹窗缺少「每次采集张数」输入"
    assert 'min="1" max="200"' in popup_html
    for name, text in (("popup.js", popup_js), ("dashboard.html", dash)):
        assert "c-cards" in text and "cards_target" in text, f"{name} 未接线 cards_target"
    assert 'id="c-cards"' in dash and 'min="1" max="200"' in dash
    assert "16 张" in popup_html or "16 张" in dash, "必须向用户说明站点一页 16 张"
    panel = (PROJECT_ROOT / "extension" / "pxb7-collector.user.js").read_text(encoding="utf-8")
    assert 'id="pxb7-p-cards"' in panel and "cards_target" in panel
