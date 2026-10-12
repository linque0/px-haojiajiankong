"""从已审计、已核对入库的任务生成进度与价格描述；不访问网站。"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import statistics
from pathlib import Path

from prepare_wuwa_sampling_plan import write_csv


def percentile(values, p):
    values = sorted(values)
    position = (len(values) - 1) * p
    lo = int(position)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (position - lo)


def summarize(root):
    plan_dir = root / "registered-plan"
    ledger_path = plan_dir / "采集任务登记.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8-sig"))
    with (plan_dir / "采样启动基线.csv").open(encoding="utf-8-sig", newline="") as f:
        baseline_ids = {r["listing_id"] for r in csv.DictReader(f)}
    all_ids, descriptions = set(), []
    for task in ledger["tasks"]:
        folder = root / task["task_id"]
        audit_path, gateway_path = folder / "批次详情审计.json", folder / "网关快照核对.json"
        if not audit_path.exists() or not gateway_path.exists():
            continue
        report = json.loads(audit_path.read_text(encoding="utf-8-sig"))
        gateway = json.loads(gateway_path.read_text(encoding="utf-8-sig"))
        if report["task_id"] != task["task_id"] or not gateway.get("ok"):
            raise ValueError(f'{task["task_id"]}: 审计任务/网关响应不匹配')
        rows = gateway["rows"]
        verified = []
        eligible = [m for m in report["members"] if m["model_eligible"] and m["condition"] is True]
        for m in eligible:
            # 必须保留列表当时挂牌价，不能用后来改变的价格替换。
            match = [r for r in rows if r["listing_id"] == m["listing_id"]
                     and r["round"][:16] == m["snapshot_round"][:16]
                     and r["price"] == m["listing_price"] and r["game_id"] == 10302
                     and r["cells"].get("server") == "官服"]
            if match:
                verified.append(m)
        ids = {m["listing_id"] for m in verified}
        complete = task["minimum"] <= len(ids) <= task["maximum"]
        task.update(status="completed" if complete else "below_minimum",
                    collected_unique=report["selected_unique"], detail_found=report["detail_found"],
                    detail_complete=sum(m["status"] == "complete" for m in report["members"]),
                    condition_verified=len(eligible), model_eligible=len(ids),
                    run_ids=[report["list_run_id"]], members=sorted(ids),
                    started_at=report["list_collected_at"], ended_at=report["ended_at"])
        all_ids.update(ids)
        if not ids:
            continue
        prices = [m["listing_price"] for m in verified]
        acceptance = {"task_id": task["task_id"], "unique": report["selected_unique"],
                      "qualified": len(eligible), "gateway_price_round_verified": len(ids),
                      "new_ids_vs_frozen_318": len(ids - baseline_ids),
                      "price_min": min(prices), "price_median": statistics.median(prices),
                      "price_max": max(prices), "price_mean": statistics.mean(prices),
                      "status": "complete" if complete else "below_minimum"}
        (folder / "验收记录.json").write_text(json.dumps(acceptance, ensure_ascii=False, indent=2), encoding="utf-8")
        descriptions.append({"task_id": task["task_id"], "合格账号": len(ids),
                             "挂牌价P25": percentile(prices, .25), "挂牌价中位数": statistics.median(prices),
                             "挂牌价P75": percentile(prices, .75), "挂牌价均值": statistics.mean(prices),
                             "最低挂牌价": min(prices), "最高挂牌价": max(prices),
                             "状态": task["status"], "排序": task["sort"],
                             "说明": "筛选样本的描述性分布；不能据此认定独立加价或误差减少"})
    ledger.update(actual_new_memberships=sum(t["model_eligible"] for t in ledger["tasks"]),
                  collected_unique_across_tasks=len(all_ids), net_new_ids=len(all_ids - baseline_ids),
                  updated_at=dt.datetime.now().isoformat(timespec="seconds"))
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(plan_dir / "采集任务表.csv", [{"task_id": t["task_id"], "类型": t["kind"],
              "区间": json.dumps(t["ranges"], ensure_ascii=False), "精确条件": json.dumps(t["exact"], ensure_ascii=False),
              "目标": t["target"], "下限": t["minimum"], "上限": t["maximum"],
              "状态": t["status"], "合格任务成员": t["model_eligible"]} for t in ledger["tasks"]])
    if descriptions:
        write_csv(root / "已验收任务挂牌价分布.csv", descriptions)
    return {"completed_tasks": sum(t["status"] == "completed" for t in ledger["tasks"]),
            "unique": len(all_ids), "net_new": len(all_ids - baseline_ids), "descriptions": descriptions}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(summarize(args.root), ensure_ascii=False))
