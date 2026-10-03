"""前置闸门拒绝的那一轮必须「什么都不做、什么都不虚报」（gate 回归）。

背景：2026-10-02 22:58 真实站点 WAF 滑块触发 24h 退避后，后续 crawl 被前置闸门拒绝。
当时暴露三个如实性缺陷，本文件把它们钉住：
  1) 仍会启动 chromium（本轮 0 请求，却做了无意义的浏览器启动）；
  2) summary.raw_dir 指向一个从未创建的目录（虚报落盘）；
  3) CLI 打印全零「字段命中明细」表（看起来像解析失败，实际是根本没取页面）。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pxb7 import cli  # noqa: E402
from pxb7 import config as cfg  # noqa: E402
from pxb7 import pipeline as PL  # noqa: E402
from pxb7 import risk as R  # noqa: E402


def _tmp_settings_file(tmp_path: Path) -> Path:
    """基于真实 settings.yaml 派生一份路径全指向 tmp 的副本（不碰仓库内真实数据）。"""
    raw = dict(cfg.load_settings().raw)
    state_dir = tmp_path / "state"
    raw["paths"] = {
        "db": str(tmp_path / "t.duckdb"),
        "raw_root": str(tmp_path / "raw"),
        "state_dir": str(state_dir),
        "storage_primary": str(state_dir / "storage_primary.json"),
        "storage_backup": str(state_dir / "storage_backup.json"),
        "risk_state": str(state_dir / "risk_state.json"),
        "log_dir": str(tmp_path / "logs"),
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "settings.yaml"
    target.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return target


def _write_paused_state(path: Path, *, hours: float = 1.0, legacy_v1: bool = False) -> dt.datetime:
    """写入 task_paused 状态（模拟 WAF 触发后的 24h 退避中）。

    legacy_v1=True 时写成旧的顶层平铺格式，用于验证 v1→v2 迁移仍能拦住该任务。
    """
    backoff_until = dt.datetime.now().replace(microsecond=0) + dt.timedelta(hours=hours)
    now = dt.datetime.now().isoformat(timespec="seconds")
    entry = {
        "level": "task_paused", "reason": "captcha",
        "backoff_until": backoff_until.isoformat(timespec="seconds"),
        "consecutive": 1, "probe_mode": False, "frequency_factor": 4.0,
        "raw_only": False, "updated_at": now,
    }
    payload = (dict(entry, task_id="genshin_official", session_slot="guest",
                    last_trigger=now, updated_at=now, alerts=[], history=[],
                    schema_version="v1")
               if legacy_v1 else
               {"schema_version": "v2", "level": "normal", "reason": None,
                "backoff_until": None, "task_id": "genshin_official",
                "session_slot": "guest", "frequency_factor": 4.0, "probe_mode": False,
                "raw_only": False, "consecutive": 1, "last_trigger": now, "updated_at": now,
                "alerts": [], "history": [], "tasks": {"genshin_official": entry}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return backoff_until


def test_refused_round_makes_no_browser_no_raw_no_fake_table(tmp_path, monkeypatch, capsys) -> None:
    settings_file = _tmp_settings_file(tmp_path)
    risk_state = tmp_path / "state" / "risk_state.json"
    backoff_until = _write_paused_state(risk_state)
    summary_path = tmp_path / "runs" / "refused.json"

    # 断言：拒绝的那一轮绝不启动浏览器
    import pxb7.browser as B

    def _boom(self):
        raise AssertionError("前置闸门拒绝时不应启动 chromium")

    monkeypatch.setattr(B.BrowserSession, "start", _boom)

    exit_code = cli.main(["crawl", "--settings", str(settings_file),
                          "--task", "genshin_official",
                          "--summary-json", str(summary_path)])
    out, err = capsys.readouterr()

    assert exit_code == cli.EXIT_RISK_ABORT == 21
    # 1) 不打印全零字段表，且明说没有页面
    assert "字段命中明细" not in out
    assert "本轮未获取到任何页面" in out
    # 2) 不虚报 raw 目录
    assert "（本轮未落盘 raw）" in out
    assert not (tmp_path / "raw").exists(), "拒绝轮次不得创建 raw 目录"
    # 3) 明确说明未发起任何请求
    assert "前置闸门拦截" in err and "未发起任何请求" in err
    assert "captcha" in err and backoff_until.isoformat(timespec="seconds") in err

    # 4) summary 契约键齐全且值诚实
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert set(summary) == set(PL.SUMMARY_KEYS)
    assert summary["status"] == "aborted"
    assert summary["risk_trigger"] == "captcha", "risk_trigger 记触发原因，不是状态级别"
    assert summary["raw_dir"] is None
    assert summary["pages"] == 0 and summary["cards_seen"] == 0
    assert summary["parse_success_rate"] == 0.0 and summary["extract_hit_rate"] == 0.0
    assert summary["snapshots_inserted"] == 0 and summary["new_listings"] == 0


def test_legacy_v1_state_still_blocks_its_task(tmp_path) -> None:
    """旧版（顶层平铺）risk_state.json 读取后必须迁移，仍拦住原任务、且不外溢。"""
    settings_file = _tmp_settings_file(tmp_path)
    settings = cfg.load_settings(settings_file)
    _write_paused_state(settings.paths.risk_state, hours=2.0, legacy_v1=True)

    machine = R.RiskMachine(settings, state_path=settings.paths.risk_state)
    assert machine.state.schema_version == R.STATE_SCHEMA_VERSION
    assert "genshin_official" in machine.state.tasks
    assert machine.check_before_run("genshin_official").allow is False
    assert machine.check_before_run("another_task").allow is True


def test_refused_round_reports_risk_detail(tmp_path) -> None:
    """PipelineResult.risk_detail 必须给出 level/backoff_until/剩余秒数，供排障说明原因。"""
    settings_file = _tmp_settings_file(tmp_path)
    settings = cfg.load_settings(settings_file)
    _write_paused_state(settings.paths.risk_state, hours=3.0)
    task = cfg.load_tasks(settings=settings).by_id("genshin_official")
    machine = R.RiskMachine(settings, state_path=settings.paths.risk_state)
    result = PL.run_once(settings=settings, task=task, session=object(), machine=machine,
                         keywords=[], run_id="refused", notify_result=False)
    detail = result.risk_detail
    assert detail is not None
    assert detail["level"] == "task_paused" and detail["reason"] == "captcha"
    assert detail["requests_issued"] is False
    assert detail["backoff_remaining_s"] > 0
    assert result.summary["raw_dir"] is None
