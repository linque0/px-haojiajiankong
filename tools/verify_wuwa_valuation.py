"""以标准库独立核对估值产物，不调用训练代码；生成交付核验记录。"""
import csv
import datetime
import hashlib
import json
import math
import statistics
from pathlib import Path

OUTPUT = Path(r"F:\deepseek workplace\pxb7-price-monitor\data\analysis\valuation\wuwa-20261005")
VISUAL = Path(r"C:\Users\linque\.codex\visualizations\2026\10\05\01a10b52-890d-7b31-9d9b-3cefbbed9bef")
LABELS = {"median": "价格中位数", "yellow": "仅黄数", "structure": "结构特征", "named": "名称细分"}


def load_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main():
    manifest = json.loads((OUTPUT / "manifest.json").read_text(encoding="utf8"))
    model = json.loads((OUTPUT / "model.json").read_text(encoding="utf8"))
    validation = json.loads((OUTPUT / "validation.json").read_text(encoding="utf8"))
    source = Path(manifest["source"])
    raw = {r["listing_id"]: r for r in load_csv(source)}
    results = load_csv(OUTPUT / "账号估值结果.csv")
    audit = {r["listing_id"]: r for r in load_csv(OUTPUT / "字段审计.csv")}
    checks = {}
    checks["source_sha256_unchanged"] = hashlib.sha256(source.read_bytes()).hexdigest() == manifest["sha256"]
    checks["result_rows_and_ids"] = len(results) == len(raw) == 294 and set(r["listing_id"] for r in results) == set(raw)
    eligible = [r for r in results if r["配置质量"] == "可建模"]
    checks["eligible_and_missing_estimates"] = len(eligible) == 285 and sum(r["分组留出估值_元"] == "" for r in results) == 9
    checks["original_prices_preserved"] = all(float(r["挂牌价_元"]) == float(raw[r["listing_id"]]["价格 ¥"]) for r in results)
    checks["yellow_reconciles_for_eligible"] = all(float(raw[r["listing_id"]]["黄数"]) == int(audit[r["listing_id"]]["重建黄数"]) for r in eligible)
    split_sets = [{audit[i]["配置指纹"] for i in model[key]} for key in ["train_ids", "calibration_ids", "test_ids"]]
    checks["configuration_groups_do_not_cross_splits"] = all(not split_sets[i] & split_sets[j] for i in range(3) for j in range(i))
    peers = load_csv(OUTPUT / "可比账号明细.csv")
    checks["peer_rows_and_no_self_or_same_configuration"] = len(peers) == 1425 and all(p["目标listing_id"] != p["可比listing_id"] and audit[p["目标listing_id"]]["配置指纹"] != audit[p["可比listing_id"]]["配置指纹"] for p in peers)
    checks["oof_bands_ordered"] = all(0 < float(r["经验参考带下限_元"]) < float(r["分组留出估值_元"]) < float(r["经验参考带上限_元"]) for r in eligible)
    checks["all_extrapolation_rows_diverted"] = sum(bool(r["模型支持标记"]) for r in results) == 5 and all(r["关系"] == "外推/新名称：人工估值" for r in results if r["模型支持标记"])
    # 用独立实现重放已保存线性模型：从原CSV和审计余额构造输入。
    forecasts, actuals = [], []
    for item_id in model["test_ids"]:
        row, a = raw[item_id], audit[item_id]
        chars = {token[2:]: int(token[0]) for token in row["共鸣链（N命）"].split("、")}
        weapons = [(token[2:], int(token[1])) for token in row["武器精炼（精N）"].split("、")]
        common_c = {"安可", "卡卡罗", "鉴心", "凌阳", "维里奈"}
        common_w = {"浩境粼光", "擎渊怒涛", "千古洑流", "漪澜浮录", "停驻之烟"}
        other_c = [n for name, n in chars.items() if name not in common_c]
        other_w = [n for name, n in weapons if name not in common_w]
        extras = {token.split("：", 1)[0]: token.split("：", 1)[1].split("、") for token in row["额外付费商品"].split("；") if "：" in token}
        x = {"yellow": float(row["黄数"]), "other_character_count": len(other_c), "other_chain_extra": sum(other_c),
             "other_chain_2plus": sum(n >= 2 for n in other_c), "other_chain_3plus": sum(n >= 3 for n in other_c),
             "other_chain_6": sum(n == 6 for n in other_c), "standard_character_count": sum(name in common_c for name in chars),
             "standard_chain_extra": sum(n for name, n in chars.items() if name in common_c), "other_weapon_count": len(other_w),
             "other_weapon_refine_extra": sum(n - 1 for n in other_w), "standard_weapon_count": sum(name in common_w for name, n in weapons),
             "standard_weapon_refine_extra": sum(n - 1 for name, n in weapons if name in common_w), "level": float(row["等级"]),
             "skin_count": len(extras.get("人物皮肤", [])), "vehicle_count": len(extras.get("车架模组", [])),
             "accessory_count": len(extras.get("摩托饰品", [])), "observed_flexible_pulls": (float(a["星声"] or 0) + float(a["月相"] or 0)) / 160,
             "observed_character_tickets": float(a["浮金波纹"] or 0), "observed_weapon_tickets": float(a["铸潮波纹"] or 0),
             "coral_pulls": float(a["余波珊瑚"] or 0) / 8,
             "verified_pair_count": int("嘉贝莉娜" in chars and any(name == "光影双生" for name, n in weapons)),
             "missing_extra_detail": int(not row["额外付费商品"])}
        for key in ["星声", "月相", "余波珊瑚", "浮金波纹", "铸潮波纹", "唤声涡纹"]:
            x["missing_resource:" + key] = int(a[key] == "")
        for name, n in chars.items():
            x["char_present:" + name] = 1
            x["char_chain:" + name] = n
        for name, n in weapons:
            x["weapon_copies:" + name] = x.get("weapon_copies:" + name, 0) + n
        linear_keys = {"level", "other_chain_extra", "standard_chain_extra", "other_chain_2plus", "other_chain_3plus", "other_chain_6"}
        logprice = model["intercept"]
        for i, key in enumerate(model["keys"]):
            value = x.get(key, 0)
            if key not in linear_keys and not key.startswith(("missing_", "char_present:", "char_chain:")):
                value = math.log1p(value)
            logprice += model["coef"][i] * (value - model["mean"][i]) / model["scale"][i]
        forecasts.append(math.exp(logprice))
        actuals.append(float(row["价格 ¥"]))
    independent = {"n": len(actuals), "mae_yuan": statistics.mean(abs(a - p) for a, p in zip(actuals, forecasts)),
                   "median_ape": statistics.median(abs(p / a - 1) for a, p in zip(actuals, forecasts)),
                   "coverage": statistics.mean(abs(math.log(a / p)) <= model["interval_log_radius"] for a, p in zip(actuals, forecasts))}
    checks["independent_test_metrics_match"] = all(abs(independent[k] - validation["heldout_metrics"][k]) < 1e-9 for k in independent)
    oof_actual, oof_pred = [float(r["挂牌价_元"]) for r in eligible], [float(r["分组留出估值_元"]) for r in eligible]
    checks["rounded_oof_metrics_match"] = abs(statistics.median(abs(p / a - 1) for a, p in zip(oof_actual, oof_pred)) - validation["oof_metrics"]["median_ape"]) < .0001
    if not all(checks.values()):
        raise AssertionError(checks)
    stamp = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
    result = {"checked_at": stamp, "checks": checks, "independently_recomputed_test": independent,
              "regression": {"collected": 362, "passed": 360, "skipped": 2, "new_tests_passed": 14,
                             "command": "project .venv python -X utf8 -m pytest -q --tb=short --basetemp task_owned_directory",
                             "note": "默认pytest临时目录读取被拒；改为工作树内专属临时目录后全量通过。"},
              "not_verified": ["实际成交与价格趋势", "真实交接与违规状态", "全部实体与专武归属", "跨卖家和跨时间泛化"]}
    (OUTPUT / "验收记录.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    # 源资料CSV与字段规范在Excel中使用UTF-8 BOM，所有其它模型产物已由主程序带BOM输出。
    for name in ["来源登记.csv", "估值字段规范.csv"]:
        content = (OUTPUT / name).read_text(encoding="utf-8-sig")
        (OUTPUT / name).write_text(content, encoding="utf-8-sig")
    VISUAL.mkdir(parents=True, exist_ok=True)
    comparison = load_csv(OUTPUT / "模型对比.csv")
    reviewed_rows = [{"model": LABELS[r["family"]], "medianApePct": round(float(r["median_ape"]) * 100, 2), "testN": int(r["n"])} for r in comparison]
    chart_source = {"label": "鸣潮挂牌估值验证", "files": [{"label": "模型对比.csv"}, {"label": "validation.json"}],
                    "caveats": ["独立测试56个有效官服账号；目标为挂牌价，非成交价。", "模型与参数只在训练集合内部选择。"],
                    "filters": ["游戏：鸣潮", "区服：官服", "字段审计通过", "分组留出测试集合：56个账号"]}
    chart = {"schemaVersion": 1, "id": "wuwa-validation", "title": "鸣潮挂牌估值：独立测试误差",
             "description": "中位绝对百分比误差，越低越好；56个测试账号。",
             "chart": {"type": "bar", "x": "model", "y": "medianApePct", "yLabel": "中位绝对误差（%）", "showXAxisLabel": False},
             "rows": reviewed_rows, "columns": ["model", "medianApePct", "testN"],
             "source": chart_source, "theme": "codex-classic"}
    receipt = {"schemaVersion": 1, "items": [{"id": "wuwa-validation", "title": "独立测试模型比较", "queries": [
        {"id": "holdout-results", "source": {**chart_source, "metricDefinitions": [{"name": "中位绝对百分比误差", "definition": "中位绝对百分比误差取56个测试账号的 abs(预测挂牌价/实际挂牌价−1) 中位数。"}]},
         "rows": reviewed_rows, "columns": chart["columns"], "preview": {"kind": "aggregate", "note": "四类模型在同一56个测试账号上的聚合结果。", "totalRows": 4}}]}]}
    additional = {"schemaVersion": 1, "items": [{"id": "wuwa-framework", "title": "估值维度与适用边界", "queries": [
        {"id": "source-data", "summary": "294个账号中285个通过字段与区服审查；本次输出挂牌估值，交接与养成条件未提供。", "source": {"label": "鸣潮当前账号CSV", "files": [{"label": source.name}, {"label": "字段审计.csv"}], "caveats": ["一次导出不能证明成交或趋势。"]}},
        {"id": "market-framework", "summary": "鸣潮、原神、星铁和绝区零原始估价报告用于比较资产拆分、养成与账号条件的结构；各游戏人民币单价未迁移。", "source": {"label": "原始估价报告", "links": [
            {"label": "鸣潮报告101702", "url": "https://m.kejinshou.com/report/high/d_101702"},
            {"label": "原神报告28153585", "url": "https://m.kejinshou.com/report/high/d_28153585"},
            {"label": "星铁报告2143574", "url": "https://www.kejinshou.com/report/normal/d_2143574"},
            {"label": "绝区零报告89016", "url": "https://m.kejinshou.com/report/high/d_89016"}], "caveats": ["平台估价报告不是成交证明，历史金额不用于当前模型。"]}}]}]}
    for name, payload in [("wuwa-validation.json", chart), ("wuwa-validation-sources.json", receipt), ("wuwa-framework-sources.json", additional)]:
        (VISUAL / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
