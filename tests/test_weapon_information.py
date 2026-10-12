"""区分未采到与详情未公示；截图文字构造的离线用例，不冒充真实 DOM。"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pxb7 import backfill as B, collection as C, db, gateway as G, parser as P, weapons as W
from tests.test_listing_attributes import state, post, card_html
from tests.test_backfill import row


SCREENSHOT_TITLE = ('【MMHFW8204】48级，1黄，星声：67696，余波珊瑚：12，'
                    '1个五星角色：维里奈，6个四星角色：秧秧，炽霞，散华，白芷，秋水，渊武；'
                    '详情看图【官服】【官方截图】')


def detail(title, extra=''):
    return f'<div class="product-detail"><h1>{title}</h1>{extra}<div>1人已收藏</div></div>'


def test_screenshot_unreported_is_not_fabricated_zero(state):
    post(state, 'cards', card_html(SCREENSHOT_TITLE))
    result = post(state, 'detail', detail(SCREENSHOT_TITLE))
    assert result['weapon_information']['state'] == 'unreported'
    assert 'five_star_weapons' not in result['attributes']
    assert result['weapon_details_complete']
    assert not any(m['key'].startswith('five_star_weapon') for m in result['missing'])
    data = G.handle_listings(state, {'game_id': ['10302']})
    assert data['rows'][0]['weapon_information']['state'] == 'unreported'
    assert data['rows'][0]['cells']['five_star_weapons'] is None
    restarted = G.GatewayState(state.settings, state.tasks, default_task_id='wuwa_official')
    assert G.handle_listings(restarted, {})['rows'][0]['weapon_information']['state'] == 'unreported'
    # 标记持久化，重启仍然保留；相同提交不追加修订。
    with db.connect(state.settings.paths.db) as conn:
        count = conn.execute('SELECT count(*) FROM fct_listing_revision').fetchone()[0]
        assert W.status(None, C.current_data(conn, '123456')['extracted_features'])['state'] == 'unreported'
    post(state, 'detail', detail(SCREENSHOT_TITLE))
    with db.connect(state.settings.paths.db) as conn:
        assert conn.execute('SELECT count(*) FROM fct_listing_revision').fetchone()[0] == count


@pytest.mark.parametrize('title,extra', [
    ('48级，1个五星角色：维里奈…', ''),
    ('48级，1个五星角色：维里奈', ''),
    (SCREENSHOT_TITLE, '<div>五星武器：加载失败</div>'),
    (SCREENSHOT_TITLE.replace('详情看图', '五星武器：？？；详情看图'), ''),
])
def test_unready_or_unparseable_still_missing(state, title, extra):
    post(state, 'cards', card_html(title))
    result = post(state, 'detail', detail(title, extra))
    assert result['weapon_information']['state'] == 'unknown'
    assert not result['weapon_details_complete']
    assert any(m['key'] == 'five_star_weapons' for m in result['missing'])


@pytest.mark.parametrize('wording', ['无五星武器', '五星武器：无', '0个五星武器'])
def test_explicit_none_has_zero_and_refinement_not_applicable(state, wording):
    title = f'48级，1黄，1个五星角色：维里奈；{wording}；详情看图【官方截图】'
    post(state, 'cards', card_html(title))
    result = post(state, 'detail', detail(title))
    assert result['attributes']['five_star_weapons'] == 0
    assert result['weapon_information']['state'] == 'none'
    assert result['weapon_details_complete']
    assert not any(m['key'].startswith('five_star_weapon') for m in result['missing'])


def test_no_report_does_not_override_existing_weapons_or_missing_refinement(state):
    post(state, 'cards', card_html('48级，2黄，1个五星角色：维里奈；1个五星武器'))
    result = post(state, 'detail', detail(SCREENSHOT_TITLE))
    assert result['weapon_information']['state'] == 'present'
    assert not result['weapon_details_complete']
    assert any(m['key'] == 'five_star_weapon_refinements' for m in result['missing'])


def test_missing_uses_observation_but_only_for_wuwa():
    marker = {W.FEATURE: {'state': 'unreported', 'source': 'detail', 'reason': '完整详情未列出武器'}}
    assert not B.missing(row(five_star_weapons=None, features=marker))
    assert B.missing(row(game_id=10026, five_star_weapons=None, features=marker))
    assert B.missing(row(five_star_weapons=1, features=marker))
    assert B.missing(row(five_star_weapons=None, features=marker | {'five_star_weapon_refined': 3}))
    assert B.missing(row(five_star_weapons=None))


def test_negative_phrase_does_not_become_zero():
    assert 'five_star_weapons' not in P.parse_detail_attributes(detail('并非无五星武器；详情看图'))[1]


def test_recommendations_cannot_supply_or_block_empty_observation(state):
    post(state, 'cards', card_html(SCREENSHOT_TITLE))
    result = post(state, 'detail', detail(SCREENSHOT_TITLE,
        '<div class="smallCard" productid="999999">1个五星武器：精5千古洑流</div>'))
    assert result['weapon_information']['state'] == 'unreported'


def test_unreported_later_becomes_actual_weapons_with_history(state):
    post(state, 'cards', card_html(SCREENSHOT_TITLE))
    post(state, 'detail', detail(SCREENSHOT_TITLE))
    title = SCREENSHOT_TITLE.replace('详情看图', '1个五星武器：精1千古洑流；详情看图')
    result = post(state, 'detail', detail(title))
    assert result['weapon_information']['state'] == 'present'
    assert result['attributes']['five_star_weapons'] == 1
    assert result['weapon_details_complete']
    with db.connect(state.settings.paths.db) as conn:
        versions = conn.execute('SELECT data_json FROM fct_listing_revision ORDER BY version').fetchall()
    assert any(json.loads(r[0])['extracted_features'].get(W.FEATURE, {}).get('state') == 'unreported'
               for r in versions)


def test_curated_csv_distinguishes_both_empty_cases_and_failure():
    from pxb7 import analysis as A
    from tests.test_curated_csv import _bare_row
    marker = {W.FEATURE: {'state': 'unreported', 'source': 'detail'}}
    empty = A._fmt_wuwa_cells(_bare_row(extracted_features=json.dumps(marker)))
    assert empty[6] == empty[8] == '已采集·页面未提供'
    absent = A._fmt_wuwa_cells(_bare_row(five_star_weapons=0))
    assert absent[6] == 0 and absent[8] == '不适用（无五星武器）'
    unknown = A._fmt_wuwa_cells(_bare_row())
    assert unknown[6] == unknown[8] == '未采到／解析失败'


def test_saved_dom_repair_only_weapon_status_and_is_idempotent(state):
    from tools.repair_weapon_information import repair
    post(state, 'cards', card_html(SCREENSHOT_TITLE))
    post(state, 'detail', detail(SCREENSHOT_TITLE))
    with db.connect(state.settings.paths.db) as conn:
        features = G._parse_features(conn.execute('SELECT extracted_features FROM fct_listing_snapshot').fetchone()[0])
        features.pop(W.FEATURE)
        conn.execute('UPDATE fct_listing_snapshot SET extracted_features=?', [json.dumps(features)])
        protected = conn.execute('SELECT * EXCLUDE(extracted_features) FROM fct_listing_snapshot').fetchall()
    assert repair(state.settings)['changed_snapshots'] == 1
    report = repair(state.settings, apply=True)
    assert report['changed_snapshots'] == 1 and report['backup'] and report['analysis_sync']['ok']
    assert repair(state.settings)['changed_snapshots'] == 0
    with db.connect(state.settings.paths.db) as conn:
        assert conn.execute('SELECT * EXCLUDE(extracted_features) FROM fct_listing_snapshot').fetchall() == protected
        updated = G._parse_features(conn.execute('SELECT extracted_features FROM fct_listing_snapshot').fetchone()[0])
        assert {k: v for k, v in updated.items() if k != W.FEATURE} == features


def test_content_detail_weapon_markers_node():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, 'tests/content_weapon_information.cjs'], cwd=root,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('later_detail', [False, True])
def test_replay_old_zero_never_overwrites_later_known_weapon(state, later_detail):
    import datetime as dt
    from tools.repair_weapon_information import repair
    title = '48级，2黄；1个五星角色：维里奈；1个五星武器：精1千古洑流；详情看图'
    result = post(state, 'cards', card_html(title))
    stamp = dt.datetime.fromisoformat(result['round'])
    root = state.settings.paths.raw_root / 'offline-observation-fixture'
    root.mkdir(parents=True)
    texts = ['48级；1个五星角色：维里奈；0个五星武器；详情看图'] + ([title] if later_detail else [])
    for index, text in enumerate(texts, 1):
        path = root / f'detail_{index}.json'
        path.write_text(json.dumps({'task_id': 'wuwa_official', 'listing_id': '123456',
            'collected_at': (stamp + dt.timedelta(seconds=index)).isoformat()}), encoding='utf-8')
        path.with_suffix('.html').write_text(detail(text), encoding='utf-8')
    with db.connect(state.settings.paths.db) as conn:
        before = conn.execute('SELECT * EXCLUDE(extracted_features) FROM fct_listing_snapshot').fetchall()
    assert repair(state.settings, apply=True)['explicit_zero_updates'] == 0
    with db.connect(state.settings.paths.db) as conn:
        assert conn.execute('SELECT * EXCLUDE(extracted_features) FROM fct_listing_snapshot').fetchall() == before
