"""冻结后的双市场状态归因与条件入场筛选；不运行模型或连接交易账户。"""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path

from quantbot.ai.development import select_times
from quantbot.ai.entry_attribution import reconstruct_entries
from quantbot.ai.feeds import fetch_snapshot
from quantbot.evidence import source_tree_hash
from quantbot.factors.entry_context import FACTOR_DEFINITIONS, FACTOR_IDS, build_entry_panel
from quantbot.factors.entry_screen import PROTOCOL as SCREEN_PROTOCOL, evaluate_market, finalize_pool


def read(path):
    return json.loads(Path(path).read_text())


def save(path, data):
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write('\n')


def file_ref(path):
    path = Path(path).resolve()
    with path.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    return {'path': str(path), 'sha256': digest}


def check_ref(reference):
    if file_ref(reference['path'])['sha256'] != reference['sha256']:
        raise ValueError('冻结输入已改变：' + reference['path'])
    return Path(reference['path'])


def instant(value):
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise ValueError('时点必须带时区')
    return at.astimezone(UTC)


def canonical(value):
    return instant(value).isoformat()


def input_references(protocol):
    yield protocol['explanation']
    yield protocol['prior_evidence']
    yield protocol['prior_confirmation_registration']
    yield protocol['prior_strategy_report']
    yield from protocol['preserved_policies']
    for section in protocol['markets'].values():
        yield section['data']
        if 'actions' in section:
            yield section['actions']
        yield section['baseline_report']
        for row in section['baseline_runs']:
            yield row['run']
            yield row['journal']


def validate_protocol(protocol):
    if (protocol['id'] != 'dual_market_entry_factors_v1' or protocol['stage'] != 'development_only'
            or protocol['model_calls_authorized'] != 0 or protocol['trading_enabled']
            or protocol['formal_admission'] or protocol['sealed_holdout_evaluated']):
        raise ValueError('只接受已冻结、零模型调用、交易关闭的开发方案')
    if set(protocol['markets']) != {'crypto', 'stocks'}:
        raise ValueError('本研究须同时登记两个市场')
    definitions = [{key: item[key] for key in ('id', 'operator', 'threshold')}
                   for item in FACTOR_DEFINITIONS]
    if definitions != protocol['factor_definitions']:
        raise ValueError('因子定义与冻结方案不一致')
    if protocol['state_definition'] != {
            'trend_emas': [50, 200], 'ema_adjust': False, 'volatility_window': 20,
            'volatility_ddof': 1, 'prior_median_window': 126, 'prior_median_shift': 1,
            'regimes': ['up', 'down', 'mixed'], 'volatility_states': ['high', 'normal']}:
        raise ValueError('市场状态定义与冻结方案不一致')
    expected = {'costs': [15, 30], 'minimum_feature_coverage': .95, 'minimum_events_per_window': 5,
                'minimum_total_retained': 30, 'minimum_total_rejected': 10, 'minimum_positive_windows': 2,
                'bootstrap_replicates': 4999, 'block_sessions': 20, 'seed': 1729,
                'familywise_alpha': .05, 'current_tests': 8, 'correction_family_size': 124,
                'maximum_factors_per_market': 2, 'selection_order': 'fixed_factor_definition_order',
                'scope': 'exploratory_old_path_entry_ablation_not_new_strategy_returns'}
    if protocol['screening'] != expected or len(protocol['prior_tests']) != 116:
        raise ValueError('筛选或旧试验预算与登记不一致')
    for market, section in protocol['markets'].items():
        if section['spec']['market'] != market or section['spec']['source'] != 'csv':
            raise ValueError('只接受显式登记的离线CSV')
        if section['spec'].get('sec_context_path'):
            raise ValueError('本研究不读取额外事件源')
        if len(section['windows']) != 3 or len(section['baseline_runs']) != 8:
            raise ValueError('需要三个分窗和连续账户的两档成本完整证据')
        expected_runs = {(w['id'], c) for w in section['windows'] for c in (15, 30)} | {
            ('continuous', 15), ('continuous', 30)}
        if len(expected_runs) != 8 or {(r['window'], r['cost_bps']) for r in section['baseline_runs']} != expected_runs:
            raise ValueError('基准窗口或成本重复/缺失')
    for reference in input_references(protocol):
        check_ref(reference)


