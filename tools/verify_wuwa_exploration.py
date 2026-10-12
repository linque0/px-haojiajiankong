"""独立复算留出指标、地图项贡献及发布门槛；不访问网站、不改源数据。"""
import argparse
import csv
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools import wuwa_valuation as V
from tools.wuwa_exploration_valuation import group_key


def price(record, model):
    terms = []
    for i, key in enumerate(model['keys']):
        value = record['features'].get(key)
        if value is None and key.startswith(('char_present:', 'char_chain:', 'weapon_copies:')):
            value = 0
        # 单独复算线性/log1p 变换；不调用生产 predict。
        linear = key.startswith(('missing_', 'char_present:', 'char_chain:')) or key in {
            'level','other_chain_extra','standard_chain_extra','other_chain_2plus',
            'other_chain_3plus','other_chain_6','map_mean_progress','map_progress_coverage','map_astrite_coverage'}
        transformed = model['medians'][i] if value is None else float(value) if linear else math.log1p(value)
        terms.append((transformed-model['mean'][i])/model['scale'][i]*model['coef'][i])
    return math.exp(model['intercept'] + sum(terms))


def verify(source, directory):
    folder = Path(directory)
    report = json.loads((folder/'validation.json').read_text(encoding='utf-8'))
    old = json.loads((folder/'baseline-model.json').read_text(encoding='utf-8'))
    new = json.loads((folder/'experimental-model.json').read_text(encoding='utf-8'))
    chosen = json.loads((folder/'model.json').read_text(encoding='utf-8'))
    assert hashlib.sha256(Path(source).read_bytes()).hexdigest() == report['source_sha256']
    records = V.read_rows(source, new['map_catalog'])
    eligible = {r['id']:r for r in records if r['eligible']}
    sets = [set(new[k]) for k in ['train_ids','calibration_ids','test_ids']]
    assert set.union(*sets) == set(eligible)
    for i, a in enumerate(sets):
        for b in sets[i+1:]:
            assert a.isdisjoint(b)
            assert {group_key(eligible[k]) for k in a}.isdisjoint({group_key(eligible[k]) for k in b})
    assert all(old[k] == new[k] for k in ['train_ids','calibration_ids','test_ids'])
    for model, key in [(old,'baseline_metrics'),(new,'experimental_metrics')]:
        held = [eligible[k] for k in model['test_ids']]
        estimates = [price(r, model) for r in held]
        median = statistics.median(abs(p/r['price']-1) for r,p in zip(held,estimates))
        mae = statistics.mean(abs(p-r['price']) for r,p in zip(held,estimates))
        assert math.isclose(median, report[key]['median_ape'], abs_tol=1e-10)
        assert math.isclose(mae, report[key]['mae_yuan'], abs_tol=1e-8)
    gate = report['gate']
    train_groups = len({group_key(eligible[k]) for k in new['train_ids'] if eligible[k]['maps']['estimated_maps']})
    valid_groups = len({group_key(eligible[k]) for k in new['test_ids'] if eligible[k]['maps']['estimated_maps']})
    assert gate['mapped_groups'] == {'train':train_groups,'validation':valid_groups}
    old_metrics, new_metrics = report['baseline_metrics'],report['experimental_metrics']
    passes = (train_groups>=50 and valid_groups>=20 and
              new_metrics['median_ape'] <= old_metrics['median_ape']*.90 and
              new_metrics['mae_yuan'] <= old_metrics['mae_yuan'])
    assert passes == gate['promoted']
    assert chosen['family'] == (new if passes else old)['family']
    with (folder/'账号估值结果.csv').open(encoding='utf-8-sig',newline='') as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == len(records) and {r['listing_id'] for r in rows} == {r['id'] for r in records}
    for row in rows:
        r = next(r for r in records if r['id']==row['listing_id'])
        expected = r['maps']['remaining_astrite_pulls']
        assert row['地图预计剩余星声抽数'] == '' if expected is None else math.isclose(float(row['地图预计剩余星声抽数']),expected,abs_tol=1e-8)
        if row['地图实验分组留出估值_元']:
            estimate = float(row['地图实验分组留出估值_元'])
            contribution = float(row['地图模型贡献_log相对训练均值'])
            assert abs(estimate - estimate*math.exp(-contribution) - float(row['地图模型贡献_元相对训练均值'])) < .03
    return {'ok':True,'source_rows':len(records),'eligible_rows':len(eligible),
            'default_family':chosen['family'],'gate':gate,'checks':'源文件hash、全组互斥、同验证集、独立指标复算、公式/贡献、发布门槛'}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True)
    parser.add_argument('--output',required=True,help='模型产物目录')
    args=parser.parse_args()
    print(json.dumps(verify(args.input,args.output),ensure_ascii=False,indent=2))
