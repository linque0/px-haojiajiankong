"""从已保存 DOM 修复已有快照字段，不新增轮次、不访问站点；默认预览，--apply 先备份再写入。

执行 --apply 前停止采集网关，完成后重新启动。原采集时间通过 raw 元数据定位。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pxb7 import config, db, extract as X, gateway as G, parser as P, pipeline as PL

FIELDS = ("level", "yellow_cnt", "five_star_chars", "five_star_weapons", "favorites_cnt")


def repair(settings, *, apply=False, paid_items_only=False, character_chains_only=False):
    if paid_items_only and character_chains_only:
        raise ValueError('请选择一种修复范围')
    with PL.run_lock(settings):
        conn = db.connect(settings.paths.db, read_only=not apply)
        try:
            rows = conn.execute(
                "SELECT s.listing_id, s.snapshot_at, l.game_id, s.level, s.yellow_cnt,"
                " s.five_star_chars, s.five_star_weapons, s.favorites_cnt, s.extracted_features"
                " FROM fct_listing_snapshot s JOIN dim_listing l USING(listing_id)").fetchall()
            original = {(r[0], r[1]): dict(zip(FIELDS, r[3:8]),
                        features=json.loads(r[8]) if r[8] else {}, game_id=r[2]) for r in rows}
            values = {k: {**v, "features": dict(v["features"])} for k, v in original.items()}
            tasks = {t.task_id: t for t in config.load_tasks(settings=settings).tasks}
            sources = []
            for meta_path in settings.paths.raw_root.rglob('*.json'):
                if not meta_path.name.startswith(('list_p', 'detail_')):
                    continue
                meta = json.loads(meta_path.read_text(encoding='utf-8'))
                html_path = meta_path.with_suffix('.html')
                task = tasks.get(meta.get('task_id', meta_path.parent.parent.parent.name))
                if not task or not meta.get('collected_at') or not html_path.exists():
                    continue
                sources.append((dt.datetime.fromisoformat(meta['collected_at']), task, html_path, meta))
            for at, task, html_path, meta in sorted(sources, key=lambda s: (s[0], str(s[2]))):
                if character_chains_only and task.game_id != 10302:
                    continue
                html = html_path.read_text(encoding='utf-8')
                if G._reject_risk_page(html):
                    continue
                keywords = X.seed_keywords(game_id=task.game_id)
                if html_path.name.startswith('list_p'):
                    round_ts = db.truncate_to_round(at, settings.snapshot_round_minutes)
                    for card in P.parse_list_page(html).cards:
                        key = (card.listing_id, round_ts)
                        if not card.parse_ok or key not in values or values[key]['game_id'] != task.game_id:
                            continue
                        item = values[key]
                        if paid_items_only:
                            if task.game_id == 10302:
                                item['features'].update(X.wuwa_paid_item_features(card.title or ''))
                            continue
                        full_list = card.hits.get('title') in {'L1-tooltip', 'L1-title-api'}
                        keep_full = item['features'].get('_list_full_title') and not full_list
                        for name in FIELDS:
                            if keep_full and name != 'favorites_cnt':
                                continue
                            if card.fields.get(name) is not None and name not in item['features'].get('_detail_fields', []):
                                item[name] = card.fields[name]
                        features = X.extract_listing(keywords, listing_id=card.listing_id,
                                                     title=card.title, card_fields=card.fields).features
                        if full_list:
                            features['_list_full_title'] = True
                            for roster, scalar in (('five_star_character_chains', 'constellation_cnt'),
                                                   ('five_star_weapon_refinements', 'five_star_weapon_refined')):
                                if roster not in features:
                                    item['features'].pop(roster, None)
                                    item['features'].pop(scalar, None)
                        if not keep_full and card.title and '四星角色' in card.title and 'constellation_cnt' not in features:
                            item['features'].pop('constellation_cnt', None)
                        preserved = set(item['features'].get('_detail_rosters', []))
                        if keep_full:
                            preserved.update(key for key in ('five_star_character_chains', 'five_star_weapon_refinements')
                                             if key in item['features'])
                        for roster in preserved:
                            features.pop(roster, None)
                            features.pop('constellation_cnt' if roster == 'five_star_character_chains'
                                         else 'five_star_weapon_refined', None)
                        item['features'].update(features)
                else:
                    listing_id = meta.get('listing_id')
                    candidates = [k for k in values if k[0] == listing_id and k[1] <= at
                                  and values[k]['game_id'] == task.game_id]
                    if meta.get('snapshot_round'):
                        pinned = dt.datetime.fromisoformat(meta['snapshot_round'])
                        candidates = [k for k in candidates if k[1] == pinned]
                    if not candidates:
                        continue
                    item = values[max(candidates, key=lambda k: k[1])]
                    title, attributes = P.parse_detail_attributes(html)
                    if paid_items_only:
                        if task.game_id == 10302:
                            item['features'].update(X.wuwa_paid_item_features(title or ''))
                        continue
                    item.update(attributes)
                    favorites = G._parse_detail_fields(html)['favorites_cnt']
                    if favorites is not None:
                        item['favorites_cnt'] = favorites
                    features = X.extract_listing(keywords, listing_id=listing_id,
                                                 title=title, card_fields=attributes).features
                    features['_detail_fields'] = list(attributes)
                    features['_detail_rosters'] = [key for key in
                        ('five_star_character_chains', 'five_star_weapon_refinements') if key in features]
                    item['features'].update(features)
            if character_chains_only:
                # 重放时沿用列表/详情来源优先级，最终仅写角色链数；其他特征和列保持原值。
                for key, item in values.items():
                    projected = dict(original[key]['features'])
                    if item['game_id'] == 10302:
                        for name in ('five_star_character_chains', 'constellation_cnt'):
                            if name in item['features']:
                                projected[name] = item['features'][name]
                    values[key] = {**original[key], 'features': projected}
            changes = {k: v for k, v in values.items() if v != original[k]}
            report = {'mode': 'apply' if apply else 'preview', 'snapshots': len(rows),
                      'scope': 'character_chains' if character_chains_only else 'paid_items' if paid_items_only else 'all_fields',
                      'raw_pages': len(sources), 'changed_snapshots': len(changes),
                      'field_changes': {name: sum(v[name] != original[k][name] for k, v in changes.items())
                                        for name in FIELDS}}
            if character_chains_only:
                report['zero_chain_entries_added'] = sum(
                    max(0, sum(entry['value'] == 0 for entry in v['features'].get('five_star_character_chains', []))
                        - sum(entry['value'] == 0 for entry in original[k]['features'].get('five_star_character_chains', [])))
                    for k, v in changes.items())
            if apply and changes:
                conn.execute('CHECKPOINT')
                backup = settings.paths.db.parent / 'backups' / ('fields-' + dt.datetime.now().strftime('%Y%m%dT%H%M%S%f') + '.duckdb')
                backup.parent.mkdir(parents=True, exist_ok=True)
                # Windows 不允许复制被 DuckDB 写连接占用的数据库；仍持有项目运行锁。
                conn.close()
                shutil.copy2(settings.paths.db, backup)
                conn = db.connect(settings.paths.db)
                report['backup'] = str(backup)
                conn.execute('BEGIN TRANSACTION')
                try:
                    for (listing_id, snapshot_at), item in changes.items():
                        conn.execute('UPDATE fct_listing_snapshot SET level=?, yellow_cnt=?, five_star_chars=?,'
                                     ' five_star_weapons=?, favorites_cnt=?, extracted_features=?'
                                     ' WHERE listing_id=? AND snapshot_at=?',
                                     [*[item[name] for name in FIELDS], json.dumps(item['features'], ensure_ascii=False),
                                      listing_id, snapshot_at])
                    conn.execute('COMMIT')
                except Exception:
                    conn.execute('ROLLBACK')
                    raise
            return report
        finally:
            conn.close()


if __name__ == '__main__':
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument('--apply', action='store_true')
    scope = args.add_mutually_exclusive_group()
    scope.add_argument('--paid-items-only', action='store_true', help='只补齐鸣潮额外付费商品，不改变其他字段')
    scope.add_argument('--character-chains-only', action='store_true', help='只更新鸣潮角色链数，补入未标注的0命角色')
    options = args.parse_args()
    print(json.dumps(repair(config.load_settings(), apply=options.apply, paid_items_only=options.paid_items_only,
                           character_chains_only=options.character_chains_only), ensure_ascii=False, indent=2))