def register(protocol_path, output):
    protocol_path, output = Path(protocol_path).resolve(), Path(output).resolve()
    protocol = read(protocol_path)
    declared = read(protocol_path.parent / 'protocol-freeze.json')['protocol']
    if file_ref(protocol_path) != declared:
        raise ValueError('预登记协议已经改变')
    validate_protocol(protocol)
    output.mkdir(parents=True, exist_ok=False)
    freeze = {'protocol': file_ref(protocol_path), 'source_hash': source_tree_hash(),
              'frozen_at': datetime.now(UTC).isoformat(), 'model_calls': 0, 'trading_enabled': False,
              'compiled_factor_definitions': FACTOR_DEFINITIONS, 'compiled_screen_protocol': SCREEN_PROTOCOL,
              'formal_admission': False, 'stage': 'development_only'}
    save(output / 'execution-freeze.json', freeze)
    return freeze


def join_episode(episode, panel, window):
    event = deepcopy(episode)
    key = (canonical(event['signal_at']), event['symbol'])
    if key not in panel:
        raise ValueError(f'真实入场无法精确对齐事前特征：{key}')
    values = panel[key]
    event['signal_at'] = key[0]
    event['window'] = window
    event['market_state'] = values['market_state']
    event['asset_trend'] = values['asset_trend']
    event['factors'] = {name: float(values[name]) if values[name] is not None
                        and math.isfinite(float(values[name])) else None for name in FACTOR_IDS}
    return event


def episode_groups(events, *, by_symbol=False):
    groups = defaultdict(list)
    for event in events:
        key = event['market_state'] + (':' + event['symbol'] if by_symbol else '')
        groups[key].append(event)
    result = []
    for state, own in sorted(groups.items()):
        result.append({'state': state, 'episodes': len(own),
                       'net_pnl': sum(e['net_pnl'] for e in own),
                       'gross_pnl': sum(e['gross_pnl'] for e in own),
                       'costs': sum(e['costs'] for e in own),
                       'net_winners': sum(e['net_pnl'] > 0 for e in own),
                       'short_stop_episodes': sum(e['holding_bars'] <= 5 and
                           'protective_stop' in e['exit_reasons'] for e in own),
                       'short_stop_net_pnl': sum(e['net_pnl'] for e in own if e['holding_bars'] <= 5
                           and 'protective_stop' in e['exit_reasons'])})
    return result


def daily_groups(daily, panel, benchmark, initial):
    groups = defaultdict(lambda: {'days': 0, 'close_position_days': 0, 'net_pnl': 0.0})
    previous_equity, previous_at = initial, None
    for row in daily:
        state = panel[(canonical(previous_at), benchmark)]['market_state'] if previous_at else 'initial'
        target = groups[state]
        target['days'] += 1
        target['close_position_days'] += bool(row['positions'])
        target['net_pnl'] += row['equity'] - previous_equity
        previous_at, previous_equity = row['as_of'], row['equity']
    return [{'state': state, **value} for state, value in sorted(groups.items())]


