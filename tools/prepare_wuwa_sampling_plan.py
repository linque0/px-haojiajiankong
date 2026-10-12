"""离线生成鸣潮条件采样任务台账与旧样本覆盖诊断；不访问网站、不写采集库。"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path

from wuwa_valuation import read_rows


def specifications():
    tasks = []

    def add(task_id, kind, ranges=None, exact=None, note=""):
        tasks.append({"task_id": task_id, "kind": kind, "ranges": ranges or {},
                      "exact": exact or {}, "note": note, "game_id": 10302,
                      "server": "官服", "mode": "detail", "sort": "最新发布",
                      "target": 64, "minimum": 50, "maximum": 100,
                      "status": "not_started", "collected_unique": 0,
                      "detail_complete": 0, "condition_verified": 0,
                      "model_eligible": 0, "run_ids": [], "members": []})

    add("B00", "baseline")
    for prefix, field, bands in [
        ("P", "价格 ¥", [(0, 299.99), (300, 999.99), (1000, None)]),
        ("Y", "黄数", [(0, 39), (40, 79), (80, 99), (100, None)]),
        ("C", "五星角色", [(0, 14), (15, 24), (25, None)]),
        ("L", "等级", [(0, 69), (70, 79), (80, 80)]),
    ]:
        for i, band in enumerate(bands, 1):
            add(f"{prefix}{i:02}", "single", {field: list(band)})
    for i, (field, minimum) in enumerate([
        ("星声", 6400), ("浮金波纹", 20), ("铸潮波纹", 20),
        ("月相", 160), ("余波珊瑚", 80),
    ], 1):
        add(f"R{i:02}", "single", {field: [minimum, None]})
    for i, exact in enumerate([
        {"characters": {"守岸人": 0}}, {"characters": {"爱弥斯": 6}},
        {"weapons": ["光影双生"]}, {"teams": ["爱达千"]},
        {"skins": ["桃夭灼灼"]}, {"vehicles": ["云帛机骑"]},
    ], 1):
        add(f"E{i:02}", "exact_single", exact=exact,
            note="待现场确认选项/档位与筛选语义；characters值为最低链数")
    add("X01", "combined", {"黄数": [40, 79], "星声": [6400, None]})
    add("X02", "combined", {"黄数": [40, 79]}, {"characters": {"爱弥斯": 0}})
    add("X03", "combined", exact={"characters": {"嘉贝莉娜": 0}, "weapons": ["光影双生"]})
    add("X04", "combined", exact={"characters": {"爱弥斯": 0}, "teams": ["爱达千"]})
    add("X05", "combined", exact={"skins": ["桃夭灼灼"], "vehicles": ["云帛机骑"]})
    add("X06", "combined", {"黄数": [0, 39], "等级": [80, 80]})
    return tasks


def matches(record, spec):
    """三值核对：未知不能当作满足或不满足；配队未有可核验定义则未知。"""
    unknown = False
    if record["raw"].get("区服") != spec["server"]:
        return False
    for field, (lo, hi) in spec["ranges"].items():
        if field == "价格 ¥":
            value = record["price"]
        elif field in ("黄数", "等级", "五星角色"):
            text = record["raw"].get(field)
            value = float(text) if text not in (None, "") else None
        else:
            value = record["resources"].get(field)
        if value is None:
            unknown = True
        elif (lo is not None and value < lo) or (hi is not None and value > hi):
            return False
    for name, chain in spec["exact"].get("characters", {}).items():
        if not record["chars"]:
            unknown = True
        elif name not in record["chars"] or record["chars"][name] < chain:
            return False
    for name in spec["exact"].get("weapons", []):
        if not record["weapons"]:
            unknown = True
        elif name not in {n for n, _ in record["weapons"]}:
            return False
    # 完整名单没有列示外观时仍为未知：未列示不等于明确没有。
    extras = record["raw"].get("额外付费商品") or ""
    for key, label in [("skins", "人物皮肤"), ("vehicles", "车架模组")]:
        for name in spec["exact"].get(key, []):
            segments = [s for s in extras.split("；") if s.startswith(label + "：")]
            if not segments:
                unknown = True
            else:
                names = segments[0].split("：", 1)[1].split("、")
                if name not in names:
                    return False
    if spec["exact"].get("teams"):
        unknown = True
    return None if unknown else True


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--paused-task", choices=[t["task_id"] for t in specifications()])
    args = parser.parse_args()
    tasks = specifications()
    assert len(tasks) == len({t["task_id"] for t in tasks}) == 31
    assert all(50 <= t["target"] <= 100 for t in tasks)
    args.output.mkdir(parents=True, exist_ok=True)
    # 保护已经登记的真实任务成员；计划只能生成到新目录。
    manifest_path = args.output / "采集任务登记.json"
    if manifest_path.exists():
        raise SystemExit("任务登记已存在；请选择新输出目录，避免覆盖真实进度")
    source_bytes = args.input.read_bytes()
    frozen_input = args.output / "采样启动基线.csv"
    frozen_input.write_bytes(source_bytes)
    records = read_rows(frozen_input)
    coverage = []
    for task in tasks:
        if task["task_id"] == args.paused_task:
            task["status"] = "paused_verification"
            task["note"] += "；现场出现访问验证，网关拒收，合格采集0条"
        result = [matches(r, task) for r in records]
        coverage.append({"task_id": task["task_id"], "旧CSV满足条件": sum(v is True for v in result),
                         "旧CSV条件未知": sum(v is None for v in result),
                         "旧CSV满足且原模型合格": sum(v is True and r["eligible"] for r, v in zip(records, result)),
                         "本轮新增": 0, "说明": "仅旧样本覆盖诊断，不计入本轮50–100条验收"})
    manifest = {"created_at": dt.datetime.now().isoformat(timespec="seconds"),
                "purpose": "挂牌价条件采样研究", "status": "paused_verification" if args.paused_task else "planned",
                "source_csv": str(args.input.resolve()),
                "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "frozen_input": str(frozen_input.resolve()), "baseline_rows": len(records),
                "planned_memberships": sum(t["target"] for t in tasks),
                "actual_new_memberships": 0, "tasks": tasks,
                "note": "研究任务独立于wuwa_official网关路由；规划数量不是已采集数量"}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(args.output / "采集任务表.csv", [
        {"task_id": t["task_id"], "类型": t["kind"], "区间": json.dumps(t["ranges"], ensure_ascii=False),
         "精确条件": json.dumps(t["exact"], ensure_ascii=False), "目标": t["target"],
         "下限": t["minimum"], "上限": t["maximum"], "状态": t["status"], "合格新增": 0}
        for t in tasks])
    write_csv(args.output / "旧样本条件覆盖.csv", coverage)
    print(json.dumps({"tasks": len(tasks), "planned_memberships": 1984,
                      "actual_new_memberships": 0, "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
