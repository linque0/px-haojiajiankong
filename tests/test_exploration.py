"""地图采集、可选检测、估算、快照回填和 CSV 原子同步；全用临时库。"""
import csv
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from pxb7 import analysis as A, backfill as B, db, exploration as E, gateway as G
from tests.test_gateway import settings, state, _post, CARDS_PAYLOAD, DETAIL_PAYLOAD
from tests.test_backfill import row as complete_row
from tests.test_wuwa_valuation import v, row

ROOT = Path(__file__).parents[1]
HTML = (ROOT / 'tests/fixtures/wuwa_map_exploration_20261007.html').read_text(encoding='utf-8')
sys.path.insert(0, str(ROOT / 'tools'))
import repair_wuwa_exploration as repair
import wuwa_exploration_valuation as model


def config():
    return {'version': 'test', 'aliases': {'别名': '子地图'}, 'maps': {
        '父地图': {'resources': {'astrite': 9000}},
        '子地图': {'parent': '父地图', 'resources': {'astrite': 2000}}, '未知图': {'resources': {}}}}


def test_parse_real_structure_and_optional_field():
    assert E.parse_html(HTML) == [{'name': '云陵谷', 'progress': 0},
        {'name': '中曲台地', 'progress': 60.5}, {'name': '梦枢天罗', 'progress': 100}]
    assert E.parse_html('<div>云陵谷 探索度80%</div>') == []
    assert B.missing(complete_row(features={E.FEATURE: None})) == []
    assert B.missing(complete_row(features={E.FEATURE: [{'name': '未知', 'progress': 0}]})) == []


@pytest.mark.parametrize('progress,remaining', [(0, 2000), (60, 800), (60.5, 790), (100, 0)])
def test_formula_and_astrite_only(progress, remaining):
    result = E.estimate([{'name': '子地图', 'progress': progress}], config())
    assert result['remaining']['astrite'] == remaining
    assert result['remaining_astrite_pulls'] == remaining / 160
    assert set(result['remaining']) == {'astrite'} and 'radiant_tide' not in result['remaining']
    text = E.resource_text(result['remaining'])
    assert text == f'星声：约{remaining:g}'
    assert '波纹' not in text and '涡纹' not in text
    assert result['partial']


def test_unknown_invalid_alias_conflict_and_parent_child():
    result = E.estimate([{'name': '父地图', 'progress': 0}, {'name': '别名', 'progress': 60},
                         {'name': '子地图', 'progress': 60}, {'name': '未登记地图', 'progress': 100}], config())
    assert len(result['maps']) == 2 and result['remaining']['astrite'] == 800
    assert result['maps'][1]['remaining']['astrite'] is None
    assert result['coverage']['astrite'] == 1 / 3
    assert E.normalize([{'name': '别名', 'progress': 60}, {'name': '子地图', 'progress': 61}], config())[0]['progress'] is None
    for invalid in [-1, 101, float('nan'), True]:
        assert E.estimate([{'name': '子地图', 'progress': invalid}], config())['remaining']['astrite'] is None
    assert E.estimate([], config())['remaining_astrite_pulls'] is None


def seed_wuwa(state):
    state.config['targets'] = ['wuwa_official']
    _post(state, '/ingest/cards', {**CARDS_PAYLOAD, 'url': 'https://www.pxb7.com/buy/10302/1'})
    with db.connect(state.settings.paths.db) as conn:
        return conn.execute('SELECT listing_id,snapshot_at FROM fct_listing_snapshot ORDER BY listing_id').fetchone()


def test_ingest_round_preservation_list_protection_and_read_api(state):
    listing, stamp = seed_wuwa(state)
    with db.connect(state.settings.paths.db) as conn:
        history = stamp - dt.timedelta(minutes=30)
        conn.execute('INSERT INTO fct_listing_snapshot SELECT * REPLACE (? AS snapshot_at) FROM fct_listing_snapshot WHERE listing_id=?', [history, listing])
        protected = conn.execute('SELECT listing_id,snapshot_at,price_yuan FROM fct_listing_snapshot ORDER BY 1,2').fetchall()
    status, ack = _post(state, '/ingest/detail', {'url': f'https://www.pxb7.com/product/{listing}/1',
             'snapshot_round': stamp.isoformat(), 'html': HTML})
    assert status == 200 and len(ack['map_exploration']) == 3 and ack['analysis_sync']['ok']
    _post(state, '/ingest/cards', {**CARDS_PAYLOAD, 'url': 'https://www.pxb7.com/buy/10302/1'})
    with db.connect(state.settings.paths.db) as conn:
        assert protected == conn.execute('SELECT listing_id,snapshot_at,price_yuan FROM fct_listing_snapshot ORDER BY 1,2').fetchall()
        assert E.FEATURE not in json.loads(conn.execute('SELECT extracted_features FROM fct_listing_snapshot WHERE listing_id=? AND snapshot_at=?', [listing, history]).fetchone()[0])
        current = json.loads(conn.execute('SELECT extracted_features FROM fct_listing_snapshot WHERE listing_id=? AND snapshot_at=?', [listing, stamp]).fetchone()[0])
        assert current[E.FEATURE][0]['progress'] == 0
    data = G.handle_listings(state, {'game_id': '10302'})
    assert any(r['cells']['map_exploration'] and r['cells']['map_exploration']['partial'] for r in data['rows'])
    with Path(ack['analysis_sync']['path']).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames[-2:] == ['地图探索度', '地图预计剩余资源']
        exported = next(r for r in reader if r['listing_id'] == listing)
        assert '云陵谷：0%' in exported['地图探索度']
        assert '星声：约' in exported['地图预计剩余资源'] and '波纹' not in exported['地图预计剩余资源']