def analyze_market(protocol, market, output):
    section = protocol['markets'][market]
    directory = output / market
    directory.mkdir(exist_ok=False)
    config, spec = deepcopy(section['config']), deepcopy(section['spec'])
    spec['data_path'] = str(check_ref(section['data']))
    snapshot = fetch_snapshot(spec, config, datetime.now(UTC), directory, fresh=False)
    if snapshot.times[-1].date().isoformat() > section['latest_allowed_close_date']:
        raise ValueError('行情超出冻结开发边界')
    frame = build_entry_panel(snapshot)
    if frame.duplicated(['as_of', 'symbol']).any():
        raise ValueError('状态特征键重复')
    frame.to_csv(directory / 'entry-context.csv', index=False)
    panel = {(canonical(row['as_of']), row['symbol']): row for row in frame.to_dict('records')}
    calendars = {w['id']: [at.astimezone(UTC).isoformat() for at in select_times(snapshot, w)]
                 for w in section['windows']}
    continuous_times = select_times(snapshot, section['continuous_window'])
    if [at for w in section['windows'] for at in calendars[w['id']]] != [
            at.astimezone(UTC).isoformat() for at in continuous_times]:
        raise ValueError('三个窗口必须无重叠覆盖连续账户')
    source_report = read(check_ref(section['baseline_report']))
    events_by_cost, runs = {15: [], 30: []}, []
    for descriptor in section['baseline_runs']:
        document = read(check_ref(descriptor['run']))
        summary = document['summary']
        if (summary['candidate'] != section['baseline_candidate']
                or summary['window'] != descriptor['window'] or summary['cost_bps'] != descriptor['cost_bps']):
            raise ValueError('基准身份与冻结运行不一致')
        reported = [r for r in source_report['runs'] if r['candidate'] == summary['candidate']
                    and r['window'] == summary['window'] and r['cost_bps'] == summary['cost_bps']]
        if len(reported) != 1 or reported[0] != {**summary, 'journal': descriptor['journal']}:
            raise ValueError('基准逐次产物与报告不一致')
        rebuilt = reconstruct_entries(Path(descriptor['run']['path']), descriptor['journal'], market)
        if rebuilt['source']['code_hash'] != source_report['source_hash']:
            raise ValueError('基准日志与报告源码指纹不一致')
        events = [join_episode(e, panel, descriptor['window']) for e in rebuilt['events']]
        opened = [join_episode(e, panel, descriptor['window']) for e in rebuilt['open_episodes']]
        stopped = [d for d in rebuilt['daily'] if not d['has_decision'] and not d['is_final_bar']] \
            if summary['risk_hold'] else []
        first_stop = stopped[0]['as_of'] if stopped else None
        after_stop = [d for d in rebuilt['daily'] if first_stop and instant(d['as_of']) >= instant(first_stop)]
        if descriptor['window'] != 'continuous':
            grid = calendars[descriptor['window']]
            for event in events:
                if event['signal_at'] not in grid or instant(event['exit_at']) > instant(grid[-1]):
                    raise ValueError('闭合周期越过冻结验证窗口')
            events_by_cost[descriptor['cost_bps']].extend(events)
        row = {'window': descriptor['window'], 'cost_bps': descriptor['cost_bps'],
               'baseline_candidate': section['baseline_candidate'], 'summary': summary,
               'events': events, 'open_episodes': opened, 'accounting': rebuilt['accounting'],
               'source': rebuilt['source'], 'entry_state_groups': episode_groups(events),
               'entry_state_symbol_groups': episode_groups(events, by_symbol=True),
               'previous_close_state_daily_pnl': daily_groups(rebuilt['daily'], panel,
                    config['market']['benchmark'], rebuilt['initial_equity']),
               'close_flat_days': sum(not d['positions'] for d in rebuilt['daily']),
               'risk_halt_timing': {'first_suppressed_decision': first_stop,
                    'halted_valuation_days': len(after_stop),
                    'halted_flat_close_days': sum(not d['positions'] for d in after_stop)},
               'risk_hold': summary['risk_hold']}
        save(directory / f"{descriptor['window']}__cost{descriptor['cost_bps']}__entries.json", row)
        runs.append(row)
    diagnosis = {'market': market, 'baseline_candidate': section['baseline_candidate'], 'runs': runs,
                 'state_basis': 'entry signal as_of; daily mark-to-market uses previous completed close state',
                 'closed_events_for_screening': {str(cost): len(events) for cost, events in events_by_cost.items()},
                 'continuous_not_pooled': True, 'formal_admission': False, 'model_calls': 0}
    save(directory / 'state-diagnosis.json', diagnosis)
    return diagnosis, events_by_cost, calendars


def render_diagnosis(diagnoses):
    lines = ['# 双市场事前状态归因', '',
             '状态按真实入场信号时点划分；以下为连续账户闭合持仓周期损益，费用与税前分红已计入。期末未平仓另列，不是零收益。状态样本少时只能描述，不能据此宣称统计规律。', '',
             '| 市场 | 单边成本 | 入场状态 | 闭合周期 | 净损益 | 费用 | 五日内止损周期 / 净损益 |',
             '|---|---:|---|---:|---:|---:|---|']
    for diagnosis in diagnoses:
        for run in diagnosis['runs']:
            if run['window'] != 'continuous':
                continue
            for row in run['entry_state_groups']:
                lines.append(f"| {diagnosis['market']} | {run['cost_bps']} bps | {row['state']} | {row['episodes']} | "
                    f"{row['net_pnl']:+.4f} | {row['costs']:.4f} | {row['short_stop_episodes']} / {row['short_stop_net_pnl']:+.4f} |")
            if run['open_episodes']:
                lines.append(f"\n{diagnosis['market']} / {run['cost_bps']} bps：{len(run['open_episodes'])} 个期末未闭合周期单列。\n")
    lines += ['', '加密金额单位 USDT，美股 USD，不跨市场合计。完整的分窗、逐标的、逐日上一收盘状态归因与账目恒等式见对应市场 JSON。', '']
    return '\n'.join(lines)


