"""列表悬浮全文：真实样本入库、跨卡片隔离及扩展有限等待。"""
import shutil
import subprocess
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from pxb7 import gateway as G, parser as P
from tests.test_listing_attributes import state, post, card_html, TITLE

ROOT = Path(__file__).resolve().parents[1]
REAL = (ROOT / 'tests/fixtures/real_hover_titles_pxb7_20261004.html').read_text(encoding='utf8')


def test_real_hidden_and_visible_tooltips_reach_listing_rows(state):
    post(state, 'cards', REAL)
    rows = {r['listing_id']: r for r in G.handle_listings(state, {'game_id':['10302']})['rows']}
    for listing_id, weapons, chars in [('2429861601854109738',9,13), ('2429860888248456079',21,23)]:
        cells = rows[listing_id]['cells']
        assert cells['five_star_weapons'] == weapons
        assert cells['five_star_chars'] == chars
        assert len(cells['five_star_weapon_refined']) == weapons
    assert rows['2429861601854109738']['cells']['five_star_weapon_refined'][0] == {'name':'千古洑流','value':2}
    # 关闭的悬浮节点可重放；优惠券未重新进入价格/账号字段。
    assert rows['2429861601854109738']['price'] == 600


def test_conflicting_or_ambiguous_popups_are_not_assigned():
    def popup(text):
        return f'<div class="t-popup" style="display:none"><div class="longTitle"><div>{text}</div><div class="more">点击查看更多</div></div></div>'
    one = card_html() + popup(TITLE)
    assert P.parse_list_page(one).cards[0].fields['five_star_weapons'] == 15
    ambiguous = one + card_html().replace('123456', '234567')
    assert all(c.fields.get('five_star_weapons') is None for c in P.parse_list_page(ambiguous).cards)
    conflicting = one + popup(TITLE.replace('15个五星武器', '16个五星武器'))
    assert P.parse_list_page(conflicting).cards[0].fields.get('five_star_weapons') is None


def test_short_reingest_keeps_full_list_rosters(state):
    soup = BeautifulSoup(REAL, 'html.parser')
    post(state, 'cards', REAL)
    for popup in soup.select('.t-popup'):
        popup.decompose()
    post(state, 'cards', str(soup))
    rows = G.handle_listings(state, {'game_id':['10302']})['rows']
    assert sorted(len(r['cells']['five_star_weapon_refined']) for r in rows) == [9,21]
    assert sorted(r['cells']['five_star_weapons'] for r in rows) == [9,21]
    from tools.repair_listing_fields import repair
    assert repair(state.settings)['changed_snapshots'] == 0


def test_new_full_title_can_remove_old_weapons(state):
    post(state, 'cards', REAL)
    soup = BeautifulSoup(REAL, 'html.parser')
    title = soup.select_one('.longTitle > div:not(.more)')
    text = title.get_text().split('9个五星武器')[0] + '0个五星武器；详情看图'
    title.clear()
    title.append(text)
    post(state, 'cards', str(soup))
    row = next(r for r in G.handle_listings(state, {'game_id':['10302']})['rows']
               if r['listing_id'] == '2429861601854109738')
    assert row['cells']['five_star_weapons'] == 0
    assert row['cells']['five_star_weapon_refined'] is None


