"""字段采集到分页展示的回归：标签串号、四星混入、详情污染及重采覆盖。"""
import dataclasses
import json

import pytest

from pxb7 import config as cfg, db, extract as X, gateway as G, parser as P


TITLE = "80级，40黄；19个五星角色：3命弗洛洛,2命安可,1命鉴心；12个四星角色：满命散华；15个五星武器：精1千古洑流,精3星序协响,精1千古洑流"
CARD_TITLE = TITLE.split("；15个")[0]


def card_html(title=CARD_TITLE, favorites=0):
    return (f'<a href="/product/123456/1" data-listing-id="123456">'
            f'<div price="24800" productname="{title}" collectcount="{favorites}">{title}</div>'
            '<span>官服 未绑定TAP 找回包赔</span></a>')


def detail_html():
    # 站点把完整标题逐字放入 span；推荐卡片的等级/武器不能污染主商品。
    title = ''.join(f'<span>{c}</span>' for c in TITLE)
    return ('<div class="product-detail"><a href="/buy/10302/1">鸣潮</a>'
            f'<div class="line-clamp-5">{title}</div><div>1人已收藏</div>'
            '<div>角色 Lv.90</div><div class="smallCard" productid="999999">'
            '<h1>60级，99个五星武器：精5推荐武器</h1></div></div>')


@pytest.fixture
def state(tmp_path):
    base = cfg.load_settings()
    settings = dataclasses.replace(base, paths=dataclasses.replace(
        base.paths, db=tmp_path / 'fields.duckdb', raw_root=tmp_path / 'raw',
        state_dir=tmp_path / 'state', runs=tmp_path / 'runs', risk_state=tmp_path / 'risk.json'))
    db.init_db(settings.paths.db)
    with db.connect(settings.paths.db) as conn:
        db.upsert_dim_keywords(conn, X.seed_db_rows())
    return G.GatewayState(settings, cfg.load_tasks(settings=settings).tasks,
                          default_task_id='wuwa_official')


def post(state, kind, html):
    url = 'https://www.pxb7.com/' + ('buy/10302/1' if kind == 'cards' else 'product/123456/1')
    status, result = G.handle_ingest(state, '/ingest/' + kind,
                                   json.dumps({'url': url, 'html': html}).encode())
    assert status == 200, result
    return result


@pytest.mark.parametrize('title,chars,weapons', [
    ('等级60 黄数41 五星角色 21 五星武器 6', 21, 6),
    ('80级，40黄，19个五星角色：3命弗洛洛；15个五星武器：精1千古洑流', 19, 15),
    ('80级 19个五星角色：3命弗洛洛', 19, None),
])
def test_card_counts_do_not_read_chain_or_adjacent_count(title, chars, weapons):
    card = P.parse_card(card_html(title))
    assert card.fields['five_star_chars'] == chars
    assert card.fields.get('five_star_weapons') == weapons
    assert card.fields['favorites_cnt'] == 0
    assert card.fields['level'] in (60, 80)


def test_roster_keeps_names_duplicates_and_excludes_four_stars():
    result = X.extract_listing(X.seed_keywords(game_id=10302), listing_id='123456', title=TITLE)
    assert result.features['constellation_cnt'] == 3
    assert [r['name'] for r in result.features['five_star_character_chains']] == ['弗洛洛', '安可', '鉴心']
    assert [r['value'] for r in result.features['five_star_weapon_refinements']] == [1, 3, 1]
    assert result.features['five_star_weapon_refined'] == 3


def test_roster_unannotated_characters_are_zero_chain():
    # 2026-10-04 用户规则：段内未标注 N命 的角色视为 0命，同样进共鸣链列表并保持原文顺序
    result = X.extract_listing(X.seed_keywords(game_id=10302), listing_id='123456',
                               title='80级，20黄，19个五星角色：3命弗洛洛，2命安可，鉴心；15个五星武器：精1千古洑流')
    chains = result.features['five_star_character_chains']
    assert [r['name'] for r in chains] == ['弗洛洛', '安可', '鉴心']
    assert [r['value'] for r in chains] == [3, 2, 0]
    assert result.features['constellation_cnt'] == 3
    assert result.features['five_star_weapon_refined'] == 1
    # 武器段不猜精0：无精炼标注的武器不进列表
    assert [r['name'] for r in result.features['five_star_weapon_refinements']] == ['千古洑流']


def test_roster_all_unannotated_characters_are_zero_chain():
    result = X.extract_listing(X.seed_keywords(game_id=10302), listing_id='123456',
                               title='80级，20黄，2个五星角色：鉴心，维里奈')
    chains = result.features['five_star_character_chains']
    assert [r['value'] for r in chains] == [0, 0]
    assert result.features['constellation_cnt'] == 0


