"""离线审计一个详情采样批次，导出挂牌标签与条件成员；不请求站点或修改DB。"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pxb7 import extract as X, parser as P
from pxb7.gateway import _reject_risk_page
from prepare_wuwa_sampling_plan import matches, specifications
from wuwa_valuation import parse_row


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def v2_b01_specification():
    """V2-B01严格单批验收口径：官服鸣潮账号，不附加资产或价格条件，固定48条。"""
    return {"task_id": "V2-B01", "kind": "baseline", "ranges": {}, "exact": {},
            "note": "仅鸣潮账号、官服；全部资产/价格筛选留空",
            "game_id": 10302, "server": "官服", "mode": "detail", "sort": "actual",
            # A baseline may inspect extra candidates to replace incomplete details,
            # while the accepted sample itself remains exactly 48 unique accounts.
            "target": 48, "minimum": 48, "maximum": 100}


def v2_team_specifications():
    """V2-T01..T06：详情中必须明确拥有指定的全部三名队员，链数不限。"""
    teams = {
        "V2-T01": ("爱弥斯", "达妮娅", "千咲"),
        "V2-T02": ("清宵", "达妮娅", "莫宁"),
        "V2-T03": ("绯雪", "琳奈", "莫宁"),
        "V2-T04": ("爱弥斯", "琳奈", "莫宁"),
        "V2-T05": ("西格莉卡", "仇远", "守岸人"),
        "V2-T06": ("绯雪", "洛瑟菈", "穗穗"),
    }
    return [
        {"task_id": task_id, "kind": "full_team", "ranges": {},
         "exact": {"characters": {name: 0 for name in members}},
         "team_members": list(members), "game_id": 10302, "server": "官服",
         "mode": "detail", "sort": "actual", "target": 48,
         "minimum": 48, "maximum": 48}
        for task_id, members in teams.items()
    ]


def detail_row(card, title, attributes):
    features = X.extract_listing(X.seed_keywords(game_id=10302), listing_id=card.listing_id,
                                 title=title, card_fields=attributes).features
    def roster(key, suffix):
        return "、".join(f'{item["value"]}{suffix}{item["name"]}'
                        for item in features.get(key, []))
    row = {"listing_id": card.listing_id, "游戏": "鸣潮", "价格 ¥": card.fields.get("price_yuan"),
           "区服": card.fields.get("server", ""), "商品发布时间": card.fields.get("publish_time_text", ""),
           "等级": attributes.get("level"), "黄数": attributes.get("yellow_cnt"),
           "五星角色": attributes.get("five_star_chars"), "五星武器": attributes.get("five_star_weapons"),
           "共鸣链（N命）": roster("five_star_character_chains", "命"),
           "武器精炼（精N）": "、".join(f'精{i["value"]}{i["name"]}'
                for i in features.get("five_star_weapon_refinements", [])),
           "资源": "；".join(f'{label}：{features[key]}' for key, label in X.WUWA_RESOURCES if key in features),
           "额外付费商品": "；".join(f'{label}：' + "、".join(features[key])
                 for key, label in X.WUWA_PAID_ITEMS if features.get(key)),
           "TAP绑定": card.fields.get("tap_status", "")}
    return row


def audit(list_meta_path, raw_root, spec, ended_at):
    cutoff = dt.datetime.fromisoformat(ended_at)
    if cutoff.tzinfo is not None or cutoff > dt.datetime.now():
        raise ValueError("审计截止须为已经发生的本地时间，不能使用未来时间")
    meta = json.loads(list_meta_path.read_text(encoding="utf-8-sig"))
    if meta.get("task_id") != "wuwa_official" or meta.get("page_type") != "list" or not meta.get("ok"):
        raise ValueError("必须提供鸣潮成功列表批次元数据")
    list_html = list_meta_path.with_suffix(".html")
    html = list_html.read_text(encoding="utf-8")
    if _reject_risk_page(html):
        raise ValueError("列表含激活的验证/阻断页信号")
    parsed = P.parse_list_page(html, url=meta["url"])
    cards = {c.listing_id: c for c in parsed.cards if c.listing_id}
    if len(cards) > spec["maximum"]:
        raise ValueError(f'列表唯一商品{len(cards)}条，超过任务{spec["maximum"]}条上限；不自动截断')
    start = meta["collected_at"]
    if ended_at < start:
        raise ValueError("结束时间早于列表采集时间")
    candidates = {}
    for path in raw_root.rglob("detail_*.json"):
        item = json.loads(path.read_text(encoding="utf-8-sig"))
        listing_id = item.get("listing_id")
        stamp = item.get("collected_at", "")
        if listing_id not in cards or item.get("task_id") != "wuwa_official" or not start <= stamp <= ended_at:
            continue
        # 本任务必须回填本列表轮次；不能拿其他轮次或旧详情代替。
        # 列表round按半小时取整，与网关当前快照口径相同。
        minute = int(start[14:16]) // 30 * 30
        expected = start[:14] + f"{minute:02}:00"
        if item.get("snapshot_round") != expected:
            continue
        candidates.setdefault(listing_id, []).append((stamp, path, item))
    members, rows = [], []
    list_hash = sha(list_html)
    for listing_id, card in cards.items():
        entry = {"task_id": spec["task_id"], "listing_id": listing_id,
                 "list_run_id": meta["run_id"], "listing_price": card.fields.get("price_yuan"),
                 "list_collected_at": start, "list_sha256": list_hash,
                 "status": "detail_missing", "detail_file": "", "detail_sha256": "",
                 "condition": None, "model_eligible": False, "flags": []}
        choices = sorted(candidates.get(listing_id, []), key=lambda t: t[0])
        if choices:
            stamp, path, detail_meta = choices[0]
            detail_html = path.with_suffix(".html")
            data = detail_html.read_text(encoding="utf-8")
            entry.update(detail_file=str(detail_html.resolve()), detail_sha256=sha(detail_html),
                         detail_collected_at=stamp, snapshot_round=detail_meta["snapshot_round"])
            if _reject_risk_page(data):
                entry["status"] = "risk_page"
            else:
                title, attributes = P.parse_detail_attributes(data)
                row = detail_row(card, title, attributes)
                record = parse_row(row)
                entry.update(condition=matches(record, spec), model_eligible=record["eligible"],
                             flags=record["flags"], fingerprint=record["fingerprint"],
                             status="complete" if record["eligible"] else "detail_incomplete")
                rows.append(row)
        members.append(entry)
    qualified = [m for m in members if m["model_eligible"] and m["condition"] is True]
    report = {"task_id": spec["task_id"], "list_run_id": meta["run_id"],
              "source_url": meta["url"], "list_collected_at": start, "ended_at": ended_at,
              "selected_unique": len(cards), "detail_found": sum(bool(m["detail_file"]) for m in members),
              "complete_condition_verified": len(qualified),
              "status": "meets_sample_size" if spec["minimum"] <= len(qualified) <= spec["maximum"] else "below_minimum",
              "members": members, "note": "离线详情完整性/条件审计；入库确认须另核对网关快照。配队定义未核实时condition为空。"}
    return report, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-meta", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--task", choices=[*(t["task_id"] for t in specifications()),
                                            "V2-B01", *(t["task_id"] for t in v2_team_specifications())],
                        required=True)
    parser.add_argument("--ended-at", required=True, help="ISO本地时间，精确到秒")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec = (v2_b01_specification() if args.task == "V2-B01" else
            next((t for t in v2_team_specifications() if t["task_id"] == args.task),
                 next(t for t in specifications() if t["task_id"] == args.task)))
    report, rows = audit(args.list_meta, args.raw_root, spec, args.ended_at)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "批次详情审计.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if rows:
        with (args.output / "账号详情样本.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    print(json.dumps({k: v for k, v in report.items() if k != "members"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
