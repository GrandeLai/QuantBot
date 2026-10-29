"""正式 Paper 收盘取证；只有券商只读调用，全部事实一次事务追加。"""

from __future__ import annotations

import fcntl
import json
import math
from pathlib import Path

from ..broker.base import AccountInfo, Broker
from ..config import config_hash
from ..data.session import MarketSession, market_now
from ..governance.provenance import source_fingerprint
from ..review import ReviewJournal
from ..review.paper import (
    EQUITY_MISSING, RECONCILIATION_MISSING, PaperCampaignSpec, PaperDayAssessment,
    assess_paper_session,
)
from .paths import reject_legacy_state, runtime_paths
from .recovery import assert_safe_to_trade, inspect_startup_recovery


def collect_reconciliations(
    broker: Broker, campaign: PaperCampaignSpec, state: dict, account: AccountInfo,
) -> list[dict]:
    """只读取得三类核对结果，未知结果显式保留，统一交给账本事务追加。"""
    results: list[dict] = []

    def record(**value):
        results.append(value)

    expected_positions = {
        symbol: int(item.get("direction", 1)) * int(item.get("qty", 0))
        for symbol, item in sorted(state.items())
        if int(item.get("qty", 0)) != 0
    }
    actual_positions: dict[str, int] | None = None
    position_values: dict[str, float] | None = None
    try:
        broker_positions = broker.get_positions()
        actual_positions = {
            symbol: int(position.shares)
            for symbol, position in sorted(broker_positions.items())
            if int(position.shares) != 0
        }
        position_values = {
            symbol: float(position.market_value)
            for symbol, position in sorted(broker_positions.items())
        }
        position_status = (
            "MATCH" if actual_positions == expected_positions else "DRIFT"
        )
        position_actual: object = actual_positions
    except Exception as exc:  # noqa: BLE001 - 对账读取未知必须显式保留
        position_status = "UNKNOWN"
        position_actual = {
            "error": f"{type(exc).__name__}: {exc}",
        }
    record(
        category="positions",
        status=position_status,
        expected=expected_positions,
        actual=position_actual,
    )

    try:
        working = broker.list_open_orders()
        actual_orders = [
            {
                "order_id": order.order_id,
                "client_order_id": order.client_order_id,
                "symbol": order.symbol,
                "side": order.side,
                "quantity": order.quantity,
                "status": order.status,
            }
            for order in working
        ]
        working_status = "MATCH" if not actual_orders else "DRIFT"
        working_actual: object = actual_orders
    except Exception as exc:  # noqa: BLE001 - 空列表不能冒充读取失败
        working_status = "UNKNOWN"
        working_actual = {"error": f"{type(exc).__name__}: {exc}"}
    record(
        category="working_orders",
        status=working_status,
        expected=[],
        actual=working_actual,
    )

    account_expected: dict[str, object] = {
        "account_fingerprint": campaign.account_fingerprint,
        "identity": "equity ~= cash + signed position market value",
    }
    account_actual: dict[str, object] = {
        "equity": float(account.equity),
        "cash": float(account.cash),
        "buying_power": float(account.buying_power),
    }
    if actual_positions is None or position_values is None:
        account_status = "UNKNOWN"
        account_actual["error"] = "positions unavailable"
    else:
        implied_equity = float(account.cash) + sum(position_values.values())
        tolerance = max(1.0, abs(float(account.equity)) * 0.0001)
        account_actual.update(
            {
                "implied_equity": implied_equity,
                "tolerance": tolerance,
            }
        )
        finite = all(
            math.isfinite(float(value))
            for value in (
                account.equity,
                account.cash,
                account.buying_power,
                implied_equity,
            )
        )
        account_status = (
            "MATCH"
            if finite and abs(float(account.equity) - implied_equity) <= tolerance
            else "DRIFT"
        )
    record(
        category="account",
        status=account_status,
        expected=account_expected,
        actual=account_actual,
    )
    return results