def test_repair_backup_exact_matching_idempotence_and_failed_sync_retry(state, monkeypatch):
    listing, stamp = seed_wuwa(state)
    folder = state.settings.paths.raw_root / 'maps'
    folder.mkdir(parents=True)
    page = folder / f'detail_{listing}.html'
    page.write_text(HTML, encoding='utf-8')
    meta = {'listing_id': listing, 'url': f'https://www.pxb7.com/product/{listing}/1',
            'collected_at': (stamp + dt.timedelta(days=1)).isoformat(), 'snapshot_round': stamp.isoformat()}
    page.with_suffix('.json').write_text(json.dumps(meta), encoding='utf-8')
    with db.connect(state.settings.paths.db) as conn:
        first = A.sync_game_csv(state.settings, conn, game_id=10302)
    csv_path = Path(first['path'])
    old_csv = csv_path.read_bytes()
    before = repair.repair(state.settings)
    assert before['changed_snapshots'] == 1
    real_replace = Path.replace
    monkeypatch.setattr(Path, 'replace', lambda *a, **k: (_ for _ in ()).throw(PermissionError('CSV in use')))
    result = repair.repair(state.settings, apply=True)
    assert Path(result['backup']).exists() and not result['analysis_sync']['ok']
    assert result['protected_sha256_before'] == result['protected_sha256_after']
    assert csv_path.read_bytes() == old_csv
    assert not G.build_stats_payload(state)['csv_sync_status']['10302']['ok']
    monkeypatch.setattr(Path, 'replace', real_replace)
    assert B.sync(state, {'game_ids': [10302]})['results'][0]['ok']
    assert G.build_stats_payload(state)['csv_sync_status']['10302']['ok']
    assert '云陵谷：0%' in csv_path.read_text(encoding='utf-8-sig')
    assert repair.repair(state.settings, apply=True)['changed_snapshots'] == 0
    # 取消固定轮次标记：跨天的旧页不得套用到最新行。
    meta.pop('snapshot_round')
    page.with_suffix('.json').write_text(json.dumps(meta), encoding='utf-8')
    assert repair.repair(state.settings)['matched_detail_pages'] == 0


def test_repair_backup_failure_does_not_write(state, monkeypatch):
    listing, stamp = seed_wuwa(state)
    folder = state.settings.paths.raw_root / 'maps'
    folder.mkdir(parents=True)
    (folder / f'detail_{listing}.html').write_text(HTML, encoding='utf-8')
    (folder / f'detail_{listing}.json').write_text(json.dumps({'listing_id': listing,
            'collected_at': stamp.isoformat(), 'snapshot_round': stamp.isoformat()}), encoding='utf-8')
    monkeypatch.setattr(repair.shutil, 'copy2', lambda *a: (_ for _ in ()).throw(PermissionError('backup failed')))
    with pytest.raises(PermissionError):
        repair.repair(state.settings, apply=True)
    assert repair.repair(state.settings)['changed_snapshots'] == 1


def test_map_model_direct_remaining_features_missing_and_balance_separation():
    before = v.parse_row(row())
    after = v.parse_row(row(**{'地图探索度': '子地图：60%'}), config())
    assert after['features']['map_remaining_astrite_pulls'] == 5
    assert after['features']['observed_flexible_pulls'] == before['features']['observed_flexible_pulls']
    assert before['features']['map_remaining_astrite_pulls'] is None
    assert before['eligible'] and after['eligible']
    assert before['fingerprint'] == after['fingerprint']
    assert 'map_remaining_astrite_pulls' in v.feature_keys([after], 'exploration')
    assert 'map_remaining_astrite_pulls' not in v.feature_keys([after], 'structure')
    fitted = {'family':'exploration', 'intercept':5., 'keys':['map_remaining_astrite_pulls'],
              'medians':[0], 'mean':[0], 'scale':[1], 'coef':[.2]}
    assert v.map_contribution(after, fitted) == pytest.approx(.2 * __import__('math').log1p(5))
    full = v.parse_row(row(**{'地图探索度':'子地图：100%'}), config())
    assert v.predict_log(after, fitted) > v.predict_log(full, fitted)
    assert v.parse_row(row(**{'价格 ¥':'99999','地图探索度':'子地图：60%'}), config())['features'] == after['features']


def test_near_config_groups_and_promotion_thresholds():
    records = [v.parse_row(row(listing_id=str(i), **{'资源': f'星声：{i * 160}', '等级': str(75+i%5)})) for i in range(10)]
    assert len({model.group_key(r) for r in records}) == 1
    assert sum(bool(f) for f in model.group_folds(records, 5, 42)) == 1
    mapped = [v.parse_row(row(listing_id=str(i), **{'共鸣链（N命）': f'0命角色{i}', '地图探索度':'云陵谷：60%'})) for i in range(70)]
    old = {'median_ape': .20, 'mae_yuan': 100}
    assert model.promotion_gate(mapped[:50], mapped[50:], old, {'median_ape':.17,'mae_yuan':99})['promoted']
    assert not model.promotion_gate(mapped[:49], mapped[50:], old, {'median_ape':.17,'mae_yuan':99})['promoted']
    assert not model.promotion_gate(mapped[:50], mapped[50:], old, {'median_ape':.19,'mae_yuan':99})['promoted']
    assert not model.promotion_gate(mapped[:50], mapped[50:], old, {'median_ape':.17,'mae_yuan':101})['promoted']