def test_roster_zero_chain_rejects_noise_and_truncated_chain():
    # 孤立「3命」（被截断的具名条目）不冒充 0命；【官方截图】等尾随杂质从名字剔除
    result = X.extract_listing(X.seed_keywords(game_id=10302), listing_id='123456',
                               title='80级，2个五星角色：3命，鉴心【官方截图】，15个五星武器：精1千古洑流')
    chains = result.features['five_star_character_chains']
    assert [r['name'] for r in chains] == ['鉴心']
    assert [r['value'] for r in chains] == [0]


def test_card_title_without_attribute_keeps_weapon_section():
    title = '80级，25个五星角色：' + '满命维里奈，' * 30 + '17个五星武器：精1千古洑流'
    html = f'<a href="/product/123456/1"><div class="smallCardTitle">{title}</div></a>'
    assert P.parse_card(html).title == title


def test_optional_columns_use_all_filtered_rows_and_preserve_real_zero(state):
    post(state, 'cards', card_html())
    with db.connect(state.settings.paths.db) as conn:
        conn.execute("UPDATE fct_listing_snapshot SET mail_status=NULL, viewers_masked=NULL, "
                     "publish_time_text='2026-10-03 20:58:40'")
    page = G.handle_listings(state, {'game_id': ['10302']})
    assert not page['show_viewers']
    assert 'mail_status' not in {c['key'] for c in page['columns']}
    assert page['rows'][0]['cells']['published_at'] == '2026-10-03 20:58:40'
    # 即使翻到空页，列仍依据整个游戏；真实的浏览0不当作没有信息。
    with db.connect(state.settings.paths.db) as conn:
        conn.execute("UPDATE fct_listing_snapshot SET viewers_masked=0, mail_status='未绑定'")
    empty = G.handle_listings(state, {'game_id': ['10302'], 'offset': ['20']})
    assert empty['rows'] == [] and empty['show_viewers']
    assert {'published_at', 'mail_status'} <= {c['key'] for c in empty['columns']}
    with db.connect(state.settings.paths.db) as conn:
        conn.execute("UPDATE fct_listing_snapshot SET publish_time_text=NULL")
    assert 'published_at' not in {c['key'] for c in G.handle_listings(state, {})['columns']}


@pytest.mark.parametrize('resources,expected', [
    ('星声：1,234，月相：0，余波珊瑚：62，浮金波纹：11，铸潮波纹：1', [1234, 0, 62, 11, 1]),
    ('星声：0', [0, None, None, None, None]),
    ('', None),
])
@pytest.mark.parametrize('via_detail', [False, True])
def test_wuwa_resources_display_existing_features_and_unknowns(state, resources, expected, via_detail):
    title = '80级，40黄；' + resources + '；19个五星角色：3命安可；1个五星武器：精1千古洑流'
    post(state, 'cards', card_html(CARD_TITLE if via_detail else title))
    if via_detail:
        post(state, 'detail', '<div class="product-detail"><a href="/buy/10302/1">鸣潮</a>'
             f'<div class="line-clamp-5">{title}</div></div>')
    page = G.handle_listings(state, {'game_id': ['10302']})
    assert {'key': 'resources', 'label': '资源', 'source': 'feat'} in page['columns']
    value = page['rows'][0]['cells']['resources']
    if expected is None:
        assert value is None
    else:
        assert [item['name'] for item in value] == ['星声', '月相', '余波珊瑚', '浮金波纹', '铸潮波纹']
        assert [item['value'] for item in value] == expected
    assert 'resources' not in {column[0] for column in G.columns_for_game(10026)}
    assert 'resources' not in {column[0] for column in G.columns_for_game(None)}


@pytest.mark.parametrize('detail_first', [False, True])
def test_detail_attributes_reach_display_and_survive_list_reingest(state, detail_first):
    if detail_first:
        post(state, 'detail', detail_html())
    post(state, 'cards', card_html(favorites=1))
    if not detail_first:
        post(state, 'detail', detail_html())
    post(state, 'cards', card_html(favorites=1))
    row = G.handle_listings(state, {'game_id': ['10302']})['rows'][0]
    assert row['cells']['level'] == 80
    assert row['cells']['five_star_chars'] == 19
    assert row['cells']['five_star_weapons'] == 15
    assert row['favorites'] == 1
    assert row['cells']['constellation_cnt'][0] == {'name': '弗洛洛', 'value': 3}
    assert len(row['cells']['five_star_weapon_refined']) == 3


