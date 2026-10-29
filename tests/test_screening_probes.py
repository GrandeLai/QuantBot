from copy import deepcopy
from datetime import datetime

import pytest

from quantbot.ai.replay_evidence import inspect_replay
from quantbot.factors.conditional_study import file_ref, save
from quantbot.factors.mechanism_probes import KINDS, run_probe
from quantbot.factors.screening_probes import (
    FILTERS, provider_for, research_cards, screening_origins, screening_panel, targets,
)
from quantbot.factors.screening_study import run
from tests.test_dividend_replan import dividend_fixture
from tests.test_mechanism_probes import card, fixture_snapshot


def cards():
    return research_cards(card(KINDS[1], ['BTC/USDT', 'ETH/USDT']),
                          card(KINDS[2], ['SPY', 'QQQ', 'IWM', 'IEF', 'GLD']))


@pytest.mark.parametrize('market', ['crypto', 'stocks'])
def test_new_features_are_prefix_invariant(market):
    config, _, snapshot = fixture_snapshot(market)
    full = screening_panel(snapshot)
    prefix = type(snapshot)(market, snapshot.rows[:180*len(config['market']['symbols'])],
                            config, snapshot.source, snapshot.calendar, snapshot.actions)
    assert all(full[key] == value for key, value in screening_panel(prefix).items())


def test_onset_is_once_per_contiguous_episode_and_same_dates_for_both_crypto_cards():
    _, _, snapshot = fixture_snapshot('crypto')
    panel = screening_panel(snapshot)
    selected = snapshot.times[130:230]
    window = {'id': 'w', 'start': str(selected[0].date()), 'end': str(selected[-1].date()),
              'expected_bars': len(selected), 'first_close': selected[0].isoformat(),
              'last_close': selected[-1].isoformat()}
    for i in (151, 152, 153, 157, 158):
        for symbol in ('BTC/USDT', 'ETH/USDT'):
            panel[(snapshot.times[i].isoformat(), symbol)].update(zscore20=-2.1,
                previous_zscore20=-1 if i in (151, 157) else -2.1)
    expected = [snapshot.times[i] for i in (151, 157)]
    assert screening_origins(cards()[0], snapshot, panel, window) == expected
    assert screening_origins(cards()[1], snapshot, panel, window) == expected


def test_filtered_weights_stay_cash_and_cost_hurdle_does_not_move_with_fees():
    config, _, snapshot = fixture_snapshot('stocks')
    panel = screening_panel(snapshot)
    at = snapshot.times[180].isoformat()
    for symbol in config['market']['symbols']:
        panel[(at, symbol)].update(tr63=.1, tr21=.01, tr_index=1.2, tr_sma126=1.1)
    panel[(at, 'IEF')]['tr21'] = 0
    chosen, valid = targets(cards()[2], panel, at, config, candidate=True)
    assert valid and len(chosen) == 4 and set(chosen.values()) == {.12}
    panel[(at, 'IEF')]['tr63'] = .0378
    chosen, _ = targets(cards()[3], panel, at, config, candidate=True)
    assert 'IEF' not in chosen and set(chosen.values()) == {.12}
    stressed = deepcopy(config)
    stressed['execution']['cost_bps'] = 30
    assert targets(cards()[3], panel, at, stressed, candidate=True)[0] == chosen
    panel[(at, 'IEF')]['tr63'] = .03780001
    assert 'IEF' in targets(cards()[3], panel, at, config, candidate=True)[0]


def execute(tmp_path, config, spec, snapshot, panel, item, *, candidate=True, name='probe'):
    at = snapshot.times[180]
    provider = provider_for(item, panel, at.isoformat(), config, snapshot.actions or [], candidate=candidate)
    path = tmp_path/(name+'.jsonl')
    result = run_probe(snapshot, item, panel, at, config, spec, path, candidate=candidate, provider=provider)
    accounts = [r['payload'] for r in inspect_replay(path)['records'] if r['kind'] == 'account']
    return result, accounts, provider


def recovery_fixture(confirm=1, exit_z=-1):
    config, spec, snapshot = fixture_snapshot('crypto')
    panel = screening_panel(snapshot)
    for i in range(180, 191):
        for symbol in config['market']['symbols']:
            panel[(snapshot.times[i].isoformat(), symbol)].update(
                zscore20=exit_z if i >= 180+confirm else -2.1,
                previous_zscore20=-1 if i == 180 else -2.1)
    return config, spec, snapshot, panel


