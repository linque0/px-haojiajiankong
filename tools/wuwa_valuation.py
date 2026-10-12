"""鸣潮挂牌估值研究：离线 CSV 审计、分组验证、可解释岭回归。

训练依赖 numpy；字段解析、审计和预测仅使用 Python 标准库。
本工具不访问站点，不写数据库，不把挂牌价解释为成交价值。
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import hashlib
import json
import math
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from pxb7 import exploration as E

MAP_FEATURES = ["map_remaining_astrite_pulls",
                "map_mean_progress", "map_progress_coverage", "map_astrite_coverage",
                "missing_map_progress", *["missing_map_resource:" + k for k in E.RESOURCE_LABELS]]

STANDARD_CHARACTERS = {"安可", "卡卡罗", "鉴心", "凌阳", "维里奈"}
STANDARD_WEAPONS = {"浩境粼光", "擎渊怒涛", "千古洑流", "漪澜浮录", "停驻之烟"}
# 最小已核对配对；其余不凭同时出现猜配对，也不将此集合视为全游戏名单。
VERIFIED_PAIRS = {"嘉贝莉娜": "光影双生"}
RESOURCE_KEYS = ["星声", "月相", "余波珊瑚", "浮金波纹", "铸潮波纹", "唤声涡纹"]
ASSET_COLUMNS = ["共鸣链（N命）", "武器精炼（精N）", "资源", "额外付费商品"]
NUMERIC_COLUMNS = ["等级", "黄数", "五星角色", "五星武器"]
REQUIRED_COLUMNS = {"listing_id", "游戏", "价格 ¥", *ASSET_COLUMNS, *NUMERIC_COLUMNS, "区服", "商品发布时间"}
CORE_FEATURES = [
    "other_character_count", "other_chain_extra", "other_chain_2plus",
    "other_chain_3plus", "other_chain_6", "standard_character_count",
    "standard_chain_extra", "other_weapon_count", "other_weapon_refine_extra",
    "standard_weapon_count", "standard_weapon_refine_extra", "level",
    "skin_count", "vehicle_count", "accessory_count", "observed_flexible_pulls",
    "observed_character_tickets", "observed_weapon_tickets", "coral_pulls",
    "verified_pair_count",
]
SUPPORT_FEATURES = ["other_chain_6", "other_chain_extra", "other_weapon_refine_extra", "observed_flexible_pulls"]
FEATURE_LABELS = {
    "map_remaining_astrite_pulls": "地图预计剩余星声抽数（大致估算）",
    "map_mean_progress": "已列示地图平均探索度",
    "map_progress_coverage": "地图探索度覆盖率", "map_astrite_coverage": "地图星声统计覆盖率",
    "yellow": "黄数（仅基准）", "other_character_count": "非基础常驻角色数",
    "other_chain_extra": "非基础常驻追加链数", "other_chain_2plus": "非基础常驻≥2链人数",
    "other_chain_3plus": "非基础常驻≥3链人数", "other_chain_6": "非基础常驻6链人数",
    "standard_character_count": "基础常驻角色数", "standard_chain_extra": "基础常驻追加链数",
    "other_weapon_count": "非基础常驻武器件数", "other_weapon_refine_extra": "非基础常驻追加精炼份数",
    "standard_weapon_count": "基础常驻武器件数", "standard_weapon_refine_extra": "基础常驻追加精炼份数",
    "level": "联觉等级", "skin_count": "列示人物皮肤数（非已验付费数）",
    "vehicle_count": "列示车架模组数", "accessory_count": "列示摩托饰品数",
    "observed_flexible_pulls": "已列示星声/月相折算抽数",
    "observed_character_tickets": "已列示浮金波纹", "observed_weapon_tickets": "已列示铸潮波纹",
    "coral_pulls": "已列示余波珊瑚抽券等价数", "verified_pair_count": "已核对角色武器配对数",
}


def number(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None


def named_items(text, kind):
    """全条目解析；武器保留同名多件，角色同名采用最高链并另报冲突。"""
    parts = re.split(r"[、，,；;]", text or "")
    pattern = r"([0-6])命\s*(.+)" if kind == "character" else r"精([1-5])\s*(.+)"
    items, unparsed = [], []
    for token in parts:
        token = token.strip()
        if not token:
            continue
        match = re.fullmatch(pattern, token)
        if not match:
            unparsed.append(token)
        else:
            items.append((match[2].strip(), int(match[1])))
    return items, unparsed


def parse_row(row, map_catalog=None):
    flags = []
    raw_chars, bad_chars = named_items(row.get("共鸣链（N命）", ""), "character")
    weapons, bad_weapons = named_items(row.get("武器精炼（精N）", ""), "weapon")
    chars = {}
    repeated = []
    for name, chain in raw_chars:
        if name in chars:
            repeated.append(name)
        chars[name] = max(chain, chars.get(name, -1))
    if repeated:
        flags.append("duplicate_character:" + ",".join(sorted(set(repeated))))
    if not chars:
        flags.append("character_detail_missing")
    if not weapons:
        flags.append("weapon_detail_missing")
    if bad_chars:
        flags.append("character_unparsed")
    if bad_weapons:
        flags.append("weapon_unparsed")
    if row.get("游戏") != "鸣潮":
        flags.append("wrong_game")
    if row.get("区服") != "官服":
        flags.append("unsupported_server")
    numeric = {c: number(row.get(c)) for c in NUMERIC_COLUMNS}
    for key, value in numeric.items():
        if value is None:
            flags.append("missing:" + key)
        elif value < 0 or int(value) != value:
            flags.append("invalid:" + key)
    if numeric["等级"] is not None and not 1 <= numeric["等级"] <= 80:
        flags.append("level_out_of_range")
    if numeric["五星角色"] is not None and numeric["五星角色"] != len(chars):
        flags.append("character_count_mismatch")
    if numeric["五星武器"] is not None and numeric["五星武器"] != len(weapons):
        flags.append("weapon_count_mismatch")
    char_copies = sum(chain + 1 for chain in chars.values())
    weapon_copies = sum(refine for _, refine in weapons)
    reconstructed = char_copies + weapon_copies
    if numeric["黄数"] is not None and numeric["黄数"] != reconstructed:
        flags.append("yellow_mismatch")
    resource_text = row.get("资源", "") or ""
    resources = {}
    for token in re.split(r"[；;]", resource_text):
        match = re.fullmatch(r"\s*([^：:]+)[：:]\s*(\d+)\s*", token)
        if match:
            key, value = match[1].strip(), int(match[2])
            if key in resources:
                flags.append("duplicate_resource:" + key)
            resources[key] = value
        elif token.strip():
            flags.append("resource_unparsed")
    if not resource_text:
        flags.append("resource_detail_missing")
    extras = {}
    for token in re.split(r"[；;]", row.get("额外付费商品", "") or ""):
        if "：" in token:
            key, text = token.split("：", 1)
            extras[key.strip()] = [x.strip() for x in re.split(r"[、，,]", text) if x.strip()]
    standard = {k: v for k, v in chars.items() if k in STANDARD_CHARACTERS}
    other = {k: v for k, v in chars.items() if k not in STANDARD_CHARACTERS}
    sw = [(k, v) for k, v in weapons if k in STANDARD_WEAPONS]
    ow = [(k, v) for k, v in weapons if k not in STANDARD_WEAPONS]
    # 未展示的资源保留 None；以下合计是“已列示下界”，另有缺项特征。
    flexible = (resources.get("星声", 0) + resources.get("月相", 0)) / 160
    weapon_names = {k for k, _ in weapons}
    features = {
        "yellow": numeric["黄数"], "other_character_count": len(other),
        "other_chain_extra": sum(other.values()), "other_chain_2plus": sum(v >= 2 for v in other.values()),
        "other_chain_3plus": sum(v >= 3 for v in other.values()), "other_chain_6": sum(v == 6 for v in other.values()),
        "standard_character_count": len(standard), "standard_chain_extra": sum(standard.values()),
        "other_weapon_count": len(ow), "other_weapon_refine_extra": sum(v - 1 for _, v in ow),
        "standard_weapon_count": len(sw), "standard_weapon_refine_extra": sum(v - 1 for _, v in sw),
        "level": numeric["等级"], "skin_count": len(extras.get("人物皮肤", [])),
        "vehicle_count": len(extras.get("车架模组", [])), "accessory_count": len(extras.get("摩托饰品", [])),
        "observed_flexible_pulls": flexible, "observed_character_tickets": resources.get("浮金波纹", 0),
        "observed_weapon_tickets": resources.get("铸潮波纹", 0), "coral_pulls": resources.get("余波珊瑚", 0) / 8,
        "verified_pair_count": sum(c in chars and w in weapon_names for c, w in VERIFIED_PAIRS.items()),
    }
    for key in RESOURCE_KEYS:
        features["missing_resource:" + key] = int(key not in resources)
    maps = E.estimate(E.parse_csv(row.get("地图探索度"), map_catalog), map_catalog)
    features.update(map_remaining_astrite_pulls=maps["remaining_astrite_pulls"],
                    map_mean_progress=None if maps["mean_progress"] is None else maps["mean_progress"] / 100,
                    map_progress_coverage=maps["observed_maps"] / maps["expected_maps"] if maps["expected_maps"] else 0,
                    map_astrite_coverage=maps["coverage"]["astrite"],
                    missing_map_progress=int(not maps["observed_maps"]))
    for key in E.RESOURCE_LABELS:
        if key != "astrite":
            features["map_remaining_" + key] = maps["remaining"][key]
        features["missing_map_resource:" + key] = int(maps["remaining"][key] is None)
    features["missing_extra_detail"] = int(not row.get("额外付费商品"))
    for name, chain in chars.items():
        features["char_present:" + name] = 1
        features["char_chain:" + name] = chain
    for name, refine in weapons:
        key = "weapon_copies:" + name
        features[key] = features.get(key, 0) + refine
    price = number(row.get("价格 ¥"))
    if price is None or price <= 0:
        flags.append("invalid_price")
    # 配置指纹不使用价格、收藏、商品 ID 或发布时间。
    fingerprint_payload = [sorted(chars.items()), sorted(weapons), sorted(resources.items()), sorted(extras.items()), numeric["等级"], row.get("区服", "")]
    fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, ensure_ascii=False).encode()).hexdigest()
    return {"raw": row, "id": str(row.get("listing_id", "")), "price": price,
            "chars": chars, "weapons": weapons, "resources": resources,
            "maps": maps,
            "features": features, "flags": flags, "fingerprint": fingerprint,
            "char_copies": char_copies, "weapon_copies": weapon_copies,
            "reconstructed_yellow": reconstructed,
            "eligible": not flags}


def read_rows(path, map_catalog=None):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError("CSV 缺少列: " + ",".join(sorted(missing)))
        records = [parse_row(r, map_catalog) for r in reader]
    seen = set()
    for r in records:
        if not r["id"] or r["id"] in seen:
            r["flags"].append("duplicate_or_missing_listing_id")
            r["eligible"] = False
        seen.add(r["id"])
    return records


def transformed(value, key):
    # 数量取 log1p，链数/等级线性：保留非线性边际与高链阈值。
    if value is None:
        return None
    if key.startswith("missing_") or key.startswith("char_present:") or key in {"map_mean_progress", "map_progress_coverage", "map_astrite_coverage"}:
        return float(value)
    if key in {"level", "other_chain_extra", "standard_chain_extra", "other_chain_2plus", "other_chain_3plus", "other_chain_6"} or key.startswith("char_chain:"):
        return float(value)
    return math.log1p(value)


def feature_keys(records, family):
    if family == "yellow":
        return ["yellow"]
    keys = CORE_FEATURES + ["missing_resource:" + k for k in RESOURCE_KEYS] + ["missing_extra_detail"]
    if family.startswith("exploration"):
        keys += MAP_FEATURES
    if family in {"named", "exploration_named"}:
        counts = collections.Counter(k for r in records for k, v in r["features"].items()
                                     if k.startswith(("char_present:", "char_chain:", "weapon_copies:")) and v > 0)
        # 名称只从当前训练集合选出，不窥视验证/测试；稀有名称退回聚合特征。
        keys += sorted(k for k, n in counts.items() if n >= 12)
    return keys


def design(records, keys, medians=None):
    import numpy as np
    matrix = []
    for r in records:
        row = []
        for key in keys:
            value = r["features"].get(key)
            if value is None and key.startswith(("char_present:", "char_chain:", "weapon_copies:")):
                value = 0
            value = transformed(value, key)
            row.append(float("nan") if value is None else value)
        matrix.append(row)
    x = np.array(matrix, dtype=float).reshape(len(records), len(keys))
    if medians is None:
        # 全训练集未知的列没有可学信息；矩阵占位为0，原特征仍为None并保留缺失标记。
        medians = np.array([np.median(col[np.isfinite(col)]) if np.isfinite(col).any() else 0. for col in x.T])
    return np.where(np.isnan(x), medians, x), medians


def fit(records, family, alpha):
    import numpy as np
    y = np.log([r["price"] for r in records])
    if family == "median":
        return {"family": family, "intercept": float(np.median(y)), "keys": [], "alpha": alpha,
                "medians": [], "mean": [], "scale": [], "coef": [], "train_n": len(records)}
    keys = feature_keys(records, family)
    x, medians = design(records, keys)
    mean, scale = x.mean(axis=0), x.std(axis=0)
    scale[scale < 1e-10] = 1
    z = (x - mean) / scale
    intercept = float(y.mean())
    coef = np.linalg.solve(z.T @ z + alpha * np.eye(len(keys)), z.T @ (y - intercept))
    return {"family": family, "keys": keys, "alpha": alpha, "intercept": intercept,
            "medians": medians.tolist(), "mean": mean.tolist(), "scale": scale.tolist(),
            "coef": coef.tolist(), "train_n": len(records),
            "raw_min": {k: min(r["features"].get(k, 0) for r in records) for k in CORE_FEATURES},
            "raw_max": {k: max(r["features"].get(k, 0) for r in records) for k in CORE_FEATURES}}


def predict_log(record, model):
    if model["family"] == "median":
        return model["intercept"]
    result = model["intercept"]
    for i, key in enumerate(model["keys"]):
        value = record["features"].get(key)
        if value is None and key.startswith(("char_present:", "char_chain:", "weapon_copies:")):
            value = 0
        value = transformed(value, key)
        if value is None:
            value = model["medians"][i]
        result += (value - model["mean"][i]) / model["scale"][i] * model["coef"][i]
    return result


def predict(records, model):
    import numpy as np
    return np.exp([predict_log(r, model) for r in records])


def support_warnings(record, model):
    unseen_chars = set(record["chars"]) - set(model.get("known_characters", []))
    unseen_weapons = {k for k, _ in record["weapons"]} - set(model.get("known_weapons", []))
    warnings = []
    if unseen_chars:
        warnings.append("unseen_character:" + ",".join(sorted(unseen_chars)))
    if unseen_weapons:
        warnings.append("unseen_weapon:" + ",".join(sorted(unseen_weapons)))
    outside = [k for k in SUPPORT_FEATURES if record["features"][k] > model.get("raw_max", {}).get(k, math.inf)]
    if outside:
        warnings.append("outside_training_range:" + ",".join(outside))
    return warnings


def group_folds(records, k, seed):
    groups = sorted(set(r["fingerprint"] for r in records))
    random.Random(seed).shuffle(groups)
    assignment = {g: i % k for i, g in enumerate(groups)}
    return [[i for i, r in enumerate(records) if assignment[r["fingerprint"]] == fold] for fold in range(k)]


def metrics(records, estimates, q=None):
    import numpy as np
    actual = np.array([r["price"] for r in records])
    relative = np.abs(estimates / actual - 1)
    result = {"n": len(records), "mae_yuan": float(np.abs(actual - estimates).mean()),
              "median_ape": float(np.median(relative)), "mean_ape": float(relative.mean()),
              "log_mae": float(np.abs(np.log(actual) - np.log(estimates)).mean()),
              "within_30pct": float((relative <= .30).mean()),
              "wape": float(np.abs(actual - estimates).sum() / actual.sum())}
    if q is not None:
        result["coverage"] = float((np.abs(np.log(actual / estimates)) <= q).mean())
    return result


def select(records, seed=10105, families=None, grouping=None):
    import numpy as np
    folds = (grouping or group_folds)(records, 4, seed)
    families = families if families is not None else ["median", "yellow", "structure", "named"]
    candidates = [(family, a) for family in families for a in ([0] if family == "median" else [1., 10., 100.])]
    scores = []
    for family, alpha in candidates:
        predicted = np.zeros(len(records))
        for fold in folds:
            held = set(fold)
            train = [r for i, r in enumerate(records) if i not in held]
            model = fit(train, family, alpha)
            predicted[fold] = predict([records[i] for i in fold], model)
        score = metrics(records, predicted)
        score.update(family=family, alpha=alpha)
        scores.append(score)
    # 仅训练集合内 CV 选模型，测试不参与。按 log MAE 保证各价位可比。
    best = min(scores, key=lambda s: s["log_mae"])
    return fit(records, best["family"], best["alpha"]), scores


def quantile(values, target=.8):
    ordered = sorted(float(x) for x in values)
    rank = min(len(ordered), math.ceil((len(ordered) + 1) * target))
    return ordered[max(0, rank - 1)]


def json_write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def csv_write(path, rows, fields=None):
    if not rows:
        return
    with Path(path).open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def audit(records):
    import numpy as np
    prices = [r["price"] for r in records if r["price"] is not None and r["price"] > 0]
    groups = collections.Counter(r["fingerprint"] for r in records)
    flags = collections.Counter(f.split(":")[0] for r in records for f in r["flags"])
    return {"total_rows": len(records), "unique_listing_ids": len(set(r["id"] for r in records)),
            "eligible_rows": sum(r["eligible"] for r in records), "flagged_rows": sum(bool(r["flags"]) for r in records),
            "servers": dict(collections.Counter(r["raw"].get("区服") or "缺失" for r in records)),
            "price_quantiles": {str(p): float(np.quantile(prices, p)) for p in [0, .05, .25, .5, .75, .95, 1]},
            "price_mean": float(np.mean(prices)), "flags": dict(flags),
            "duplicate_asset_groups": sum(n > 1 for n in groups.values()),
            "duplicate_asset_extra_rows": sum(n - 1 for n in groups.values() if n > 1),
            "resource_missing_counts": {k: sum(k not in r["resources"] for r in records) for k in RESOURCE_KEYS},
            "publication_min": min(r["raw"]["商品发布时间"] for r in records),
            "publication_max": max(r["raw"]["商品发布时间"] for r in records)}


def cohort_summary(records):
    import numpy as np
    rows = []
    for title, fn in [
        ("黄数", lambda r: "≤30" if r["features"]["yellow"] <= 30 else "31–50" if r["features"]["yellow"] <= 50 else "51–70" if r["features"]["yellow"] <= 70 else ">70"),
        ("非基础常驻6链人数", lambda r: str(min(2, r["features"]["other_chain_6"])) + ("+" if r["features"]["other_chain_6"] >= 2 else "")),
    ]:
        groups = collections.defaultdict(list)
        for r in records:
            groups[fn(r)].append(r["price"])
        for group, values in sorted(groups.items()):
            rows.append({"维度": title, "组别": group, "样本数": len(values), "挂牌中位数_元": round(float(np.median(values)), 2),
                         "挂牌P25_元": round(float(np.quantile(values, .25)), 2), "挂牌P75_元": round(float(np.quantile(values, .75)), 2),
                         "口径": "有效官服样本；描述性分组，非控制其他配置的溢价"})
    return rows


def explain(record, model):
    terms = []
    for i, key in enumerate(model["keys"]):
        raw = record["features"].get(key)
        if raw is None and key.startswith(("char_present:", "char_chain:", "weapon_copies:")):
            raw = 0
        value = transformed(raw, key)
        if value is None:
            value = model["medians"][i]
        term = (value - model["mean"][i]) / model["scale"][i] * model["coef"][i]
        terms.append((key, term))
    best = sorted(terms, key=lambda x: abs(x[1]), reverse=True)[:5]
    return "; ".join(f"{FEATURE_LABELS.get(k, k)}:{v:+.3f}" for k, v in best)


def comparable_rows(record, pool, model, k=5):
    keys = [x for x in CORE_FEATURES if x not in {"verified_pair_count", "vehicle_count", "accessory_count"}]
    spread = {key: max(1., model.get("raw_max", {}).get(key, 1) - model.get("raw_min", {}).get(key, 0)) for key in keys}
    peers = []
    for candidate in pool:
        if candidate["fingerprint"] == record["fingerprint"]:
            continue
        distance = sum(((record["features"].get(key) or 0) - (candidate["features"].get(key) or 0)) ** 2 / spread[key] ** 2 for key in keys) ** .5
        peers.append((distance, candidate))
    # 同配置多挂单只保留一条可比号，防止同一配置影响加权参考。
    picked, seen = [], set()
    for distance, candidate in sorted(peers, key=lambda t: (t[0], t[1]["id"])):
        if candidate["fingerprint"] not in seen:
            picked.append({"目标listing_id": record["id"], "可比listing_id": candidate["id"],
                           "结构距离": round(distance, 4), "可比挂牌价_元": candidate["price"]})
            seen.add(candidate["fingerprint"])
        if len(picked) == k:
            break
    return picked


def run(input_path, output, *, use_exploration=True):
    import numpy as np
    if use_exploration:
        probe = read_rows(input_path)
        if any(r["maps"]["observed_maps"] for r in probe):
            from tools.wuwa_exploration_valuation import run as run_exploration
            return run_exploration(input_path, output)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    records = read_rows(input_path)
    eligible = [r for r in records if r["eligible"]]
    if len(eligible) < 80:
        raise ValueError("有效样本不足80，停止训练；请补齐字段或增加样本。")
    quality = audit(records)
    # 指纹分组固定 60/20/20；每个重复配置只能进入一个集合。
    folds = group_folds(eligible, 5, 20261005)
    fit_ids = set(folds[0] + folds[1] + folds[2])
    train = [r for i, r in enumerate(eligible) if i in fit_ids]
    calibration = [eligible[i] for i in folds[3]]
    test = [eligible[i] for i in folds[4]]
    model, cv_scores = select(train)
    cal_pred = predict(calibration, model)
    q = quantile(np.abs(np.log([r["price"] for r in calibration]) - np.log(cal_pred)))
    test_pred = predict(test, model)
    heldout = metrics(test, test_pred, q)
    benchmark_rows = []
    for family in ["median", "yellow", "structure", "named"]:
        chosen = min([s for s in cv_scores if s["family"] == family], key=lambda s: s["log_mae"])
        alternative = fit(train, family, chosen["alpha"])
        score = metrics(test, predict(test, alternative))
        score.update(family=family, alpha=chosen["alpha"], selected=family == model["family"])
        benchmark_rows.append(score)
    oof = {}
    oof_terms = {}
    oof_warnings = {}
    oof_folds = group_folds(eligible, 5, 73021)
    outer_scores = []
    # 每个账号完全不参与自身及同配置组的模型选择/训练。
    for fold_number, indices in enumerate(oof_folds):
        excluded = set(indices)
        outer_train = [r for i, r in enumerate(eligible) if i not in excluded]
        outer_test = [eligible[i] for i in indices]
        fold_model, _ = select(outer_train, seed=fold_number + 814)
        fold_model["known_characters"] = sorted({k for r in outer_train for k in r["chars"]})
        fold_model["known_weapons"] = sorted({k for r in outer_train for k, _ in r["weapons"]})
        fold_pred = predict(outer_test, fold_model)
        outer_scores.append({"fold": fold_number + 1, "family": fold_model["family"], "alpha": fold_model["alpha"], **metrics(outer_test, fold_pred)})
        for r, pred in zip(outer_test, fold_pred):
            oof[r["id"]] = float(pred)
            oof_terms[r["id"]] = explain(r, fold_model)
            oof_warnings[r["id"]] = support_warnings(r, fold_model)
            if pred > max(r0["price"] for r0 in outer_train):
                oof_warnings[r["id"]].append("prediction_above_training_price_max")
    oof_metric = metrics(eligible, np.array([oof[r["id"]] for r in eligible]))
    empirical_q = quantile(abs(math.log(r["price"] / oof[r["id"]])) for r in eligible)
    model.update(version="wuwa-listing-v1.0", target="pxb7 官服挂牌参考价，人民币",
                 input_sha256=hashlib.sha256(Path(input_path).read_bytes()).hexdigest(),
                 interval_log_radius=q, interval_target=.8, interval_method="split conformal, group split",
                 valid_scope="完整官服配置；单次快照；不含声骸、账号交接、成交折扣；新名称需复核",
                 feature_labels=FEATURE_LABELS, train_ids=[r["id"] for r in train],
                 calibration_ids=[r["id"] for r in calibration], test_ids=[r["id"] for r in test],
                 known_characters=sorted({k for r in train for k in r["chars"]}),
                 known_weapons=sorted({k for r in train for k, _ in r["weapons"]}))
    result_rows, comparables = [], []
    splits = {r["id"]: name for name, group in [("train", train), ("calibration", calibration), ("test", test)] for r in group}
    for r in records:
        estimate = oof.get(r["id"])
        low = estimate * math.exp(-empirical_q) if estimate else None
        high = estimate * math.exp(empirical_q) if estimate else None
        relation = "暂不估值：字段/区服待复核"
        if estimate:
            relation = "低于参考带：核验线索" if r["price"] < low else "高于参考带：定价复核" if r["price"] > high else "参考带内"
            if oof_warnings.get(r["id"]):
                relation = "外推/新名称：人工估值"
        peers = comparable_rows(r, eligible, model) if estimate else []
        comparables.extend(peers)
        result_rows.append({"listing_id": r["id"], "区服": r["raw"].get("区服", ""), "挂牌价_元": r["price"],
                            "分组留出估值_元": round(estimate, 2) if estimate else "",
                            "经验参考带下限_元": round(low, 2) if low else "", "经验参考带上限_元": round(high, 2) if high else "",
                            "挂牌相对估值偏差": round(r["price"] / estimate - 1, 4) if estimate else "",
                            "关系": relation, "配置质量": "可建模" if r["eligible"] else "待复核",
                            "模型支持标记": ";".join(oof_warnings.get(r["id"], [])),
                            "交易条件完整性": "低：实名/绑定/交接/违规记录未采集", "练度完整性": "低：声骸/技能/深塔记录未采集",
                            "基准验证分组": splits.get(r["id"], "excluded"), "质量标记": ";".join(r["flags"]),
                            "重建黄数": r["reconstructed_yellow"], "角色份数": r["char_copies"], "武器份数": r["weapon_copies"],
                            "非基础常驻角色数": r["features"]["other_character_count"], "非基础常驻6链人数": r["features"]["other_chain_6"],
                            "已列示星声月相抽数": round(r["features"]["observed_flexible_pulls"], 2),
                            "资源未列示项": "、".join(k for k in RESOURCE_KEYS if k not in r["resources"]),
                            "配对覆盖": "仅嘉贝莉娜×光影双生；未覆盖其余配对", "配队估值状态": "待人工核验，不计额外配队溢价",
                            "主要模型项_log相对训练均值": oof_terms.get(r["id"], ""),
                            "可比账号ID": ";".join(p["可比listing_id"] for p in peers)})
    audit_rows = []
    for r in records:
        a = {"listing_id": r["id"], "纳入训练候选": r["eligible"], "质量标记": ";".join(r["flags"]),
             "角色明细数_去重": len(r["chars"]), "角色声明数": r["raw"].get("五星角色", ""),
             "武器明细件数_保留重复": len(r["weapons"]), "武器声明数": r["raw"].get("五星武器", ""),
             "黄数声明": r["raw"].get("黄数", ""), "重建黄数": r["reconstructed_yellow"], "配置指纹": r["fingerprint"]}
        for k in RESOURCE_KEYS:
            a[k] = r["resources"].get(k, "")
        audit_rows.append(a)
    coefficient_rows = [{"特征": key, "名称": FEATURE_LABELS.get(key, key), "标准化log价格系数": round(coef, 6),
                         "换算乘数_每1个变换后标准差": round(math.exp(coef), 6), "变换": "线性" if transformed(2, key) == 2 else "log1p",
                         "变换后训练均值": model["mean"][i], "变换后训练标准差": model["scale"][i],
                         "解释边界": "相关性系数；不可作为单件资产人民币售价；共线与缺失代理可能影响符号"}
                        for i, (key, coef) in enumerate(zip(model["keys"], model["coef"]))]
    validation = {"split_seed": 20261005, "split_unit": "配置指纹组；含资源、外观、等级、区服，不含价格/收藏/发布时间",
                  "fit_n": len(train), "calibration_n": len(calibration), "test_n": len(test),
                  "selected_family": model["family"], "selected_alpha": model["alpha"],
                  "cv_selection": "仅fit集合内部四折分组CV，按log MAE选模型与alpha；test只评估",
                  "heldout_metrics": heldout, "interval_log_radius": q, "interval_price_multiplier": math.exp(q),
                  "oof_metrics": oof_metric, "oof_interval_radius": empirical_q,
                  "oof_interval_multiplier": math.exp(empirical_q), "oof_folds": outer_scores,
                  "oof_interval_caveat": "全体OOF绝对log误差80%经验参考带，用于样本内筛查；非独立校准的置信区间或未来覆盖保证",
                  "split_interval_caveat": "名义80%同分布新配置预测带；缺失交易条件、版本变化及非独立卖家会破坏覆盖前提；不是价值置信区间",
                  "temporal_validation": "未做：单次导出，发布日期不是快照历史", "seller_group_validation": "未做：无卖家ID；相近配置可能仍相关"}
    json_write(output / "model.json", model)
    json_write(output / "data_quality.json", quality)
    json_write(output / "validation.json", validation)
    csv_write(output / "账号估值结果.csv", result_rows)
    csv_write(output / "字段审计.csv", audit_rows)
    csv_write(output / "可比账号明细.csv", comparables)
    csv_write(output / "模型系数.csv", coefficient_rows)
    csv_write(output / "模型对比.csv", benchmark_rows)
    csv_write(output / "训练内交叉验证.csv", cv_scores)
    csv_write(output / "市场分层.csv", cohort_summary(eligible))
    json_write(output / "manifest.json", {"source": str(Path(input_path).resolve()), "sha256": model["input_sha256"],
                                          "generated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
                                          "target": model["target"], "quality": quality,
                                          "model_file": "model.json", "result_rows": len(result_rows)})
    print(json.dumps({"quality": quality, "validation": validation, "output": str(output)}, ensure_ascii=False, indent=2))
    return quality, validation


def score_new(input_path, model_path, output_path):
    model = json.loads(Path(model_path).read_text(encoding="utf-8"))
    records = read_rows(input_path, model.get("map_catalog"))
    rows = []
    for r in records:
        # 新数据的价格可以缺失；估值只依赖配置，价格不作输入。
        flags = [f for f in r["flags"] if f != "invalid_price"]
        unseen = sorted(set(r["chars"]) - set(model.get("known_characters", [])))
        unseen_weapons = sorted({k for k, _ in r["weapons"]} - set(model.get("known_weapons", [])))
        outside = [k for k in CORE_FEATURES if r["features"].get(k) is not None and (r["features"][k] < model.get("raw_min", {}).get(k, -math.inf) or r["features"][k] > model.get("raw_max", {}).get(k, math.inf))]
        warnings = flags + (["unseen_character:" + ",".join(unseen)] if unseen else []) + (["unseen_weapon:" + ",".join(unseen_weapons)] if unseen_weapons else [])
        if outside:
            warnings.append("outside_training_range:" + ",".join(outside))
        if r["id"] in model.get("train_ids", []) + model.get("calibration_ids", []) + model.get("test_ids", []):
            warnings.append("known_listing:使用账号估值结果.csv的OOF结果比较挂牌")
        estimate = math.exp(predict_log(r, model)) if not flags and not unseen and not unseen_weapons and not outside else None
        q = model["interval_log_radius"]
        rows.append({"listing_id": r["id"], "挂牌参考估值_元": round(estimate, 2) if estimate else "",
                     "地图预计剩余资源": E.remaining_text(E.parse_csv(r["raw"].get("地图探索度"), model.get("map_catalog")), model.get("map_catalog")) or "未知",
                     "地图模型贡献_log": round(map_contribution(r, model), 6) if estimate else "",
                     "名义80pct预测带下限_元": round(estimate * math.exp(-q), 2) if estimate else "",
                     "名义80pct预测带上限_元": round(estimate * math.exp(q), 2) if estimate else "",
                     "复核标记": ";".join(warnings), "适用范围": model["valid_scope"]})
    csv_write(output_path, rows)


def map_contribution(record, model):
    """相对训练均值的地图项 log 贡献；不是固定人民币价格或因果贡献。"""
    result = 0.0
    for i, key in enumerate(model["keys"]):
        if key not in MAP_FEATURES:
            continue
        value = transformed(record["features"].get(key), key)
        value = model["medians"][i] if value is None else value
        result += (value - model["mean"][i]) / model["scale"][i] * model["coef"][i]
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", help="已有模型路径：只预测，输出CSV；省略则训练并输出目录")
    parser.add_argument("--legacy", action="store_true", help="只运行原有模型研究流程，忽略地图新列")
    args = parser.parse_args()
    if args.model:
        score_new(args.input, args.model, args.output)
    else:
        run(args.input, args.output, use_exploration=not args.legacy)