def test_detail_does_not_guess_account_level_from_character_level():
    _, attrs = P.parse_detail_attributes('<div class="product-detail"><span>角色 Lv.90</span></div>')
    assert 'level' not in attrs


def test_detail_ack_does_not_claim_partial_refinements_complete(state):
    post(state, 'cards', card_html())
    result = post(state, 'detail', detail_html())
    # 示例声明15件，但只列出3条精炼；回执须如实显示明细尚不完整。
    assert result['attributes']['five_star_weapons'] == 15
    assert not result['weapon_details_complete']


def test_detail_queue_pins_original_snapshot_round(state):
    import datetime as dt
    seed = post(state, 'cards', card_html())
    old_round = dt.datetime.fromisoformat(seed['round'])
    next_round = old_round + dt.timedelta(minutes=30)
    with db.connect(state.settings.paths.db) as conn:
        conn.execute("INSERT INTO fct_listing_snapshot SELECT * REPLACE (? AS snapshot_at) "
                     "FROM fct_listing_snapshot WHERE listing_id='123456'", [next_round])
    payload = {'url': 'https://www.pxb7.com/product/123456/1', 'html': detail_html(),
               'snapshot_round': seed['round']}
    status, ack = G.handle_ingest(state, '/ingest/detail', json.dumps(payload).encode())
    assert status == 200 and ack['snapshot_rows_updated'] == 1
    with db.connect(state.settings.paths.db) as conn:
        rows = conn.execute("SELECT snapshot_at, five_star_weapons FROM fct_listing_snapshot "
                            "WHERE listing_id='123456' ORDER BY snapshot_at").fetchall()
    assert rows == [(old_round, 15), (next_round, None)]
    payload['snapshot_round'] = '2000-01-01T00:00:00'
    assert G.handle_ingest(state, '/ingest/detail', json.dumps(payload).encode())[0] == 409
    assert not state.pending_details
    payload['snapshot_round'] = 'bad-round'
    assert G.handle_ingest(state, '/ingest/detail', json.dumps(payload).encode())[0] == 400


def test_repair_preserves_rounds_backs_up_and_is_idempotent(state):
    from tools.repair_listing_fields import repair
    post(state, 'cards', card_html(favorites=1))
    with db.connect(state.settings.paths.db) as conn:
        before = conn.execute('SELECT listing_id, snapshot_at FROM fct_listing_snapshot').fetchall()
        conn.execute('UPDATE fct_listing_snapshot SET level=NULL, five_star_chars=3, favorites_cnt=NULL')
    assert repair(state.settings)['changed_snapshots'] == 1
    result = repair(state.settings, apply=True)
    assert result['changed_snapshots'] == 1
    assert __import__('pathlib').Path(result['backup']).is_file()
    with db.connect(state.settings.paths.db, read_only=True) as conn:
        assert conn.execute('SELECT listing_id, snapshot_at FROM fct_listing_snapshot').fetchall() == before
        assert conn.execute('SELECT level, five_star_chars, favorites_cnt FROM fct_listing_snapshot').fetchone() == (80, 19, 1)
    assert repair(state.settings)['changed_snapshots'] == 0


@pytest.mark.parametrize('text,feature,value', [('6链卡提希娅', 'constellation_cnt', 6),
                                             ('谐振3千古洑流', 'five_star_weapon_refined', 3)])
def test_documented_chain_and_resonance_spellings(text, feature, value):
    assert X.extract_listing(X.seed_keywords(game_id=10302), listing_id='W', title=text).features[feature] == value


@pytest.mark.parametrize('skin_label', ['人物皮肤', '角色皮肤', '服饰'])
@pytest.mark.parametrize('via_detail', [False, True])
def test_wuwa_paid_items_reach_display_without_mixing_paint_or_weapon(state, skin_label, via_detail):
    title = (TITLE + f'；{skin_label}：叱妖诰,薄荷糖；车架模组：云帛机骑；'
             '涂装：深空与歌者；摩托饰品：小小救世主,绯雪团子；详情看图【官服】')
    post(state, 'cards', card_html(CARD_TITLE if via_detail else title))
    if via_detail:
        post(state, 'detail', '<div class="product-detail"><a href="/buy/10302/1">鸣潮</a>'
             f'<div class="line-clamp-5">{title}</div></div>')
        post(state, 'cards', card_html(CARD_TITLE))
    page = G.handle_listings(state, {'game_id': ['10302']})
    assert {'key': 'paid_items', 'label': '额外付费商品', 'source': 'feat'} in page['columns']
    assert page['rows'][0]['cells']['paid_items'] == [
        {'name': '车架模组', 'value': '云帛机骑'},
        {'name': '摩托饰品', 'value': '小小救世主、绯雪团子'},
        {'name': '人物皮肤', 'value': '叱妖诰、薄荷糖'},
    ]
    assert len(page['rows'][0]['cells']['five_star_weapon_refined']) == 3
    assert 'paid_items' not in {col[0] for col in G.columns_for_game(10026)}
    assert 'paid_items' not in {col[0] for col in G.columns_for_game(None)}


