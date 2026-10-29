from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from quantbot.ai.evaluation import replay
from quantbot.ai.feeds import Snapshot
from quantbot.ai.development_strategies import development_provider
from quantbot.ai.replay_evidence import inspect_replay
from quantbot.factors.cross_asset_study import HYPOTHESES, SCREEN, finalize, observe_factor, validate
from quantbot.factors.paired_events import audit_branch, opportunity, parent_checkpoints, run_branch
from quantbot.factors.paired_study import screen_market
from tests.test_paired_events import synthetic_crypto
from tests.test_paired_study import fixture


def etf_history():
    # June 19 is not a session. The lookback must use sessions, not calendar days.
    dates = ['2024-06-17', '2024-06-18', '2024-06-20', '2024-06-21', '2024-06-24', '2024-06-25']
    times = [datetime.fromisoformat(day+'T20:00:00+00:00') for day in dates]
    rows = [{'date': day, 'symbol': symbol, 'close': value}
            for symbol, values in [('SPY', [100]*6), ('QQQ', [100, 101, 102, 102, 101, 102])]
            for day, value in zip(dates, values)]
    return SimpleNamespace(market='stocks', times=times, rows=rows, actions=[]), SimpleNamespace(as_of=times[-1])


def test_crypto_requires_both_eth_conditions_and_keeps_equality_at_ema():
    snapshot = SimpleNamespace(market='crypto')
    context = SimpleNamespace(as_of=datetime(2024, 1, 1, tzinfo=UTC),
        features={'ETH/USDT': {'momentum': .01, 'close': 100, 'ema_20': 100}})
    assert observe_factor(snapshot, context, {})['keep']
    for field, value in [('momentum', 0), ('close', 99)]:
        changed = deepcopy(context)
        changed.features['ETH/USDT'][field] = value
        assert not observe_factor(snapshot, changed, {})['keep']
    context.features['ETH/USDT']['momentum'] = float('nan')
    result = observe_factor(snapshot, context, {})
    assert not result['valid'] and not result['keep'] and result['values']['momentum'] is None


def test_etf_majority_uses_five_sessions_and_ties_are_not_wins():
    snapshot, context = etf_history()
    result = observe_factor(snapshot, context, {'incumbent': 'SPY', 'target': 'QQQ'})
    assert result['valid'] and result['keep'] and result['relative_wins'] == 3
    assert len(result['daily']) == 5 and result['daily'][1]['date'] == '2024-06-20'
    assert not result['daily'][2]['relative_win']


def test_etf_ex_dividend_return_counts_entitlement_not_price_drop():
    snapshot, context = etf_history()
    snapshot.actions = [{'date': '2024-06-25', 'symbol': 'SPY', 'type': 'dividend', 'amount_per_share': '2'}]
    result = observe_factor(snapshot, context, {'incumbent': 'SPY', 'target': 'QQQ'})
    assert result['daily'][-1]['incumbent_total_return'] == pytest.approx(.02)
    assert result['relative_wins'] == 2 and not result['keep']


def test_future_prices_and_dividends_cannot_change_an_earlier_filter():
    snapshot, context = etf_history()
    event = {'incumbent': 'SPY', 'target': 'QQQ'}
    before = observe_factor(snapshot, context, event)
    snapshot.times.append(datetime(2024, 6, 26, 20, tzinfo=UTC))
    snapshot.rows += [{'date': '2024-06-26', 'symbol': 'SPY', 'close': 999},
                      {'date': '2024-06-26', 'symbol': 'QQQ', 'close': 1}]
    snapshot.actions = [{'date': '2024-06-26', 'symbol': 'QQQ', 'type': 'dividend', 'amount_per_share': '100'}]
    assert observe_factor(snapshot, context, event) == before


def test_missing_closes_and_short_history_do_not_count_as_valid_features():
    snapshot, context = etf_history()
    event = {'incumbent': 'SPY', 'target': 'QQQ'}
    assert not observe_factor(snapshot, SimpleNamespace(as_of=snapshot.times[4]), event)['valid']
    snapshot.rows.pop()
    assert not observe_factor(snapshot, context, event)['valid']


def test_origin_filter_is_immediate_or_cash_and_does_not_wait_for_later_retest(tmp_path):
    config, spec, snapshot = synthetic_crypto()
    rows = deepcopy(snapshot.rows)
    rows[42].update(open=140.3, low=140.2, close=141.0, high=142.2)
    snapshot = Snapshot('crypto', rows, config, 'synthetic_cross_filter')
    path = tmp_path/'parent.jsonl'
    replay(snapshot, spec, config, development_provider('crypto', 'btc_breakout20_filtered'),
           times=snapshot.times[40:65], journal_path=path)
    records = inspect_replay(path)['records']
    state, context, plan, prefix = next(parent_checkpoints(snapshot, records, config, spec))
    event, _ = opportunity('crypto', state, context, plan, 15)
    contexts = [snapshot.context(at) for at in snapshot.times[40:61]]
    control = run_branch(snapshot, state, contexts, config, spec, plan, event, candidate=False)
    accepted = run_branch(snapshot, state, contexts, config, spec, plan, event, candidate=True, retain_original=True)
    rejected = run_branch(snapshot, state, contexts, config, spec, plan, event, candidate=True, retain_original=False)
    assert accepted['records'] == control['records'] and accepted['final_account'] == control['final_account']
    assert accepted['factor_kept'] and accepted['target_buy_filled']
    assert rejected['net_pnl'] == 0 and not rejected['target_buy_filled'] and not rejected['factor_kept']
    assert rejected['confirmed_at'] is None and rejected['event_phase'] == 'filtered'
    for branch in (accepted, rejected):
        assert audit_branch(records, prefix, branch)['status'] == 'reconciled'
    with pytest.raises(ValueError, match='explicit origin filter'):
        run_branch(snapshot, state, contexts, config, spec, plan, event, candidate=False, retain_original=True)


def test_new_family_keeps_all_prior_exposures_and_requires_both_new_hypotheses():
    analysis, calendars = fixture()
    crypto = screen_market(analysis, calendars)
    crypto['hypothesis'] = HYPOTHESES[0]['id']
    stocks = deepcopy(crypto)
    stocks.update(market='stocks', hypothesis=HYPOTHESES[1]['id'])
    result = finalize([crypto, stocks], [{'p_value': 1.0}]*126)
    assert result['correction_family_size'] == 128 and all(result['pool_by_market'].values())
    assert result['baseline_refresh_replays'] == 16 and not result['formal_admission']
    with pytest.raises(ValueError, match='prior exposures'):
        finalize([crypto, stocks], [{'p_value': 1.0}]*124)


@pytest.mark.parametrize('change', ['trade', 'threshold'])
def test_protocol_refuses_trade_enablement_or_new_thresholds_before_reading_inputs(change):
    protocol = {'id': 'cross_asset_entry_factors_v1', 'stage': 'development_only',
        'model_calls_authorized': 0, 'trading_enabled': False, 'formal_admission': False,
        'sealed_holdout_evaluated': False, 'hypotheses': deepcopy(HYPOTHESES), 'screening': deepcopy(SCREEN)}
    if change == 'trade':
        protocol['trading_enabled'] = True
    else:
        protocol['hypotheses'][1]['minimum_relative_wins'] = 2
    with pytest.raises(ValueError):
        validate(protocol)
