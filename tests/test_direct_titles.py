"""不悬浮采集：真实标题入库，执行 MAIN 网络桥与隔离世界采集器。"""
import shutil
import subprocess
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from pxb7 import parser as P, gateway as G
from tests.test_listing_attributes import state, post

ROOT = Path(__file__).resolve().parents[1]


def direct_html():
    soup = BeautifulSoup((ROOT / 'tests/fixtures/real_hover_titles_pxb7_20261004.html').read_text(encoding='utf8'), 'html.parser')
    titles = [node.get_text('', strip=True) for node in soup.select('.longTitle > div:not(.more)')]
    for card, title in zip(soup.select('.middleCard'), titles):
        card['data-pxb7-full-title'] = title
        card['data-pxb7-full-title-id'] = card['productid']
    for node in soup.select('.t-popup'):
        node.decompose()
    return str(soup)


def test_direct_titles_ingest_and_survive_short_reingest(state):
    html = direct_html()
    parsed = P.parse_list_page(html)
    assert all(card.hits['title'] == 'L1-title-api' for card in parsed.cards)
    assert sorted(card.fields['five_star_weapons'] for card in parsed.cards) == [9, 21]
    post(state, 'cards', html)
    soup = BeautifulSoup(html, 'html.parser')
    for card in soup.select('.middleCard'):
        del card['data-pxb7-full-title']
        del card['data-pxb7-full-title-id']
    post(state, 'cards', str(soup))
    rows = G.handle_listings(state, {'game_id':['10302']})['rows']
    assert sorted(len(row['cells']['five_star_weapon_refined']) for row in rows) == [9, 21]
    from tools.repair_listing_fields import repair
    assert repair(state.settings)['changed_snapshots'] == 0


def test_wrong_id_or_prefix_cannot_override_card():
    soup = BeautifulSoup(direct_html(), 'html.parser')
    cards = soup.select('.middleCard')
    cards[0]['data-pxb7-full-title-id'] = cards[1]['productid']
    cards[1]['data-pxb7-full-title'] = '其他商品：99个五星武器'
    assert all(card.fields.get('five_star_weapons') is None for card in P.parse_list_page(str(soup)).cards)


def test_network_bridge_and_collector_behavior():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    result = subprocess.run([node, str(ROOT / 'tests/direct_titles.cjs'), str(ROOT)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_title_failure_diagnostics_saved_without_arbitrary_data(state):
    import json
    status, result = G.handle_ingest(state, '/ingest/cards', json.dumps({
        'url':'https://www.pxb7.com/buy/10302/1', 'html':direct_html(),
        'title_collection':{'cards':16, 'captured':8, 'requested':11, 'failed':3,
            'attempts':14, 'retried':3, 'stop':'unavailable',
            'errors':[{'id':'2429986113209160805', 'error':'network-error', 'attempts':2,
                       'authorization':'must-not-be-saved'}, {'id':'bad', 'error':'http-error'},
                      {'id':'2429984283689727689', 'error':'http-error', 'status':503}]}
    }).encode())
    assert status == 200, result
    meta_path = next(state.settings.paths.raw_root.rglob('list_p01.json'))
    meta = json.loads(meta_path.read_text(encoding='utf8'))
    stats = meta['title_collection']
    assert stats['attempts'] == 14 and stats['retried'] == 3
    assert len(stats['errors']) == 2
    assert stats['errors'][1]['status'] == 503
    assert 'authorization' not in json.dumps(stats)


def test_actual_partial_batch_is_acquisition_missing_not_parser_missing():
    html = (ROOT / 'tests/fixtures/partial_titles_pxb7_20261004.html').read_text(encoding='utf8')
    result = P.parse_list_page(html)
    assert result.cards_seen == 16
    full = [card for card in result.cards if card.hits['title'] == 'L1-title-api']
    assert len(full) == 8 and all(card.fields['five_star_weapons'] is not None for card in full)
    missing = {card.listing_id for card in result.cards if card.fields.get('five_star_weapons') is None}
    assert len(missing) == 8
    assert {'2429986113209160805','2429984283689727689','2429983906202538949','2429983658967469651'} <= missing