def test_wuwa_paid_items_unknown_and_explicit_section_boundaries(state):
    post(state, 'cards', card_html())
    assert G.handle_listings(state, {'game_id': ['10302']})['rows'][0]['cells']['paid_items'] is None
    features = X.wuwa_paid_item_features('车架模组：云帛机骑 摩托饰品：绯雪团子 人物皮肤：叱妖诰【官服】')
    assert features == {'vehicle_frame_modules': ['云帛机骑'], 'motorcycle_ornaments': ['绯雪团子'], 'character_skins': ['叱妖诰']}
    assert X.wuwa_paid_item_features('五星角色：安可；摩托饰品；服饰；涂装：深空与歌者') == {}


def test_paid_items_backfill_changes_only_features_and_is_idempotent(state):
    from tools.repair_listing_fields import repair
    post(state, 'cards', card_html(TITLE + '；服饰：叱妖诰；车架模组：云帛机骑'))
    with db.connect(state.settings.paths.db) as conn:
        features = json.loads(conn.execute('SELECT extracted_features FROM fct_listing_snapshot').fetchone()[0])
        for key, _ in X.WUWA_PAID_ITEMS:
            features.pop(key, None)
        conn.execute('UPDATE fct_listing_snapshot SET level=NULL, extracted_features=?', [json.dumps(features)])
        before = conn.execute('SELECT * EXCLUDE(extracted_features) FROM fct_listing_snapshot').fetchall()
    assert repair(state.settings, paid_items_only=True)['changed_snapshots'] == 1
    result = repair(state.settings, apply=True, paid_items_only=True)
    assert result['changed_snapshots'] == 1 and not any(result['field_changes'].values())
    assert __import__('pathlib').Path(result['backup']).is_file()
    with db.connect(state.settings.paths.db, read_only=True) as conn:
        assert conn.execute('SELECT * EXCLUDE(extracted_features) FROM fct_listing_snapshot').fetchall() == before
        updated = json.loads(conn.execute('SELECT extracted_features FROM fct_listing_snapshot').fetchone()[0])
    assert {key: value for key, value in updated.items() if key not in dict(X.WUWA_PAID_ITEMS)} == features
    assert repair(state.settings, paid_items_only=True)['changed_snapshots'] == 0


def test_paid_items_backfill_honors_detail_queue_snapshot_round(state):
    import datetime as dt
    from tools.repair_listing_fields import repair
    seed = post(state, 'cards', card_html())
    old_round = dt.datetime.fromisoformat(seed['round'])
    with db.connect(state.settings.paths.db) as conn:
        conn.execute('INSERT INTO fct_listing_snapshot SELECT * REPLACE (? AS snapshot_at) '
                     'FROM fct_listing_snapshot', [old_round + dt.timedelta(minutes=30)])
    title = TITLE + '；摩托饰品：绯雪团子'
    html = '<div class="product-detail"><a href="/buy/10302/1">鸣潮</a>' + f'<div class="line-clamp-5">{title}</div></div>'
    status, _ = G.handle_ingest(state, '/ingest/detail', json.dumps({
        'url': 'https://www.pxb7.com/product/123456/1', 'html': html, 'snapshot_round': seed['round']}).encode())
    assert status == 200
    with db.connect(state.settings.paths.db) as conn:
        rows = conn.execute('SELECT snapshot_at, extracted_features FROM fct_listing_snapshot').fetchall()
        for snapshot_at, raw in rows:
            features = json.loads(raw)
            features.pop('motorcycle_ornaments', None)
            conn.execute('UPDATE fct_listing_snapshot SET extracted_features=? WHERE snapshot_at=?',
                         [json.dumps(features), snapshot_at])
    assert repair(state.settings, apply=True, paid_items_only=True)['changed_snapshots'] == 1
    with db.connect(state.settings.paths.db, read_only=True) as conn:
        rows = conn.execute('SELECT snapshot_at, extracted_features FROM fct_listing_snapshot ORDER BY snapshot_at').fetchall()
    assert json.loads(rows[0][1])['motorcycle_ornaments'] == ['绯雪团子']
    assert 'motorcycle_ornaments' not in json.loads(rows[1][1])
