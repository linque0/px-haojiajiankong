"""同组比较现有挂牌模型与地图剩余资源模型；未达门槛保留旧算法为默认。

不写数据库、不按充值单价折算。输出默认/实验模型、验证、OOF结果与地图贡献。
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools import wuwa_valuation as V
from pxb7 import exploration as E


def group_key(record):
    # 同商品已在 read_rows 拒绝重复；相同具名资产忽略余额/等级小差异，整组隔离。
    payload = [sorted(record['chars'].items()), sorted(record['weapons']),
               record['raw'].get('区服'), record['raw'].get('额外付费商品') or '']
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def group_folds(records, k, seed):
    groups = sorted({group_key(r) for r in records})
    random.Random(seed).shuffle(groups)
    assignment = {g: i % k for i, g in enumerate(groups)}
    return [[i for i, r in enumerate(records) if assignment[group_key(r)] == f] for f in range(k)]


def mapped_groups(records):
    return len({group_key(r) for r in records if r['maps']['estimated_maps'] > 0})


def promotion_gate(train, validation, baseline_metrics, candidate_metrics):
    groups = {'train': mapped_groups(train), 'validation': mapped_groups(validation)}
    old = baseline_metrics['median_ape']
    improvement = (old - candidate_metrics['median_ape']) / old if old > 0 else 0.0
    reasons = []
    if groups['train'] < 50:
        reasons.append('有效地图训练组不足50')
    if groups['validation'] < 20:
        reasons.append('有效地图验证组不足20')
    if improvement < .10:
        reasons.append('中位相对误差改善不足10%')
    if candidate_metrics['mae_yuan'] > baseline_metrics['mae_yuan']:
        reasons.append('平均绝对误差恶化')
    return {'promoted': not reasons, 'reasons': reasons, 'mapped_groups': groups,
            'median_ape_improvement': improvement}


def train_pair(train, seed=10105):
    old, old_scores = V.select(train, seed=seed, grouping=group_folds)
    new, new_scores = V.select(train, seed=seed, grouping=group_folds,
                               families=['exploration', 'exploration_named'])
    return old, new, old_scores + new_scores


def metadata(model, train, calibration, validation, source):
    import numpy as np
    radius = V.quantile(np.abs(np.log([r['price'] for r in calibration]) - np.log(V.predict(calibration, model))))
    model.update(version='wuwa-listing-v1.1-map', target='pxb7 官服挂牌参考价，人民币',
                 input_sha256=hashlib.sha256(Path(source).read_bytes()).hexdigest(),
                 map_catalog=E.catalog(), feature_labels=V.FEATURE_LABELS,
                 interval_log_radius=radius, interval_method='分组校准误差80%参考带；发布门槛另用验证集',
                 valid_scope='完整官服配置；地图仅大致估算，部分覆盖；挂牌样本相关性模型',
                 train_ids=[r['id'] for r in train], calibration_ids=[r['id'] for r in calibration],
                 test_ids=[r['id'] for r in validation],
                 known_characters=sorted({k for r in train for k in r['chars']}),
                 known_weapons=sorted({k for r in train for k, _ in r['weapons']}))


def run(source, output):
    import numpy as np
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    records = V.read_rows(source)
    eligible = [r for r in records if r['eligible']]
    if len(eligible) < 80 or len({group_key(r) for r in eligible}) < 25:
        raise ValueError('有效样本或配置组不足，保留原模型，停止新模型训练')
    folds = group_folds(eligible, 5, 20261005)
    train = [eligible[i] for f in folds[:3] for i in f]
    calibration = [eligible[i] for i in folds[3]]
    validation = [eligible[i] for i in folds[4]]
    old, new, scores = train_pair(train)
    old_metrics = V.metrics(validation, V.predict(validation, old))
    new_metrics = V.metrics(validation, V.predict(validation, new))
    gate = promotion_gate(train, validation, old_metrics, new_metrics)
    for model in (old, new):
        metadata(model, train, calibration, validation, source)
    default = new if gate['promoted'] else old
    # 外层组从不参与自己的模型选择。两个算法分别生成 OOF；不按该行实际价格择优。
    oof = {}
    for f, indices in enumerate(group_folds(eligible, 5, 73021)):
        excluded = set(indices)
        outer_train = [r for i, r in enumerate(eligible) if i not in excluded]
        held = [eligible[i] for i in indices]
        a, b, _ = train_pair(outer_train, seed=f + 814)
        for r, pa, pb in zip(held, V.predict(held, a), V.predict(held, b)):
            contribution = V.map_contribution(r, b)
            oof[r['id']] = {'old': float(pa), 'experimental': float(pb),
                            'contribution_log': contribution,
                            'contribution_yuan': float(pb) - float(pb) * math.exp(-contribution)}
    split = {r['id']: name for name, rows in [('train', train), ('calibration', calibration), ('validation', validation)] for r in rows}
    results = []
    for r in records:
        estimates = oof.get(r['id'])
        result = {'listing_id': r['id'], '挂牌价_元': r['price'], '配置质量': '可建模' if r['eligible'] else '待复核',
                  '质量标记': ';'.join(r['flags']), '验证分组': split.get(r['id'], 'excluded'),
                  '配置组': group_key(r), '地图探索度': r['raw'].get('地图探索度', ''),
                  '地图预计剩余资源': E.remaining_text(E.parse_csv(r['raw'].get('地图探索度'))) or '未知',
                  '地图预计剩余星声抽数': r['maps']['remaining_astrite_pulls'],
                  '地图星声统计覆盖率': r['maps']['coverage']['astrite'],
                  '地图估算范围': '部分地图估算' if r['maps']['partial'] else '地图估算',
                  '默认模型': '地图模型' if gate['promoted'] else '原有模型',
                  '估值发布门槛': '达标' if gate['promoted'] else '未达标；地图模型仅实验'}
        for key, label in [('old', '原模型分组留出估值_元'), ('experimental', '地图实验分组留出估值_元'),
                           ('contribution_log', '地图模型贡献_log相对训练均值'), ('contribution_yuan', '地图模型贡献_元相对训练均值')]:
            result[label] = round(estimates[key], 6 if key == 'contribution_log' else 2) if estimates else ''
        result['分组留出估值_元'] = round(estimates['experimental' if gate['promoted'] else 'old'], 2) if estimates else ''
        result['默认地图模型贡献_log相对训练均值'] = round(estimates['contribution_log'], 6) if estimates and gate['promoted'] else 0 if estimates else ''
        # 发布门槛使用留出样本，不把受门槛选择影响的结果包装成完全独立 OOF 评估。
        results.append(result)
    report = {'source': str(Path(source).resolve()), 'source_sha256': old['input_sha256'],
              'catalog_version': E.catalog()['version'], 'quality': V.audit(records),
              'split_seed': 20261005, 'split_unit': '具名角色链数/武器精炼/区服/外观配置；忽略余额和等级微差，近似配置同组',
              'train_n': len(train), 'calibration_n': len(calibration), 'validation_n': len(validation),
              'baseline_metrics': old_metrics, 'experimental_metrics': new_metrics, 'gate': gate,
              'default_family': default['family'], 'train_cv': '只在训练组内选family和alpha；旧新用完全相同划分',
              'limits': '发布门槛检查用验证集；非成交价，未按卖家隔离；地图总量为社区近似（51/51满探索参考值，仅星声）；OOF分别报告两种算法，不是发布选择后的独立精度证明'}
    V.json_write(output / 'baseline-model.json', old)
    V.json_write(output / 'experimental-model.json', new)
    V.json_write(output / 'model.json', default)
    V.json_write(output / 'validation.json', report)
    V.csv_write(output / '账号估值结果.csv', results)
    V.csv_write(output / '训练内交叉验证.csv', scores)
    V.csv_write(output / '地图模型系数.csv', [{'特征': k, '名称': V.FEATURE_LABELS.get(k, k), '标准化log价格系数': c,
                 '说明': '挂牌样本校准；相对训练均值的相关性贡献，非固定人民币兑换价'} for k, c in zip(new['keys'], new['coef'])])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    run(args.input, args.output)
