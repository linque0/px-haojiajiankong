"""CLI 子命令与通知模块测试（离线：不启动浏览器、不访问网络）。"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pxb7 import cli  # noqa: E402
from pxb7 import config as cfg  # noqa: E402
from pxb7 import db  # noqa: E402
from pxb7 import notify as N  # noqa: E402
from pxb7 import pipeline as PL  # noqa: E402

LIST_HTML = """<html><body><ul class='product-list'>
<li class="product-card" data-listing-id="1000000001">
  <div class="card-title">原神 官服 满命 精5 原石 32000</div>
  <div class="card-price">¥ 12000</div>
  <div class="card-time">21分钟内发布</div>
  <ul>
    <li class="attr-level">等级 60</li><li class="attr-yellow">黄数 45</li>
    <li class="attr-star-char">五星角色 12</li><li class="attr-star-weapon">五星武器 5</li>
    <li class="attr-server">官服</li><li class="attr-mail">邮箱出售</li>
  </ul>
</li></ul></body></html>"""


@pytest.fixture()
def settings(tmp_path: Path) -> cfg.Settings:
    base = cfg.load_settings()
    return dataclasses.replace(base, paths=dataclasses.replace(
        base.paths, db=tmp_path / "cli.duckdb", raw_root=tmp_path / "raw",
        state_dir=tmp_path / "state", runs=tmp_path / "runs",
        risk_state=tmp_path / "risk.json"))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_init_db_then_status(settings, tmp_path, capsys) -> None:
    assert cli.main(["init-db", "--db", str(settings.paths.db)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "词表种子" in out and "dim_game" in out
    # 词表已入库
    conn = db.connect(settings.paths.db, read_only=True)
    try:
        total, enabled = conn.execute(
            "SELECT count(*), sum(CASE WHEN enabled THEN 1 ELSE 0 END) FROM dim_keyword"
        ).fetchone()
    finally:
        conn.close()
    assert total == 75 and enabled == 74, \
        "75 条种子（原神 18 + 鸣潮 25 + 三角洲 32）中 1 条为 disabled 占位"

    assert cli.main(["status", "--db", str(settings.paths.db)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "风控状态" in out and "词表" in out

    assert cli.main(["status", "--db", str(settings.paths.db), "--json"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["tables"]["dim_keyword"] == 75, "原神 18 + 鸣潮 25 + 三角洲 32（docs/02 §4）"
    # risk_state 是机器本地状态（真实冒烟会把它推进到 task_paused/backoff）；
    # 这里只断言字段契约，不断言具体 level，保证测试不依赖外部状态。
    for key in ("level", "reason", "backoff_until", "last_trigger", "consecutive"):
        assert key in payload["risk_state"]
    assert payload["risk_state"]["level"] in {
        "normal", "warn", "backoff", "task_paused", "stopped"}


def test_cli_parse_raw_replays_offline(settings, tmp_path, capsys) -> None:
    db.init_db(settings.paths.db)
    run_dir = settings.paths.raw_root / "genshin_official" / "20261002" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "list_p01.html").write_text(LIST_HTML, encoding="utf-8")
    (run_dir / "list_p01.json").write_text(json.dumps({
        "url": "https://www.pxb7.com/buy/10026/1", "page_type": "list", "page_no": 1,
        "collected_at": "2026-10-02T10:07:00", "http_status": 200, "ok": True,
        "collected_via": "guest", "parser_version": "v0.1.0"}, ensure_ascii=False),
        encoding="utf-8")

    exit_code = cli.main(["parse-raw", "--db", str(settings.paths.db),
                          "--raw-root", str(settings.paths.raw_root),
                          "--run-dir", str(run_dir), "--task", "genshin_official"])
    assert exit_code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "cards_seen=1" in out and "snapshots=1" in out

    summary = json.loads((run_dir / "summary.replay.json").read_text(encoding="utf-8"))
    assert set(summary) == set(PL.SUMMARY_KEYS)
    assert summary["snapshots_inserted"] == 1 and summary["cards_parsed"] == 1
    assert summary["delist_events"] == 0, "重放默认不推断下架"
    assert summary["risk_trigger"] is None

    conn = db.connect(settings.paths.db, read_only=True)
    try:
        assert conn.execute("SELECT count(*) FROM fct_listing_snapshot").fetchone()[0] == 1
        features = conn.execute(
            "SELECT extracted_features FROM fct_listing_snapshot").fetchone()[0]
        assert json.loads(features)["constellation_cnt"] == 6
        assert conn.execute("SELECT count(*) FROM fct_listing_keyword").fetchone()[0] >= 1
    finally:
        conn.close()

    # 重放幂等：再跑一次不产生重复
    assert cli.main(["parse-raw", "--db", str(settings.paths.db),
                     "--run-dir", str(run_dir)]) == cli.EXIT_OK
    conn = db.connect(settings.paths.db, read_only=True)
    try:
        assert conn.execute("SELECT count(*) FROM fct_listing_snapshot").fetchone()[0] == 1
    finally:
        conn.close()


def test_cli_parse_raw_without_raw_dir_returns_error(settings, capsys) -> None:
    db.init_db(settings.paths.db)
    assert cli.main(["parse-raw", "--db", str(settings.paths.db),
                     "--raw-root", str(settings.paths.raw_root)]) == cli.EXIT_ERROR
    assert "没有找到 raw run 目录" in capsys.readouterr().err


def test_cli_crawl_maps_risk_abort_to_exit_21(settings, monkeypatch, capsys) -> None:
    """crawl 退出码契约：0 完成 / 21 风控终止 / 1 错误。"""
    aborted = PL.PipelineResult(summary={
        **{k: None for k in PL.SUMMARY_KEYS},
        "run_id": "r-x", "task_id": "genshin_official", "status": "aborted",
        "collected_via": "guest", "pages": 0, "cards_seen": 0, "cards_parsed": 0,
        "parse_success_rate": 0.0, "extract_hit_rate": 0.0, "snapshots_inserted": 0,
        "new_listings": 0, "price_changes": 0, "delist_events": 0, "detail_fetched": 0,
        "risk_trigger": "task_paused", "raw_dir": None, "duration_s": 0.1,
    })

    captured: dict = {}

    def fake_run_once(**kwargs):
        captured.update(kwargs)
        return aborted

    monkeypatch.setattr(PL, "run_once", fake_run_once)
    exit_code = cli.main(["crawl", "--task", "genshin_official", "--pages", "2",
                          "--detail", "1", "--guest", "--db", str(settings.paths.db)])
    assert exit_code == cli.EXIT_RISK_ABORT == 21
    assert captured["pages"] == 2 and captured["detail"] == 1 and captured["mode"] == "guest"
    err = capsys.readouterr().err
    assert "风控触发" in err


def test_cli_crawl_maps_success_to_exit_0(settings, monkeypatch) -> None:
    ok = PL.PipelineResult(summary={
        **{k: None for k in PL.SUMMARY_KEYS},
        "run_id": "r-y", "task_id": "genshin_official", "status": "completed",
        "collected_via": "guest", "pages": 1, "cards_seen": 3, "cards_parsed": 3,
        "parse_success_rate": 1.0, "extract_hit_rate": 0.67, "snapshots_inserted": 3,
        "new_listings": 3, "price_changes": 0, "delist_events": 0, "detail_fetched": 0,
        "risk_trigger": None, "raw_dir": "x", "duration_s": 12.0,
    })
    monkeypatch.setattr(PL, "run_once", lambda **kw: ok)
    assert cli.main(["crawl", "--db", str(settings.paths.db)]) == cli.EXIT_OK


def test_cli_rejects_unknown_task(settings, capsys) -> None:
    assert cli.main(["crawl", "--task", "nope", "--db", str(settings.paths.db)]) == cli.EXIT_ERROR
    assert "未知标识" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 通知（凭据只从环境变量；配置为空则静默跳过）
# --------------------------------------------------------------------------- #
class FakeResponse:
    status = 200

    def getcode(self) -> int:
        return 200

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_notify_skips_silently_when_unconfigured(settings, monkeypatch) -> None:
    monkeypatch.delenv("PXB7_NOTIFY_OPS", raising=False)
    monkeypatch.delenv("PXB7_NOTIFY_WEBHOOK", raising=False)
    result = N.send_text("hello", settings=settings)
    assert result.sent is False
    assert "未配置" in (result.skipped_reason or "")
    assert settings.notify.get("webhook_url") == "", "settings 里的 webhook 必须保持为空"


def test_notify_guard_blocks_private_webhook(settings, monkeypatch) -> None:
    monkeypatch.setenv("PXB7_NOTIFY_OPS", "http://127.0.0.1:9000/hook")
    result = N.send_text("hello", settings=settings)
    assert result.sent is False
    assert "URL 守卫" in (result.skipped_reason or "")

    monkeypatch.setenv("PXB7_NOTIFY_OPS", "file:///C:/hook")
    assert N.send_text("hello", settings=settings).sent is False


def test_notify_posts_json_payload(settings, monkeypatch) -> None:
    monkeypatch.setenv("PXB7_NOTIFY_OPS", "https://open.feishu.cn/open-apis/bot/v2/hook/placeholder")
    sent: list = []

    def opener(request, timeout=None):
        sent.append((request, timeout))
        return FakeResponse()

    result = N.send_text("采集异常：解析率 0.5", settings=settings, opener=opener)
    assert result.sent is True and result.status == 200
    request, _timeout = sent[0]
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["msg_type"] == "text" and "采集异常" in payload["content"]["text"]
    assert request.get_header("Content-type", "").startswith("application/json")


def test_notify_health_only_when_problem(settings, monkeypatch) -> None:
    monkeypatch.delenv("PXB7_NOTIFY_OPS", raising=False)
    clean = {"run_id": "r", "task_id": "t", "status": "completed", "pages": 2,
             "cards_seen": 10, "cards_parsed": 10, "parse_success_rate": 1.0,
             "extract_hit_rate": 0.8, "snapshots_inserted": 10, "new_listings": 1,
             "price_changes": 0, "delist_events": 0, "collected_via": "guest",
             "risk_trigger": None}
    skipped = N.notify_health(clean, settings=settings)
    assert skipped.sent is False and "无异常" in (skipped.skipped_reason or "")

    text = N.format_health_alert({**clean, "status": "aborted", "risk_trigger": "captcha"},
                                 [{"kind": "captcha-stop", "message": "连续 2 次验证码，已停采"}])
    assert "captcha" in text and "连续 2 次验证码" in text


def test_no_credential_literals_in_repo_config() -> None:
    """源码/示例/测试不得写入可用凭据字面量（webhook 只从环境变量读）。"""
    for rel in ("pxb7/notify.py", "config/settings.yaml", "pxb7/cli.py"):
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        assert "open-apis/bot" not in text, f"{rel} 不得内置可用 webhook"
        assert "hooks.slack.com" not in text
        assert "oapi.dingtalk.com/robot/send?access_token=" not in text
