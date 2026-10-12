"""武器空值的采集语义；未公示不是数量 0，也不是采集失败。"""
from __future__ import annotations

import math
import json
import re
from typing import Any

FEATURE = 'five_star_weapon_information'
_EMPTY = re.compile(
    r'(?:^|[，,；;。\s：:])(?:(?:无|没有|暂无)五星武器|五星武器\s*[:：]\s*(?:无|没有|暂无))'
    r'(?=$|[，,；;。！\s【])')


def explicit_empty(text: str) -> bool:
    return bool(_EMPTY.search(text))


def valid_count(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0 and int(value) == value)


def observe(title: str | None, product_text: str, attributes: dict, features: dict) -> dict:
    """仅在就绪且有明确结尾的商品详情中标记未公示，不扫描截图、不推算数量。"""
    count = attributes.get('five_star_weapons', features.get('five_star_weapons_cnt'))
    if valid_count(count):
        return {'state': 'none' if count == 0 else 'present', 'source': 'detail',
                'reason': '页面明确提供五星武器数量'}
    complete = bool(title and re.search(r'详情看图|官方截图', title)
                    and not re.search(r'\.{3}|…|⋯', title)
                    and valid_count(attributes.get('five_star_chars')))
    if complete and not re.search(r'五星武器|精炼|谐振|满精|精\s*[1-5一二三四五]', product_text) \
            and not features.get('five_star_weapon_refinements'):
        return {'state': 'unreported', 'source': 'detail',
                'reason': '商品完整详情文字已采集，页面未列出五星武器及精炼；数量保持未知'}
    return {'state': 'unknown', 'source': 'detail',
            'reason': '详情未完整就绪，或存在武器信息但无法解析'}


def status(count: Any, features: dict) -> dict:
    """已保存的有效值优先于缺省观察，避免未公示覆盖已有资产。"""
    if isinstance(features, str):
        try:
            features = json.loads(features)
        except ValueError:
            features = {}
    features = features if isinstance(features, dict) else {}
    if count is None:
        count = features.get('five_star_weapons_cnt')
    observation = features.get(FEATURE)
    observation = observation if isinstance(observation, dict) else {}
    if valid_count(count):
        state = 'none' if count == 0 else 'present'
    elif count is None and observation.get('state') == 'unreported' and observation.get('source') == 'detail' \
            and not features.get('five_star_weapon_refinements') \
            and features.get('five_star_weapon_refined') in (None, 0):
        state = 'unreported'
    else:
        state = 'unknown'
    label = {'none': '已确认无五星武器', 'present': '已采到武器数量',
             'unreported': '已采集·页面未提供', 'unknown': '未采到／解析失败'}[state]
    return {'state': state, 'label': label,
            'reason': (observation.get('reason') if observation.get('state') == state else None) or label,
            'refinement_label': '不适用（无五星武器）' if state == 'none' else label,
            'resolved_empty': state in ('none', 'unreported')}
