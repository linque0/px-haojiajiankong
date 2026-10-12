"""仅从可匹配快照的已保存鸣潮详情回填地图；默认预览，--apply 先备份。

应用前停止网关；不访问商品页，不改轮次/价格/余额/其他资产。
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from pxb7 import config, db, exploration as E, gateway as G, pipeline as PL
from pxb7.analysis import sync_game_csv


def protected_digest(conn):
    """除地图及其来源外的每个快照列、其他 JSON 资产均参与完整性校验。"""
    names = [x[0] for x in conn.execute('SELECT * FROM fct_listing_snapshot LIMIT 0').description]
    feature_index = names.index('extracted_features')
    rows = conn.execute('SELECT * FROM fct_listing_snapshot ORDER BY listing_id,snapshot_at').fetchall()
    digest = hashlib.sha256()
    for row in rows:
        row = list(row)
        features = json.loads(row[feature_index] or '{}')
        features.pop(E.FEATURE, None)
        features.pop('_wuwa_map_source', None)
        row[feature_index] = features
        digest.update(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str).encode())
    return len(rows), digest.hexdigest()


def repair(settings, *, apply=False):
    with PL.run_lock(settings):
        conn = db.connect(settings.paths.db, read_only=not apply)
        try:
            original = {(r[0], r[1]): json.loads(r[2] or '{}') for r in conn.execute(
                'SELECT s.listing_id,s.snapshot_at,s.extracted_features FROM fct_listing_snapshot s'
                ' JOIN dim_listing l USING(listing_id) WHERE l.game_id=10302').fetchall()}
            sources, skipped = [], 0
            for path in settings.paths.raw_root.rglob('detail_*.json'):
                try:
                    meta = json.loads(path.read_text(encoding='utf-8'))
                    listing = str(meta.get('listing_id') or '')
                    if not listing or path.stem != 'detail_' + listing or not path.with_suffix('.html').exists():
                        continue
                    at = dt.datetime.fromisoformat(meta['collected_at'])
                    # 无固定快照标记的旧页只允许同一轮，不把跨轮页面套到最近记录。
                    stamp = dt.datetime.fromisoformat(meta['snapshot_round']) if meta.get('snapshot_round') else db.truncate_to_round(at, settings.snapshot_round_minutes)
                    key = (listing, stamp)
                    if key not in original:
                        continue
                    if meta.get('url') and meta['url'].split('#')[0] != f'https://www.pxb7.com/product/{listing}/1':
                        skipped += 1
                        continue
                    html = path.with_suffix('.html').read_text(encoding='utf-8')
                    if G._reject_risk_page(html):
                        skipped += 1
                        continue
                    entries = E.parse_html(html)
                    if entries:
                        sources.append((at, str(path), key, entries, {
                            'collected_at': at.isoformat(), 'raw': str(path.with_suffix('.html').relative_to(settings.paths.raw_root)),
                            'sha256': hashlib.sha256(html.encode()).hexdigest()}))
                except (OSError, ValueError, KeyError):
                    skipped += 1
            changes = {}
            for at, path, key, entries, source in sorted(sources):
                old = changes.get(key, original[key])
                current_source = old.get('_wuwa_map_source', {})
                if current_source.get('collected_at', '') > at.isoformat():
                    continue
                changes[key] = {**old, E.FEATURE: entries, '_wuwa_map_source': source}
            changes = {key: value for key, value in changes.items() if value != original[key]}
            before = protected_digest(conn)
            report = {'mode': 'apply' if apply else 'preview', 'snapshots': before[0],
                      'wuwa_snapshots': len(original), 'matched_detail_pages': len(sources),
                      'skipped_invalid_pages': skipped, 'changed_snapshots': len(changes),
                      'protected_sha256_before': before[1]}
            if apply and changes:
                conn.execute('CHECKPOINT')
                backup = settings.paths.db.parent / 'backups' / ('maps-' + dt.datetime.now().strftime('%Y%m%dT%H%M%S%f') + '.duckdb')
                backup.parent.mkdir(parents=True, exist_ok=True)
                conn.close()
                shutil.copy2(settings.paths.db, backup)
                if backup.stat().st_size != settings.paths.db.stat().st_size:
                    raise OSError('数据库备份不完整，停止回填')
                conn = db.connect(settings.paths.db)
                report['backup'] = str(backup)
                conn.execute('BEGIN TRANSACTION')
                try:
                    for (listing, stamp), features in changes.items():
                        conn.execute('UPDATE fct_listing_snapshot SET extracted_features=? WHERE listing_id=? AND snapshot_at=?',
                                     [json.dumps(features, ensure_ascii=False), listing, stamp])
                    if protected_digest(conn) != before:
                        raise RuntimeError('非地图字段或快照行数发生变化，回滚')
                    conn.execute('COMMIT')
                except Exception:
                    conn.execute('ROLLBACK')
                    raise
            report['protected_sha256_after'] = protected_digest(conn)[1]
            if apply:
                # 幂等运行也可重试原 CSV；文件占用不回滚已完成的地图入库。
                report['analysis_sync'] = sync_game_csv(settings, conn, game_id=10302)
            return report
        finally:
            conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(repair(config.load_settings(), apply=args.apply), ensure_ascii=False, indent=2))