def collect_paper_close(
    broker: Broker, cfg: dict, campaign: PaperCampaignSpec,
) -> PaperDayAssessment:
    """采集当日实际收盘后的账户事实；重跑只返回已有结果，不追认旧日期。"""
    if cfg.get("broker", {}).get("paper") is not True:
        raise ValueError("paper-close 要求 broker.paper: true")
    if config_hash(cfg) != campaign.candidate_config_hash:
        raise ValueError("Paper 收盘配置哈希与 campaign 不一致")
    if source_fingerprint(Path(__file__).resolve().parents[4]).source_hash != campaign.source_hash:
        raise ValueError("Paper 收盘源码哈希与 campaign 不一致")
    fingerprint = broker.account_fingerprint()
    if fingerprint != campaign.account_fingerprint:
        raise ValueError("Paper 收盘账户与 campaign 不一致")
    reject_legacy_state(cfg, "paper")
    paths = runtime_paths(cfg, "paper", fingerprint)
    now = market_now()
    day = now.date().isoformat()
    if day not in campaign.expected_trading_dates:
        raise ValueError("今天不在 Paper campaign 冻结交易日内，禁止补录历史收盘")
    session = broker.get_market_session(day)
    if not session.contains_close_observation(now):
        raise ValueError("必须在交易所当日实际收盘后采集证据")
    if not paths.journal_file.is_file() or not paths.state_file.is_file():
        raise FileNotFoundError("该账户尚无可交接的 Paper 执行账本和状态")

    with paths.state_file.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with ReviewJournal(paths.journal_file) as journal:
            assert_safe_to_trade(inspect_startup_recovery(
                journal, broker, state_path=paths.state_file
            ))
            records = journal.records_for_date(day)
            assessment = assess_paper_session(campaign, day, records)
            # 已经采集过收盘事实时，不用新价格覆盖或挑选更好的一次结果。
            close_rows = [row for row in records["equity_snapshots"]
                          if json.loads(row["metadata_json"]).get("phase") == "close"
                          and json.loads(row["metadata_json"]).get("campaign_id")
                          == campaign.campaign_id]
            if close_rows:
                return assessment
            unexpected = set(assessment.reason_codes) - {EQUITY_MISSING, RECONCILIATION_MISSING}
            if unexpected:
                raise ValueError("开盘执行证据不完整，拒绝收盘交接：" + ", ".join(sorted(unexpected)))
            started = next(row for row in records["runs"] if row["status"] == "started"
                           and json.loads(row["metadata_json"]).get("attempt_id") == assessment.attempt_id)
            metadata = json.loads(started["metadata_json"])
            if MarketSession.from_dict(metadata["market_session"]) != session:
                raise ValueError("券商会话与开盘时冻结的会话不一致")
            state = json.loads(paths.state_file.read_text(encoding="utf-8"))["positions"]
            account = broker.get_account()
            reconciliations = collect_reconciliations(broker, campaign, state, account)
            observed = market_now()
            if not session.contains_close_observation(observed):
                raise ValueError("采样已经跨过交易日，禁止追认收盘")
            common = {
                "phase": "close", "attempt_id": assessment.attempt_id,
                "campaign_id": campaign.campaign_id,
                "campaign_sha256": campaign.sha256,
                "account_fingerprint": fingerprint,
                "market_session": session.to_dict(),
            }
            prefix = f"paper-close:{assessment.attempt_id}"
            with journal.transaction() as writer:
                writer.append_equity_snapshot(
                    idempotency_key=f"{prefix}:equity", run_id=started["id"],
                    equity=account.equity, cash=account.cash,
                    buying_power=account.buying_power, position_count=len(state),
                    market_level=metadata.get("market_level"), metadata=common,
                    occurred_at=observed,
                )
                for result in reconciliations:
                    writer.append_reconciliation(
                        idempotency_key=f"{prefix}:{result['category']}", run_id=started["id"],
                        **result, occurred_at=observed,
                        details={**common, "target_attempt_id": assessment.attempt_id},
                    )
            return assess_paper_session(campaign, day, journal.records_for_date(day))