@pytest.mark.parametrize('confirm,buys', [(1, 2), (3, 2), (4, 0)])
def test_recovery_uses_completed_close_then_next_open_with_fixed_deadline(tmp_path, confirm, buys):
    config, spec, snapshot, panel = recovery_fixture(confirm)
    result, accounts, provider = execute(tmp_path, config, spec, snapshot, panel, cards()[1])
    filled = [(i, o) for i, a in enumerate(accounts) for o in a['orders']
              if o['status'] == 'filled' and o['side'] == 'buy']
    assert result['buy_fills'] == buys
    # Crypto's next candle opens exactly at the completed candle's timestamp.
    assert all(i == confirm+1 and datetime.fromisoformat(o['at']) == snapshot.times[180+confirm]
               for i, o in filled)
    assert len(provider.entry_attempts) == buys
    assert result['end_at'] == snapshot.times[190].isoformat()


def test_recovery_skips_price_already_at_exit_and_does_not_retry_canceled_buy(tmp_path):
    config, spec, snapshot, panel = recovery_fixture(exit_z=0)
    result, _, provider = execute(tmp_path, config, spec, snapshot, panel, cards()[1], name='exit')
    assert not result['buy_fills'] and not provider.entry_attempts
    config, spec, snapshot, panel = recovery_fixture()
    for row in snapshot.rows[182*2:183*2]:
        row.update(open=200, high=201, low=199, close=200)
    snapshot = snapshot.with_config(config)
    result, _, provider = execute(tmp_path, config, spec, snapshot, panel, cards()[1], name='cancel')
    assert not result['buy_fills'] and len(provider.entry_attempts) == 2
    assert len(result['cancellations']) == 2


def test_recovery_does_not_reenter_after_original_stop(tmp_path):
    config, spec, snapshot, panel = recovery_fixture()
    for row in snapshot.rows[182*2:183*2]:
        row.update(low=80, high=150)
    snapshot = snapshot.with_config(config)
    result, _, provider = execute(tmp_path, config, spec, snapshot, panel, cards()[1])
    assert result['buy_fills'] == 2 and result['exit_reasons'] == ['protective_stop']*2
    assert len(provider.entry_attempts) == 2 and not result['remaining_positions']


def test_trend_filter_is_evaluated_at_origin_and_missing_feature_blocks(tmp_path):
    config, spec, snapshot, panel = recovery_fixture()
    at = snapshot.times[180].isoformat()
    panel[(at, 'BTC/USDT')]['return60'] = -.01
    chosen, valid = targets(cards()[0], panel, at, config, candidate=True)
    assert valid and set(chosen) == {'ETH/USDT'}
    assert len(targets(cards()[0], panel, at, config, candidate=False)[0]) == 2
    panel[(at, 'BTC/USDT')]['return60'] = None
    assert targets(cards()[0], panel, at, config, candidate=True) == ({}, False)


def test_stock_control_exactly_matches_existing_replan_and_retry_recomputes_filter(tmp_path):
    config, spec, snapshot = dividend_fixture()
    panel = screening_panel(snapshot)
    base = card(KINDS[2], config['market']['symbols'])
    old = run_probe(snapshot, base, panel, snapshot.times[180], config, spec,
                    tmp_path/'old.jsonl', candidate=True, replan=True)
    control, _, _ = execute(tmp_path, config, spec, snapshot, panel, cards()[2], candidate=False, name='control')
    for key in ('net_return', 'buy_fills', 'fills', 'costs', 'dividend_income', 'replans'):
        assert control[key] == old[key]
    panel[(snapshot.times[181].isoformat(), 'IEF')]['tr21'] = -.01
    candidate, accounts, _ = execute(tmp_path, config, spec, snapshot, panel, cards()[2], name='candidate')
    assert candidate['replans'] and 'IEF' not in candidate['replans'][0]['targets']
    assert all(o['symbol'] != 'IEF' for a in accounts for o in a['orders'] if o['status'] == 'filled')


def test_freeze_tamper_prevents_any_output(tmp_path):
    protocol, frozen, output = tmp_path/'protocol.json', tmp_path/'freeze.json', tmp_path/'output'
    save(protocol, {})
    save(frozen, {'protocol': file_ref(protocol), 'source_hash': 'changed'})
    with pytest.raises(ValueError, match='冻结代码'):
        run(protocol, frozen, output)
    assert not output.exists()


def test_four_hypotheses_have_no_parameter_search_and_no_sector_substitution():
    items = cards()
    assert [c['screening_filter'] for c in items] == list(FILTERS)
    assert all(c['definition']['parameters_to_optimize'] == [] for c in items)
    assert items[2]['symbols'] == items[3]['symbols'] == ['SPY', 'QQQ', 'IWM', 'IEF', 'GLD']
