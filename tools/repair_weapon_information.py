"""从已有详情 DOM 补武器采集状态；离线、默认预览，--apply 备份后事务写入。

应用前停止采集网关；不改价格、原采集时间或其他属性，未公示不填 0。
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

from pxb7 import analysis as A, collection as C, config, db, extract as X, gateway as G
from pxb7 import parser as P, pipeline as PL, weapons as W


def repair(settings, *, apply=False):
    with PL.run_lock(settings):
        conn = db.connect(settings.paths.db, read_only=not apply)
        try:
            rows = conn.execute('SELECT s.listing_id,s.snapshot_at,s.five_star_weapons,s.extracted_features'
                                ' FROM fct_listing_snapshot s JOIN dim_listing l USING(listing_id)'
                                ' WHERE l.game_id=10302').fetchall()
            original = {(r[0], r[1]): (r[2], G._parse_features(r[3])) for r in rows}
            latest = {}
            for key in original:
                latest[key[0]] = max(latest.get(key[0], key[1]), key[1])
            tasks = {t.task_id for t in config.load_tasks(settings=settings).tasks if t.game_id == 10302}
            sources = []
            for path in settings.paths.raw_root.rglob('detail_*.json'):
                try:
                    meta = json.loads(path.read_text(encoding='utf-8'))
                    at = dt.datetime.fromisoformat(meta['collected_at'])
                    if meta.get('task_id') not in tasks or not path.with_suffix('.html').is_file():
                        continue
                    listing = str(meta.get('listing_id') or '')
                    candidates = [key for key in original if key[0] == listing and key[1] <= at]
                    if meta.get('snapshot_round'):
                        stamp = dt.datetime.fromisoformat(meta['snapshot_round'])
                        candidates = [key for key in candidates if key[1] == stamp]
                    if candidates:
                        sources.append((at, path, max(candidates, key=lambda key: key[1])))
                except (KeyError, ValueError, TypeError):
                    continue
            values = {key: (count, dict(features)) for key, (count, features) in original.items()}
            for at, path, key in sorted(sources, key=lambda source: (source[0], str(source[1]))):
                html = path.with_suffix('.html').read_text(encoding='utf-8')
                if G._reject_risk_page(html):
                    continue
                title, attributes = P.parse_detail_attributes(html)
                extraction = X.extract_listing(X.seed_keywords(game_id=10302), listing_id=key[0],
                                               title=title, card_fields=attributes)
                observation = W.observe(title, P.detail_product_text(html), attributes, extraction.features)
                # 每个轮次仅使用最新一份适用的详情证据；旧零不能覆盖后来的资产。
                count, original_features = original[key]
                features = dict(original_features)
                # 只补标记与有明文证据的零；不重放其他字段，也不删除已知资产。
                known_count = count if count is not None else features.get('five_star_weapons_cnt')
                if attributes.get('five_star_weapons') == 0 and known_count in (None, 0) \
                        and not features.get('five_star_weapon_refinements') \
                        and features.get('five_star_weapon_refined') in (None, 0):
                    count = 0
                    features['five_star_weapons_cnt'] = 0
                    features.pop('five_star_weapon_refinements', None)
                    features.pop('five_star_weapon_refined', None)
                features[W.FEATURE] = observation
                values[key] = (count, features)
            changes = {key: value for key, value in values.items() if value != original[key]}
            report = {'mode': 'apply' if apply else 'preview', 'matched_detail_pages': len(sources),
                      'changed_snapshots': len(changes),
                      'explicit_zero_updates': sum(value[0] != original[key][0] for key, value in changes.items())}
            if apply and changes:
                conn.execute('CHECKPOINT')
                backup = settings.paths.db.parent / 'backups' / (
                    'weapon-information-' + dt.datetime.now().strftime('%Y%m%dT%H%M%S%f') + '.duckdb')
                backup.parent.mkdir(parents=True, exist_ok=True)
                conn.close()
                shutil.copy2(settings.paths.db, backup)
                conn = db.connect(settings.paths.db)
                report['backup'] = str(backup)
                has_history = conn.execute("SELECT count(*) FROM information_schema.tables"
                                           " WHERE table_name='fct_listing_revision'").fetchone()[0]
                conn.execute('BEGIN TRANSACTION')
                try:
                    for (listing, stamp), (count, features) in changes.items():
                        if has_history and stamp == latest[listing]:
                            C.record(conn, listing, 'baseline', stamp)
                        conn.execute('UPDATE fct_listing_snapshot SET five_star_weapons=?,extracted_features=?'
                                     ' WHERE listing_id=? AND snapshot_at=?',
                                     [count, json.dumps(features, ensure_ascii=False), listing, stamp])
                        if has_history and stamp == latest[listing]:
                            C.record(conn, listing, 'weapon-information-replay', stamp)
                            C.complete_retry_state(conn, listing)
                    conn.execute('COMMIT')
                except Exception:
                    conn.execute('ROLLBACK')
                    raise
            if apply:
                report['analysis_sync'] = A.sync_game_csv(settings, conn, game_id=10302)
            return report
        finally:
            conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(repair(config.load_settings(), apply=args.apply), ensure_ascii=False, indent=2))
