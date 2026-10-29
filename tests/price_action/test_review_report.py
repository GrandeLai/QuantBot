"""按交易日生成的中文价格行为复盘报告。"""

from __future__ import annotations

from quantbot.price_action.review import (
    ReviewJournal,
    render_daily_review,
    stable_client_order_id,
    write_daily_review,
)


def test_empty_daily_report_marks_every_missing_evidence_boundary(tmp_path):
    path = tmp_path / "journal.sqlite"
    with ReviewJournal(path):
        pass

    report = render_daily_review(path, "2026-08-08")

    assert "价格行为交易复盘（2026-08-08）" in report
    assert "无运行样本" in report
    assert "无决策样本" in report
    assert "无订单样本" in report
    assert "无成交样本" in report
    assert "无对账样本" in report
    assert "无持仓事件样本" in report
    assert "无权益快照样本" in report
    assert "缺记录不等于无事件" in report


def test_populated_daily_report_summarizes_data_decisions_execution_and_account(
    tmp_path,
):
    path = tmp_path / "journal.sqlite"
    with ReviewJournal(path) as journal:
        with journal.transaction() as tx:
            run_id = tx.append_run(
                idempotency_key="run:2026-08-07",
                trading_date="2026-08-07",
                mode="paper",
                status="completed",
                signal_date="2026-08-06",
                data_as_of="2026-08-06",
                data_status="fresh",
                data_hash="snapshot-sha256",
            )
            accepted = tx.append_decision(
                idempotency_key="decision:AAPL:enter",
                run_id=run_id,
                symbol="AAPL",
                action="enter",
                outcome="accepted",
                reason_code="BREAKOUT_55D",
                evidence={"source": "price_action", "trigger": "breakout"},
            )
            tx.append_decision(
                idempotency_key="decision:MSFT:reject",
                run_id=run_id,
                symbol="MSFT",
                action="enter",
                outcome="rejected",
                reason_code="SECTOR_LIMIT",
                evidence={"source": "price_action", "trigger": "pullback"},
            )
            order_id = tx.append_order(
                idempotency_key="order:AAPL:filled",
                run_id=run_id,
                decision_id=accepted,
                client_order_id=stable_client_order_id(accepted),
                broker_order_id="broker-1",
                symbol="AAPL",
                side="buy",
                quantity=10,
                status="filled",
                reference_price=100.0,
            )
            tx.append_fill(
                idempotency_key="fill:broker-1",
                run_id=run_id,
                order_id=order_id,
                symbol="AAPL",
                side="buy",
                quantity=10,
                price=100.05,
                reference_price=100.0,
            )
            tx.append_position_event(
                idempotency_key="position:AAPL:open",
                run_id=run_id,
                decision_id=accepted,
                order_id=order_id,
                symbol="AAPL",
                event_type="opened",
                quantity_delta=10,
                quantity_after=10,
                price=100.05,
            )
            tx.append_equity_snapshot(
                idempotency_key="equity:open",
                run_id=run_id,
                equity=100_000,
                cash=100_000,
                buying_power=100_000,
                position_count=0,
                occurred_at="2026-08-07T13:30:00Z",
            )
            tx.append_equity_snapshot(
                idempotency_key="equity:close",
                run_id=run_id,
                equity=100_200,
                cash=98_999.5,
                buying_power=98_999.5,
                position_count=1,
                occurred_at="2026-08-07T20:00:00Z",
            )
            tx.append_reconciliation(
                idempotency_key="reconcile:AAPL",
                run_id=run_id,
                category="position",
                status="matched",
                symbol="AAPL",
                expected={"quantity": 10},
                actual={"quantity": 10},
            )

    report = render_daily_review(path, "2026-08-07")

    assert "## 一、数据复盘" in report
    assert "数据状态：fresh 1" in report
    assert "决策：2 条（accepted 1、rejected 1）" in report
    assert "拒绝原因：SECTOR_LIMIT 1" in report
    assert "价格行为证据覆盖：2/2" in report
    assert "订单事件：1 条（filled 1）" in report
    assert "成交：1 笔，共 10 股" in report
    assert "中位 +5.0bp" in report
    assert "对账：1 条（matched 1）" in report
    assert "持仓事件：1 条（opened 1）" in report
    assert "最新权益：$100,200.00" in report
    assert "当日账本内权益变化：+0.20%" in report
    assert "单日结果不足以支持策略调参" in report

    output = write_daily_review(path, "2026-08-07", tmp_path / "reports" / "daily.md")
    assert output.read_text(encoding="utf-8") == report


def test_report_is_strictly_filtered_by_run_trading_date(tmp_path):
    path = tmp_path / "journal.sqlite"
    with ReviewJournal(path) as journal:
        first = journal.append_run(
            idempotency_key="run:first",
            trading_date="2026-08-07",
            mode="paper",
            status="completed",
        )
        second = journal.append_run(
            idempotency_key="run:second",
            trading_date="2026-08-08",
            mode="paper",
            status="completed",
        )
        journal.append_decision(
            idempotency_key="decision:first",
            run_id=first,
            symbol="AAPL",
            action="enter",
            outcome="accepted",
            reason_code="BREAKOUT_55D",
            evidence={"trigger": "breakout"},
        )
        journal.append_decision(
            idempotency_key="decision:second",
            run_id=second,
            symbol="MSFT",
            action="enter",
            outcome="rejected",
            reason_code="RISK_LIMIT",
            evidence={"trigger": "pullback"},
        )

    report = render_daily_review(path, "2026-08-07")
    assert "accepted 1" in report
    assert "rejected" not in report
    assert "RISK_LIMIT" not in report
