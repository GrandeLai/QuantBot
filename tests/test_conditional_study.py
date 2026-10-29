from copy import deepcopy
import json

import pytest

from quantbot.factors.conditional_study import (
    daily_groups, file_ref, join_episode, register, run,
)
from quantbot.factors.entry_context import FACTOR_DEFINITIONS, FACTOR_IDS


def test_exact_signal_join_never_moves_midnight_to_previous_day():
    at = '2025-01-02T00:00:00+00:00'
    values = {'market_state': 'up_high', 'asset_trend': 'up', **dict.fromkeys(FACTOR_IDS, 1.0)}
    panel = {(at, 'BTC/USDT'): values}
    event = {'signal_at': at, 'symbol': 'BTC/USDT', 'entry_at': at}
    result = join_episode(event, panel, 'a')
    assert result['signal_at'] == at and result['market_state'] == 'up_high'
    assert 'factors' not in event
    with pytest.raises(ValueError, match='精确对齐'):
        join_episode({**event, 'signal_at': '2025-01-01T00:00:00+00:00'}, panel, 'a')


def test_daily_pnl_uses_previous_known_state_and_preserves_account_total():
    days = [f'2025-01-0{d}T00:00:00+00:00' for d in (1, 2, 3)]
    panel = {(day, 'BTC/USDT'): {'market_state': state}
             for day, state in zip(days, ['up_normal', 'down_high', 'mixed_high'], strict=True)}
    daily = [{'as_of': day, 'equity': equity, 'positions': {'BTC/USDT': '1'}}
             for day, equity in zip(days, [1000, 1100, 1080], strict=True)]
    values = {row['state']: row for row in daily_groups(daily, panel, 'BTC/USDT', 1000)}
    assert values['up_normal']['net_pnl'] == 100
    assert values['down_high']['net_pnl'] == -20
    assert sum(row['net_pnl'] for row in values.values()) == 80
    assert 'mixed_high' not in values


def frozen_input(tmp_path):
    dummy = tmp_path / 'synthetic-input.txt'
    dummy.write_text('synthetic freeze integrity test; never replayed')
    reference = file_ref(dummy)
    section = {'spec': {'source': 'csv'}, 'windows': [{'id': w} for w in ('a', 'b', 'c')],
               'data': reference, 'baseline_report': reference,
               'baseline_runs': [{'window': w, 'cost_bps': c, 'run': reference, 'journal': reference}
                                 for w in ('a', 'b', 'c', 'continuous') for c in (15, 30)]}
    protocol = {'id': 'dual_market_entry_factors_v1', 'stage': 'development_only',
                'model_calls_authorized': 0, 'trading_enabled': False,
                'formal_admission': False, 'sealed_holdout_evaluated': False,
                'factor_definitions': [{k: d[k] for k in ('id', 'operator', 'threshold')}
                                       for d in FACTOR_DEFINITIONS],
                'state_definition': {'trend_emas': [50, 200], 'ema_adjust': False,
                    'volatility_window': 20, 'volatility_ddof': 1, 'prior_median_window': 126,
                    'prior_median_shift': 1, 'regimes': ['up', 'down', 'mixed'],
                    'volatility_states': ['high', 'normal']},
                'screening': {'costs': [15, 30], 'minimum_feature_coverage': .95,
                    'minimum_events_per_window': 5, 'minimum_total_retained': 30,
                    'minimum_total_rejected': 10, 'minimum_positive_windows': 2,
                    'bootstrap_replicates': 4999, 'block_sessions': 20, 'seed': 1729,
                    'familywise_alpha': .05, 'current_tests': 8, 'correction_family_size': 124,
                    'maximum_factors_per_market': 2, 'selection_order': 'fixed_factor_definition_order',
                    'scope': 'exploratory_old_path_entry_ablation_not_new_strategy_returns'},
                'prior_tests': [1] * 116, 'explanation': reference, 'prior_evidence': reference,
                'prior_confirmation_registration': reference, 'prior_strategy_report': reference,
                'preserved_policies': [reference], 'markets': {}}
    for market in ('crypto', 'stocks'):
        protocol['markets'][market] = deepcopy(section)
        protocol['markets'][market]['spec']['market'] = market
    path = tmp_path / 'PROTOCOL.json'
    path.write_text(json.dumps(protocol))
    (tmp_path / 'protocol-freeze.json').write_text(json.dumps({'protocol': file_ref(path)}))
    return path, dummy


def test_frozen_study_cannot_overwrite_or_consume_changed_input(tmp_path):
    path, dummy = frozen_input(tmp_path)
    output = tmp_path / 'study'
    freeze = register(path, output)
    assert freeze['model_calls'] == 0 and not freeze['trading_enabled']
    with pytest.raises(FileExistsError):
        register(path, output)
    dummy.write_text('changed after freeze')
    with pytest.raises(ValueError, match='冻结输入已改变'):
        run(output)
    assert not (output / 'started.json').exists()


def test_changed_protocol_is_rejected_before_creating_output(tmp_path):
    path, _ = frozen_input(tmp_path)
    changed = json.loads(path.read_text())
    changed['state_definition']['prior_median_shift'] = 0
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match='预登记协议已经改变'):
        register(path, tmp_path / 'study')
    assert not (tmp_path / 'study').exists()
