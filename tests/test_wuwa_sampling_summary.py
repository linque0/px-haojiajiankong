"""防止跨任务重复计数、错轮次或变价被当作已验收入库。"""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from prepare_wuwa_sampling_plan import specifications
from summarize_wuwa_sampling import summarize


def fixture(root, task_keys, count=51):
    plan = root / "registered-plan"
    plan.mkdir()
    tasks = [t for t in specifications() if t["task_id"] in task_keys]
    (plan / "采集任务登记.json").write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    (plan / "采样启动基线.csv").write_text("listing_id\naccount0\n", encoding="utf-8")
    for task_key in task_keys:
        folder = root / task_key
        folder.mkdir()
        members = [{"listing_id": f"account{i}", "model_eligible": True, "condition": True,
                    "status": "complete", "snapshot_round": "2026-10-05T18:30:00",
                    "listing_price": 100 + i} for i in range(count)]
        audit = {"task_id": task_key, "members": members, "selected_unique": count,
                 "detail_found": count, "list_run_id": task_key,
                 "list_collected_at": "2026-10-05T18:32:00", "ended_at": "2026-10-05T18:37:00"}
        rows = [{"listing_id": m["listing_id"], "price": m["listing_price"],
                 "round": "2026-10-05T18:30", "game_id": 10302, "cells": {"server": "官服"}}
                for m in members]
        (folder / "批次详情审计.json").write_text(json.dumps(audit), encoding="utf-8")
        (folder / "网关快照核对.json").write_text(json.dumps({"ok": True, "rows": rows}), encoding="utf-8")


def test_overlapping_tasks_are_not_new_unique_accounts(tmp_path):
    fixture(tmp_path, ["Y02", "X02"])
    result = summarize(tmp_path)
    ledger = json.loads((tmp_path / "registered-plan" / "采集任务登记.json").read_text(encoding="utf-8"))
    assert result["completed_tasks"] == 2
    assert result["unique"] == 51 and result["net_new"] == 50
    assert ledger["actual_new_memberships"] == 102
    with (tmp_path / "registered-plan" / "采集任务表.csv").open(encoding="utf-8-sig") as f:
        assert all(r["状态"] == "completed" and r["合格任务成员"] == "51" for r in csv.DictReader(f))


def test_wrong_round_and_changed_price_do_not_pass_minimum(tmp_path):
    fixture(tmp_path, ["Y02"])
    path = tmp_path / "Y02" / "网关快照核对.json"
    gateway = json.loads(path.read_text(encoding="utf-8"))
    gateway["rows"][0]["round"] = "2026-10-05T19:00"
    gateway["rows"][1]["price"] += 1
    path.write_text(json.dumps(gateway), encoding="utf-8")
    result = summarize(tmp_path)
    assert result["completed_tasks"] == 0 and result["unique"] == 49
    assert result["descriptions"][0]["状态"] == "below_minimum"
