"""鸣潮地图进度及潜在资源。余额与估算分开；未知值始终保留 None。"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path

FEATURE = "wuwa_map_exploration"
# 仅统计星声；抽卡券（浮金/铸潮/唤声涡纹）不在地图估算范围内，展示与特征均不产出。
RESOURCE_LABELS = {"astrite": "星声"}


@lru_cache(maxsize=1)
def catalog():
    return json.loads((Path(__file__).with_name("wuwa_map_resources.json")).read_text(encoding="utf-8"))


def canonical(name, config=None):
    config = config if config is not None else catalog()
    name = re.sub(r"\s+", "", str(name or ""))
    return config.get("aliases", {}).get(name, name)


def percent(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(str(value).strip().rstrip("%％"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and 0 <= value <= 100 else None


def normalize(entries, config=None):
    """别名去重；矛盾进度保留未知；已有子区域时不重复计算父区域。"""
    config = config if config is not None else catalog()
    found, conflicts = {}, set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        name = canonical(entry.get("name"), config)
        if not name:
            continue
        value = percent(entry.get("progress"))
        if name in found and found[name] != value:
            conflicts.add(name)
        found[name] = value
    for name in conflicts:
        found[name] = None
    parents = set()
    for name in found:
        parent = config.get("maps", {}).get(name, {}).get("parent")
        visited = {name}
        while parent and parent not in visited:
            visited.add(parent)
            parents.add(parent)
            parent = config.get("maps", {}).get(parent, {}).get("parent")
    return [{"name": name, "progress": value} for name, value in found.items() if name not in parents]


def parse_html(html):
    from bs4 import BeautifulSoup
    from .parser import prune_hidden, sanitize_dom_text
    soup = BeautifulSoup(sanitize_dom_text(html), "html.parser")
    prune_hidden(soup)
    for node in soup.select("script, style, noscript"):
        node.decompose()
    entries = []
    # 真正的报告容器；不从同名导航、商品标题、推荐卡片猜测进度。
    for section in soup.select(".ReportCMapExploration, [data-map-exploration]"):
        for label in section.find_all(string=re.compile(r"探索度")):
            node = label.parent
            text = node.get_text(" ", strip=True)
            if not re.search(r"\d+(?:\.\d+)?\s*[%％]", text):
                node = node.parent
                if node is None:
                    continue
                text = node.get_text(" ", strip=True)
            match = re.search(r"探索度\s*[:：]?\s*(-?\d+(?:\.\d+)?)\s*[%％]", text)
            if not match:
                continue
            sibling = node.find_previous_sibling()
            if sibling is None:
                continue
            name = sibling.get_text(" ", strip=True)
            if name and "探索度" not in name and len(name) <= 60:
                entries.append({"name": name, "progress": percent(match[1])})
    return normalize(entries)


def parse_csv(text, config=None):
    entries = []
    for token in re.split(r"[；;]", text or ""):
        match = re.fullmatch(r"\s*(.+?)[：:]\s*(\d+(?:\.\d+)?)\s*[%％]\s*", token)
        unknown = re.fullmatch(r"\s*(.+?)[：:]\s*未知\s*", token)
        if match or unknown:
            entries.append({"name": (match or unknown)[1], "progress": percent(match[2]) if match else None})
    return normalize(entries, config)


def estimate(entries, config=None):
    config = config if config is not None else catalog()
    entries = normalize(entries, config)
    maps = config.get("maps", {})
    rows = []
    totals = {key: None for key in RESOURCE_LABELS}
    covered = {key: 0 for key in RESOURCE_LABELS}
    for item in entries:
        meta = maps.get(item["name"], {})
        total, remaining = {}, {}
        for key in RESOURCE_LABELS:
            value = meta.get("resources", {}).get(key)
            # 配置未核实的数字也不得混入估算。
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                value = None
            total[key] = value
            rest = None if value is None or item["progress"] is None else round(value * (1 - item["progress"] / 100), 6)
            remaining[key] = rest
            if rest is not None:
                covered[key] += 1
                totals[key] = (totals[key] or 0) + rest
        rows.append({**item, "total": total, "remaining": remaining,
                     "approximate": meta.get("approximate", True)})
    # 分母是截至配置版本的叶子地图全集；不是只以有资料的地图作分母。
    parents = {m.get("parent") for m in maps.values() if m.get("parent")}
    expected = set(maps) - parents
    denominator = len(expected | {x["name"] for x in entries})
    progress = [x["progress"] for x in entries if x["progress"] is not None]
    coverage = {key: covered[key] / denominator if denominator else 0 for key in RESOURCE_LABELS}
    return {"version": config.get("version"), "label": "大致估算", "maps": rows,
            "remaining": totals, "remaining_astrite_pulls": None if totals["astrite"] is None else totals["astrite"] / 160,
            "observed_maps": len(progress), "expected_maps": denominator,
            "estimated_maps": covered["astrite"], "coverage": coverage,
            "mean_progress": sum(progress) / len(progress) if progress else None,
            "partial": any(coverage[key] < 1 for key in RESOURCE_LABELS)}


def progress_text(entries):
    return "；".join(f"{x['name']}：{x['progress']:g}%" if x["progress"] is not None else f"{x['name']}：未知"
                    for x in normalize(entries)) or None


def resource_text(values):
    return "；".join(f"{label}：约{value:g}" if value is not None else f"{label}：未知"
                    for key, label in RESOURCE_LABELS.items() for value in [values.get(key)])


def remaining_text(entries, config=None):
    if not entries:
        return None
    result = estimate(entries, config)
    return ("部分地图估算" if result["partial"] else "地图估算") + "（大致估算）：" + resource_text(result["remaining"])
