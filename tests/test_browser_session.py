"""浏览器会话层测试：登录态选择/降级、导航守卫、请求守卫。

默认不访问真实站点；唯一的真实浏览器用例只做「被守卫拒绝的导航」与 about:blank，
若本机未安装 chromium 内核则跳过。
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pxb7 import browser as B  # noqa: E402
from pxb7 import config as cfg  # noqa: E402
from pxb7.urlguard import UnsafeUrlError, assert_page_url  # noqa: E402


def _settings_with_states(tmp_path: Path, *, primary: bool, backup: bool) -> cfg.Settings:
    """构造主备登录态文件；primary/backup=True 时写入**含 cookie 的可用状态**。

    空 cookie 的 storage_state 视为未登录（choose_session 会降级游客态），
    因此这里默认给一个占位 cookie；需要测“文件存在但未登录”的场景请显式用空 cookie。
    """
    settings = cfg.load_settings()
    primary_path = tmp_path / "storage_primary.json"
    backup_path = tmp_path / "storage_backup.json"
    usable = '{"cookies": [{"name": "demo_session", "value": "placeholder", "domain": "www.pxb7.com", "path": "/"}], "origins": []}'
    if primary:
        primary_path.write_text(usable, encoding="utf-8")
    if backup:
        backup_path.write_text(usable, encoding="utf-8")
    return dataclasses.replace(
        settings,
        paths=dataclasses.replace(settings.paths,
                                  storage_primary=primary_path,
                                  storage_backup=backup_path))


# --------------------------------------------------------------------------- #
# 会话选择（登录态优先 v1.3）
# --------------------------------------------------------------------------- #
def test_no_storage_state_files_falls_back_to_guest(tmp_path: Path) -> None:
    settings = _settings_with_states(tmp_path, primary=False, backup=False)
    choice = B.choose_session(settings)
    assert choice.slot == B.SLOT_GUEST
    assert choice.collected_via == B.COLLECTED_VIA_GUEST
    assert choice.storage_state is None
    assert "游客态" in choice.reason or "guest" in choice.reason

    session = B.BrowserSession(settings)
    info = session.session_info()
    assert info["slot"] == "guest"
    assert info["collected_via"] == "guest", "没有登录态文件时必须如实标注 collected_via=guest"
    assert info["frequency_factor"] == settings.rate_limit.guest_factor
    assert info["storage_state"] is None


def test_primary_storage_state_preferred(tmp_path: Path) -> None:
    settings = _settings_with_states(tmp_path, primary=True, backup=True)
    choice = B.choose_session(settings)
    assert choice.slot == B.SLOT_PRIMARY
    assert choice.collected_via == B.COLLECTED_VIA_LOGIN
    assert choice.storage_state == settings.paths.storage_primary
    session = B.BrowserSession(settings)
    assert session.frequency_factor == settings.rate_limit.login_factor == 1.0


def test_backup_used_when_primary_missing(tmp_path: Path) -> None:
    settings = _settings_with_states(tmp_path, primary=False, backup=True)
    choice = B.choose_session(settings)
    assert choice.slot == B.SLOT_BACKUP
    assert choice.storage_state == settings.paths.storage_backup
    assert choice.collected_via == B.COLLECTED_VIA_LOGIN


def test_explicit_guest_mode_ignores_files(tmp_path: Path) -> None:
    settings = _settings_with_states(tmp_path, primary=True, backup=True)
    choice = B.choose_session(settings, mode="guest")
    assert choice.slot == B.SLOT_GUEST and choice.collected_via == B.COLLECTED_VIA_GUEST


def test_session_downgrade_chain(tmp_path: Path) -> None:
    settings = _settings_with_states(tmp_path, primary=True, backup=True)
    session = B.BrowserSession(settings)
    assert session.slot == B.SLOT_PRIMARY

    session.downgrade_to_backup("被踢")
    assert session.slot == B.SLOT_BACKUP
    assert session.collected_via == "login"
    assert session.frequency_factor == settings.rate_limit.backup_state_factor

    session.downgrade_to_guest("备用亦失效")
    assert session.slot == B.SLOT_GUEST
    assert session.collected_via == "guest"
    assert session.frequency_factor == settings.rate_limit.guest_factor
    assert len(session.downgrades) == 2


def test_login_invalid_handler_updates_risk_state(tmp_path: Path) -> None:
    from pxb7 import collector as C
    from pxb7 import risk as R

    settings = _settings_with_states(tmp_path, primary=True, backup=True)
    machine = R.RiskMachine(settings, state_path=tmp_path / "risk.json")
    session = B.BrowserSession(settings)

    via = C.handle_login_invalid(machine, session, task_id="genshin_official", reason="踢出")
    assert via == "login" and session.slot == B.SLOT_BACKUP
    assert machine.state.session_slot == R.SLOT_BACKUP
    assert machine.state.frequency_factor == settings.rate_limit.backup_state_factor

    via2 = C.handle_login_invalid(machine, session, task_id="genshin_official", reason="备用失效")
    assert via2 == "guest" and session.slot == B.SLOT_GUEST
    assert machine.state.session_slot == R.SLOT_GUEST
    assert machine.state.observe_until is not None


# --------------------------------------------------------------------------- #
# 导航守卫（不启动浏览器即可验证「被拒 URL 不发请求」）
# --------------------------------------------------------------------------- #
def test_empty_cookie_state_is_not_treated_as_logged_in(tmp_path: Path) -> None:
    """【回归】文件存在但无 cookie ⇒ 未登录：必须降级游客态并如实标注（不得标 login）。"""
    settings = cfg.load_settings()
    primary = tmp_path / "storage_primary.json"
    backup = tmp_path / "storage_backup.json"
    empty = '{"cookies": [], "origins": []}'
    primary.write_text(empty, encoding="utf-8")
    backup.write_text(empty, encoding="utf-8")
    settings = dataclasses.replace(settings, paths=dataclasses.replace(
        settings.paths, storage_primary=primary, storage_backup=backup))

    ok, why = B.validate_storage_state(primary)
    assert ok is False and "cookie" in why
    choice = B.choose_session(settings)
    assert choice.slot == B.SLOT_GUEST
    assert choice.collected_via == B.COLLECTED_VIA_GUEST
    assert "不可用" in choice.reason
    assert B.BrowserSession(settings).collected_via == "guest"


def test_corrupt_state_file_falls_back(tmp_path: Path) -> None:
    settings = cfg.load_settings()
    primary = tmp_path / "storage_primary.json"
    backup = tmp_path / "storage_backup.json"
    primary.write_text("{ not json", encoding="utf-8")
    backup.write_text('{"cookies": [{"name": "s", "value": "v"}], "origins": []}', encoding="utf-8")
    settings = dataclasses.replace(settings, paths=dataclasses.replace(
        settings.paths, storage_primary=primary, storage_backup=backup))
    choice = B.choose_session(settings)
    assert choice.slot == B.SLOT_BACKUP, "主登录态损坏应回落备用"
    assert "不可用" in choice.reason


def test_login_wall_detection() -> None:
    from pxb7 import risk as R
    assert R.detect_login_wall("请先登录后查看完整信息") is True
    assert R.detect_login_wall("登录后可见") is True
    assert R.detect_login_wall("原神 官服 满命 钟离") is False


def test_login_session_page_signals_include_login_kicked(tmp_path: Path) -> None:
    """登录态下出现登录墙 ⇒ 归因为「登录态失效」，游客态同样文本不误报。"""
    from pxb7 import collector as C
    html = "<html><body><div>请先登录后查看</div></body></html>"
    assert C.SIGNAL_LOGIN_KICKED in C._collect_page_signals(html, logged_in=True)
    assert C.SIGNAL_LOGIN_KICKED not in C._collect_page_signals(html, logged_in=False)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/admin",
    "http://localhost/buy/10026/1",
    "http://192.168.1.5/buy/10026/1",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/",
    "file:///C:/Windows/win.ini",
    "http://example.com/buy/10026/1",        # 不在 host 白名单
    "http://www.pxb7.com/buy/10026/1",       # 非 https
])
def test_page_guard_rejects_before_any_request(tmp_path: Path, url: str) -> None:
    settings = _settings_with_states(tmp_path, primary=False, backup=False)
    session = B.BrowserSession(settings)          # 未 start()：守卫判定不需要浏览器
    with pytest.raises(UnsafeUrlError):
        assert_page_url(url, allowed_hosts=settings.site.allowed_hosts, require_https=True)
    # 守卫拒绝的 URL 会记入统计且不产生导航
    result = session.goto(None, url)              # page=None 证明未触碰浏览器
    assert result.ok is False
    assert result.error and result.error.startswith("urlguard:")
    assert session.guard.requests_blocked == 1


# --------------------------------------------------------------------------- #
# 真实浏览器冒烟（可选）：只验证守卫与 about:blank，不访问目标站
# --------------------------------------------------------------------------- #
def test_real_browser_guard_blocks_without_navigating(tmp_path: Path) -> None:
    settings = _settings_with_states(tmp_path, primary=False, backup=False)
    session = B.BrowserSession(settings, headless=True)
    try:
        session.start()
    except B.BrowserUnavailable as exc:
        pytest.skip(f"本机无可用 chromium：{exc}")

    try:
        page = session.new_page()
        # 1) 白名单外的公网地址：守卫拒绝，不发起导航
        res = session.goto(page, "https://example.com/")
        assert res.ok is False and res.error.startswith("urlguard:")
        # 2) 私有地址：同样拒绝
        res2 = session.goto(page, "http://192.168.1.1/")
        assert res2.ok is False and res2.error.startswith("urlguard:")
        # 3) 请求守卫的判定函数（route handler 用的同一个）
        from pxb7.urlguard import classify_request_url
        assert classify_request_url("http://127.0.0.1/x")[0] is False
        assert classify_request_url("https://cdn.example.com/a.js")[0] is True
        assert session.guard.requests_blocked == 2
        assert page.url == "about:blank", "被拒 URL 不应产生任何导航"
    finally:
        session.stop()


def test_storage_state_load_failure_labels_guest(tmp_path: Path) -> None:
    """【回归】storage_state 载入失败 ⇒ 实际是游客上下文，collected_via 必须改标 guest
    （终审发现项 #9：此前沿用 login 标注，数据来源失真）。"""
    settings = cfg.load_settings()
    primary = tmp_path / "storage_primary.json"
    backup = tmp_path / "storage_backup.json"
    # 结构上「有 cookies」能通过 validate_storage_state，但 Playwright 无法载入
    primary.write_text('{"cookies": "not-a-list", "origins": []}', encoding="utf-8")
    backup.write_text('{"cookies": [{"name": "s", "value": "v"}], "origins": []}', encoding="utf-8")
    settings = dataclasses.replace(settings, paths=dataclasses.replace(
        settings.paths, storage_primary=primary, storage_backup=backup))
    session = B.BrowserSession(settings, headless=True)
    try:
        session.start()
    except B.BrowserUnavailable as exc:
        pytest.skip(f"本机无可用 chromium：{exc}")
    try:
        assert session.slot == B.SLOT_GUEST
        assert session.collected_via == B.COLLECTED_VIA_GUEST
        assert any("storage_state-load-failed" in d for d in session.downgrades)
    finally:
        session.stop()
