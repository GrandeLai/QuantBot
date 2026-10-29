from copy import deepcopy
from datetime import UTC, datetime, timedelta
import hashlib
from types import SimpleNamespace

import pytest

from quantbot.evidence import source_tree_hash
from quantbot.factors.leadership_study import (
    HYPOTHESES, SCREEN, finalize, observe_factor, validate, validate_baseline_code,
)
from quantbot.factors.paired_study import screen_market
from tests.test_paired_study import fixture


def stock_history():
    dates = []
    day = datetime(2024, 6, 3, 20, tzinfo=UTC)
    while len(dates) < 21:
        if day.weekday() < 5 and day.date().isoformat() != '2024-06-19':
            dates.append(day)
        day += timedelta(days=1)
    rows = [{'date': t.date().isoformat(), 'symbol': symbol, 'close': 100 * growth**i}
            for symbol, growth in [('SPY', 1.001), ('QQQ', 1.002)] for i, t in enumerate(dates)]
    snapshot = SimpleNamespace(market='stocks', times=dates, rows=rows, actions=[])
    context = SimpleNamespace(as_of=dates[-1], features={'SPY': {'atr': 1}, 'QQQ': {'atr': 10}})
    return snapshot, context, {'incumbent': 'SPY', 'target': 'QQQ'}


def test_btc_leadership_preserves_independent_strength_when_eth_is_negative():
    context = SimpleNamespace(as_of=datetime(2024, 1, 1, tzinfo=UTC),
                              features={'BTC/USDT': {'momentum': .05}, 'ETH/USDT': {'momentum': -.02}})
    snapshot = SimpleNamespace(market='crypto')
    result = observe_factor(snapshot, context, {})
    assert result['valid'] and result['keep']
    context.features['ETH/USDT']['momentum'] = .06
    assert not observe_factor(snapshot, context, {})['keep']
    context.features['ETH/USDT']['momentum'] = .05
    assert not observe_factor(snapshot, context, {})['keep']
    context.features['ETH/USDT']['momentum'] = float('nan')
    result = observe_factor(snapshot, context, {})
    assert not result['valid'] and not result['keep'] and result['momentum']['ETH/USDT'] is None


def test_larger_raw_etf_return_can_fail_when_its_atr_is_larger():
    snapshot, context, event = stock_history()
    result = observe_factor(snapshot, context, event)
    assert result['valid'] and not result['keep']
    assert result['values']['QQQ']['total_return_20'] > result['values']['SPY']['total_return_20']
    assert result['values']['QQQ']['score'] < result['values']['SPY']['score']
    context.features['QQQ']['atr'] = 1
    assert observe_factor(snapshot, context, event)['keep']


def test_total_return_compounds_twenty_real_sessions_and_counts_ex_date_dividends():
    snapshot, context, event = stock_history()
    day = snapshot.times[-1].date().isoformat()
    snapshot.actions = [{'date': day, 'symbol': 'SPY', 'type': 'dividend', 'amount_per_share': '2'}]
    result = observe_factor(snapshot, context, event)
    assert result['sessions'] == 20
    close = next(r['close'] for r in snapshot.rows if r['symbol'] == 'SPY' and r['date'] == day)
    assert result['values']['SPY']['total_return_20'] == pytest.approx((close + 2)/100 - 1)
    assert result['values']['QQQ']['total_return_20'] == pytest.approx(1.002**20 - 1)
    assert result['lookback_start'] == '2024-06-03'


def test_tied_etf_scores_do_not_trigger_a_switch():
    snapshot, context, event = stock_history()
    for row in snapshot.rows:
        row['close'] = 100
    context.features['QQQ']['atr'] = 1
    result = observe_factor(snapshot, context, event)
    assert result['valid'] and not result['keep']


def test_future_candles_and_dividends_do_not_change_the_origin_filter():
    snapshot, context, event = stock_history()
    expected = observe_factor(snapshot, context, event)
    next_day = context.as_of + timedelta(days=1)
    snapshot.times.append(next_day)
    snapshot.rows.append({'date': next_day.date().isoformat(), 'symbol': 'QQQ', 'close': 99999})
    snapshot.actions.append({'date': next_day.date().isoformat(), 'symbol': 'QQQ', 'type': 'dividend',
                             'amount_per_share': '999999'})
    assert observe_factor(snapshot, context, event) == expected


@pytest.mark.parametrize('missing', ['short', 'close', 'atr', 'zero'])
def test_invalid_history_cannot_pass_coverage(missing):
    snapshot, context, event = stock_history()
    if missing == 'short':
        context.as_of = snapshot.times[19]
    elif missing == 'close':
        snapshot.rows.pop()
    elif missing == 'atr':
        context.features['SPY']['atr'] = float('nan')
    else:
        context.features['SPY']['atr'] = 0
    result = observe_factor(snapshot, context, event)
    assert not result['valid'] and not result['keep']


def test_cached_baseline_requires_the_exact_original_source(tmp_path):
    path = tmp_path/'engine.py'
    path.write_text('execute = 1\n')
    digest = source_tree_hash(tmp_path)
    manifest = {'source_hash': digest, 'files': {'engine.py': hashlib.sha256(path.read_bytes()).hexdigest()}}
    validate_baseline_code(manifest, tmp_path, digest)
    with pytest.raises(ValueError, match='source hash'):
        validate_baseline_code(manifest, tmp_path, 'unrelated baseline')
    path.write_text('execute = 2\n')
    with pytest.raises(ValueError, match='execution source changed'):
        validate_baseline_code(manifest, tmp_path, digest)


def test_family_retains_all_128_prior_tests_and_positive_net_alone_cannot_qualify():
    analysis, calendars = fixture()
    crypto = screen_market(analysis, calendars)
    crypto['hypothesis'] = HYPOTHESES[0]['id']
    stocks = deepcopy(crypto)
    stocks.update(market='stocks', hypothesis=HYPOTHESES[1]['id'])
    result = finalize([crypto, stocks], [{'p_value': 1.0}]*128)
    assert result['correction_family_size'] == 130 and all(result['pool_by_market'].values())
    assert result['baseline_replays_reused'] == 16 and result['new_baseline_replays'] == 0
    assert not result['formal_admission'] and not result['trading_enabled']
    bad, calendars = fixture(delta=-.001)
    row = screen_market(bad, calendars)
    row['hypothesis'] = HYPOTHESES[0]['id']
    assert not finalize([row, stocks], [{'p_value': 1.0}]*128)['pool_by_market']['crypto']
    with pytest.raises(ValueError, match='prior exposures'):
        finalize([crypto, stocks], [{'p_value': 1.0}]*126)


@pytest.mark.parametrize('change', ['trading', 'threshold'])
def test_protocol_refuses_changed_scope_or_definition(change):
    protocol = {'id': 'relative_leadership_factors_v1', 'stage': 'development_only',
        'model_calls_authorized': 0, 'trading_enabled': False, 'formal_admission': False,
        'sealed_holdout_evaluated': False, 'hypotheses': deepcopy(HYPOTHESES), 'screening': deepcopy(SCREEN)}
    if change == 'trading':
        protocol['trading_enabled'] = True
    else:
        protocol['hypotheses'][1]['lookback_sessions'] = 5
    with pytest.raises(ValueError):
        validate(protocol)