def render_screen(result):
    lines = ['# 条件入场因子筛选', '',
             '固定旧基准成交路径的事件消融；不是新策略账户收益，也不是独立验证。保留周期的净贡献和相对原路径的增量必须同时通过。', '',
             '| 市场 | 因子 | 基础保留/原周期 | 压力保留/原周期 | Holm p | 结论 |',
             '|---|---|---|---|---:|---|']
    for row in result['results']:
        counts = [f"{cost['totals']['kept_events']}/{cost['totals']['baseline_events']}" for cost in row['costs']]
        lines.append(f"| {row['market']} | {row['factor']} | {counts[0]} | {counts[1]} | "
                     f"{row['holm_p_value']:.4f} | {row['status']} |")
        lines.append('\n未通过项：' + '、'.join(row['reasons']) + '\n')
    lines += ['', f"研究池：{json.dumps(result['pool_by_market'], ensure_ascii=False)}。", '',
              '筛选空池时不构建新策略，不追加阈值。完整样本、覆盖率、放弃利润、避开亏损、两成本区块检验和失败原因见 result.json。', '']
    return '\n'.join(lines)


def run(study):
    study = Path(study).resolve()
    freeze = read(study / 'execution-freeze.json')
    if source_tree_hash() != freeze['source_hash']:
        raise ValueError('冻结后源码已变化')
    protocol = read(check_ref(freeze['protocol']))
    validate_protocol(protocol)
    save(study / 'started.json', {'started_at': datetime.now(UTC).isoformat(), 'source_hash': source_tree_hash()})
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {market: executor.submit(analyze_market, protocol, market, study) for market in ('crypto', 'stocks')}
            analyzed = {market: future.result() for market, future in futures.items()}
        # 两市场归因完整落盘后才执行因子筛选。
        (study / 'STATE-DIAGNOSIS.md').write_text(render_diagnosis([analyzed[m][0] for m in ('crypto', 'stocks')]))
        save(study / 'diagnosis-completed.json', {'completed_at': datetime.now(UTC).isoformat(),
             'files': [file_ref(study / m / 'state-diagnosis.json') for m in ('crypto', 'stocks')]})
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {market: executor.submit(evaluate_market, market, analyzed[market][1], analyzed[market][2])
                       for market in ('crypto', 'stocks')}
            rows = [row for future in futures.values() for row in future.result()]
        result = finalize_pool(rows, [r['p_value'] for r in protocol['prior_tests']])
        result.update(source_hash=freeze['source_hash'], protocol_reference=freeze['protocol'],
                      model_calls=0, strategy_backtests=0, formal_admission=False, trading_enabled=False,
                      holdout_evaluated=False, stage='development_only',
                      next_action='freeze_qualified_single_factor_strategies' if any(result['pool_by_market'].values())
                                  else 'stop_empty_pool_no_new_strategies')
        if source_tree_hash() != freeze['source_hash']:
            raise ValueError('执行期间源码改变')
        check_ref(freeze['protocol'])
        validate_protocol(protocol)
        save(study / 'result.json', result)
        (study / 'FACTOR-RESULTS.md').write_text(render_screen(result))
        save(study / 'completed.json', {'completed_at': datetime.now(UTC).isoformat(),
                                       'result': file_ref(study / 'result.json')})
        return result
    except BaseException as exc:
        save(study / 'failed.json', {'type': type(exc).__name__, 'message': str(exc),
                                   'formal_admission': False, 'trading_enabled': False})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    freeze = commands.add_parser('freeze')
    freeze.add_argument('--protocol', required=True)
    freeze.add_argument('--output', required=True)
    execute = commands.add_parser('run')
    execute.add_argument('--study', required=True)
    args = parser.parse_args()
    if args.command == 'freeze':
        print(json.dumps(register(args.protocol, args.output), ensure_ascii=False))
    else:
        result = run(args.study)
        print(json.dumps({k: result[k] for k in ('pool_by_market', 'next_action', 'formal_admission')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
