"""复用 PABot 的只追加复盘账本，追加 AI 身份与快照证据。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import fcntl
import json
from pathlib import Path

from quantbot.evidence import hash_payload, source_tree_hash
from quantbot.price_action.review import ReviewJournal

from .config import config_hash
from .domain import MarketContext
from .risk import TradePlan


class RuntimeBusyError(RuntimeError):
    """另一进程持有同一运行目录，调用方可保留队列而不记录执行失败。"""


@contextmanager
def runtime_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".run.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeBusyError("已有 QuantBot 进程正在处理这个账户") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class AIJournal:
    def __init__(self, path: Path, config: dict, mode: str, account: str):
        self.journal = ReviewJournal(path)
        self.config_hash = config_hash(config)
        self.code_hash = source_tree_hash()
        self.mode = mode
        self.account = account

    def run(self, context: MarketContext, status: str, *, metadata=None, day: date | None = None):
        payload = {
            "account_fingerprint": self.account,
            "snapshot_id": context.snapshot_id,
            "run_type": self.mode,
            "code_hash": self.code_hash,
            **(metadata or {}),
        }
        key = hash_payload(
            {
                "account": self.account,
                "snapshot": context.snapshot_id,
                "mode": self.mode,
                "status": status,
                "config": self.config_hash,
                "metadata": payload,
                "day": str(day),
            }
        )
        return self.journal.append_run(
            idempotency_key=key,
            trading_date=day or context.as_of.date(),
            signal_date=context.as_of.date(),
            mode=self.mode,
            status=status,
            data_status="snapshot_validated",
            code_version=self.code_hash,
            config_hash=self.config_hash,
            data_hash=context.snapshot_id,
            metadata=payload,
        )

    def plan(self, context: MarketContext, plan: TradePlan, *, day: date | None = None):
        run_id = self.run(context, "planned", metadata={"plan": plan.payload()}, day=day)
        for allocation in plan.decision.allocations:
            intent = next((item for item in plan.intents if item.symbol == allocation.symbol), None)
            self.journal.append_decision(
                idempotency_key=f"{run_id}:decision:{allocation.symbol}",
                run_id=run_id,
                symbol=allocation.symbol,
                action=intent.side if intent else "hold",
                outcome="risk_checked",
                reason_code="ai_allocation"
                if plan.decision.provider == "openai_compatible"
                else plan.decision.provider,
                intended_quantity=int(intent.quantity) if intent else 0,
                reference_price=context.prices[allocation.symbol],
                evidence={
                    "snapshot_id": context.snapshot_id,
                    "as_of": context.as_of.isoformat(),
                    "model": plan.decision.model,
                    "provider": plan.decision.provider,
                    "allocation": vars(allocation),
                    "features": context.features[allocation.symbol],
                    "risk_adjustments": plan.adjustments,
                },
            )
        return run_id

    def order(self, run_id, intent, status, *, price=None, metadata=None, broker_id=None):
        details = metadata or {}
        return self.journal.append_order(
            idempotency_key=hash_payload(
                {"run": run_id, "order": intent.client_order_id, "status": status, "metadata": details}
            ),
            run_id=run_id,
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=int(intent.quantity),
            status=status,
            reference_price=price,
            metadata=details,
            broker_order_id=broker_id,
        )

    def commit_order(self, run_id, intent, terminal_id, state: dict):
        return self.order(
            run_id,
            intent,
            "state_committed",
            metadata={
                "terminal_order_id": terminal_id,
                "state_sha256": hash_payload(state),
                "broker_state": state,
            },
        )

    def equity(self, run_id, account):
        self.journal.append_equity_snapshot(
            idempotency_key=f"{run_id}:equity",
            run_id=run_id,
            equity=account.equity,
            cash=account.cash,
            buying_power=account.cash,
            position_count=sum(q > 0 for q in account.positions.values()),
            metadata={"positions": dict(account.positions)},
        )

    def existing_runs(self, day: date) -> list[dict]:
        rows = self.journal.records_for_date(day)["runs"]
        return [
            row
            for row in rows
            if row["mode"] == self.mode
            and json.loads(row["metadata_json"])["account_fingerprint"] == self.account
        ]

    def close(self):
        self.journal.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
