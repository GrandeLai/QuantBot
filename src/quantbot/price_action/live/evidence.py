"""实盘价格行为证据记录器。

本模块只负责把 runner 已经作出的事实写入只追加账本，不参与选股或
修改参数。订单在调用券商之前先写 ``planned`` 事件，并由决策 ID 生成
确定性的 ``client_order_id``；进程重启后同一决策会复用同一订单身份。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from .. import __version__
from ..config import config_hash
from ..data.session import MarketSession
from ..review import (
    PaperCampaignSpec,
    ReviewJournal,
    stable_client_order_id,
    stable_record_id,
)


_RECOVERY_HOLD_STATUSES = frozenset(
    {"planned", "submitted", "uncertain", "submit_error", "partial", "dead"}
)


def market_data_fingerprint(
    data: Mapping[str, pd.DataFrame],
    asof: pd.Timestamp,
) -> str:
    """计算截至信号日的完整输入行情指纹。

    指纹覆盖代码、列名、索引和全部历史数值。它不是数据仓库的替代品，
    但足以证明两次日决策是否消费了相同的内存输入。
    """
    digest = hashlib.sha256()
    for symbol in sorted(data):
        frame = data[symbol].loc[:asof]
        digest.update(symbol.encode("utf-8"))
        digest.update(json.dumps(list(frame.columns)).encode("utf-8"))
        digest.update(str(tuple(str(dtype) for dtype in frame.dtypes)).encode("utf-8"))
        hashed = pd.util.hash_pandas_object(frame, index=True, categorize=True)
        digest.update(hashed.to_numpy().tobytes())
    return digest.hexdigest()


@dataclass(frozen=True)
class OrderEvidence:
    """一笔订单在证据账本中的稳定身份。"""

    key: str
    decision_id: str
    client_order_id: str
    planned_order_id: str
    symbol: str
    side: str
    quantity: int
    reference_price: float | None
    submitted_order_id: str | None = None
    broker_order_id: str | None = None


class LiveEvidenceRecorder:
    """把一次 runner 运行映射成可审计、幂等的事实事件。"""

    def __init__(
        self,
        cfg: Mapping[str, Any],
        mode: str,
        journal: ReviewJournal | None = None,
        campaign: PaperCampaignSpec | None = None,
        journal_path: str | Path | None = None,
    ) -> None:
        review_cfg = cfg.get("review", {}) or {}
        self.enabled = bool(review_cfg.get("enabled", False))
        if journal is not None:
            self.enabled = True
        self.journal = journal
        self._owns_journal = False
        if self.enabled and self.journal is None:
            path = Path(journal_path or review_cfg.get("journal_path", "logs/trading_journal.sqlite"))
            self.journal = ReviewJournal(path)
            self._owns_journal = True
        self.cfg = cfg
        self.mode = mode
        self.campaign = campaign
        self.run_id: str | None = None
        self.run_key: str | None = None
        self.trading_date: str | None = None
        self.signal_date: str | None = None
        self._data_hash: str | None = None
        self._data_status = "unknown"
        self.attempt_id: str | None = None
        self._identity_metadata: dict[str, Any] = {}
        self._state_sha256: str | None = None
        self._awaiting_state_commit: dict[
            str, tuple[OrderEvidence, str, str]
        ] = {}
        self._recovery_reasons: set[str] = set()

    @property
    def requires_recovery(self) -> bool:
        return bool(self._recovery_reasons)

    @property
    def recovery_reasons(self) -> tuple[str, ...]:
        return tuple(sorted(self._recovery_reasons))

    def close(self) -> None:
        if self._owns_journal and self.journal is not None:
            self.journal.close()
            self.journal = None

    def start(
        self,
        *,
        asof: pd.Timestamp,
        market_level: int,
        data_hash: str,
        data_status: str = "fresh",
        execution_date: pd.Timestamp | str | None = None,
        source_hash: str | None = None,
        account_fingerprint: str | None = None,
        market_session: MarketSession | None = None,
    ) -> str:
        """在任何订单副作用之前写入运行起点。"""
        signal_day = pd.Timestamp(asof).date().isoformat()
        execution_day = (
            pd.Timestamp(execution_date).date().isoformat()
            if execution_date is not None
            else signal_day
        )
        self.trading_date = execution_day
        self.signal_date = signal_day
        campaign_id = self.campaign.campaign_id if self.campaign else "unregistered"
        campaign_sha256 = self.campaign.sha256 if self.campaign else "unregistered"
        self.run_key = (
            f"{self.mode}:{campaign_id}:{campaign_sha256}:"
            f"{execution_day}:signal:{signal_day}"
        )
        self.attempt_id = stable_record_id("attempts", self.run_key)
        self._data_hash = data_hash
        self._data_status = data_status
        self._identity_metadata = {"market_level": int(market_level)}
        if self.campaign is not None:
            if not source_hash or not account_fingerprint:
                raise ValueError("Paper campaign 运行必须提供源码与匿名账户指纹")
            if market_session is None or execution_date is None or not (
                market_session.trading_date == execution_day
                and market_session.signal_date == signal_day
                and market_session.contains_execution(pd.Timestamp(execution_date).to_pydatetime())
            ):
                raise ValueError("Paper 执行必须绑定权威会话及实际盘中时刻")
            self._identity_metadata.update(
                {
                    "attempt_id": self.attempt_id,
                    "campaign_id": self.campaign.campaign_id,
                    "campaign_sha256": self.campaign.sha256,
                    "execution_date": execution_day,
                    "source_hash": source_hash,
                    "account_fingerprint": account_fingerprint,
                    "market_session": market_session.to_dict(),
                }
            )
        run_id = stable_record_id("runs", f"{self.run_key}:started")
        if self.journal is not None:
            occurred_at = None
            if self.campaign is not None:
                occurred_at = pd.Timestamp(execution_date).isoformat()
                # 同一决策的重试保留首次执行时刻；其余内容仍由账本逐字段核对。
                prior = next((row for row in self.journal.records_for_date(execution_day)["runs"]
                              if row["id"] == run_id), None)
                if prior is not None:
                    occurred_at = prior["occurred_at"]
            run_id = self.journal.append_run(
                idempotency_key=f"{self.run_key}:started",
                trading_date=execution_day,
                mode=self.mode,
                status="started",
                signal_date=signal_day,
                data_as_of=signal_day,
                data_status=data_status,
                code_version=__version__,
                config_hash=config_hash(self.cfg),
                data_hash=data_hash,
                metadata=self._identity_metadata,
                occurred_at=occurred_at,
            )
        self.run_id = run_id
        return run_id

    def finish(self, status: str, *, metadata: Mapping[str, Any] | None = None) -> None:
        """追加终态事件；不更新已存在的运行起点。"""
        if self.journal is None or self.run_key is None or self.trading_date is None:
            return
        terminal_metadata = {**self._identity_metadata, **dict(metadata or {})}
        # 状态一旦原子落盘，即使随后通知/对账/心跳失败，新的文件指纹仍是
        # 下次启动必须核对的事实。只在 completed 中记录会错误回退旧指纹。
        if self._state_sha256 is not None:
            terminal_metadata["state_sha256"] = self._state_sha256
        self.journal.append_run(
            idempotency_key=f"{self.run_key}:finished:{status}",
            trading_date=self.trading_date,
            mode=self.mode,
            status=status,
            signal_date=self.signal_date,
            data_as_of=self.signal_date,
            data_status=self._data_status,
            code_version=__version__,
            config_hash=config_hash(self.cfg),
            data_hash=self._data_hash,
            metadata=terminal_metadata,
        )

    def decision(
        self,
        *,
        key: str,
        symbol: str,
        action: str,
        outcome: str,
        reason_code: str,
        evidence: Mapping[str, Any],
        intended_quantity: int | None = None,
        reference_price: float | None = None,
    ) -> str:
        """追加决策；账本关闭时仍返回同一稳定 ID 供订单幂等使用。"""
        run_id = self._require_run()
        full_key = f"{self.run_key}:decision:{key}"
        decision_id = stable_record_id("decisions", full_key)
        if self.journal is not None:
            decision_id = self.journal.append_decision(
                idempotency_key=full_key,
                run_id=run_id,
                symbol=symbol,
                action=action,
                outcome=outcome,
                reason_code=reason_code,
                evidence=evidence,
                intended_quantity=intended_quantity,
                reference_price=reference_price,
            )
        return decision_id

    def plan_order(
        self,
        *,
        key: str,
        decision_id: str,
        symbol: str,
        side: str,
        quantity: int,
        reference_price: float | None,
    ) -> OrderEvidence:
        """在券商调用前持久化订单意图并返回确定性客户端订单 ID。"""
        run_id = self._require_run()
        full_key = f"{self.run_key}:order:{key}"
        client_id = stable_client_order_id(decision_id)
        record_key = f"{full_key}:planned"
        order_id = stable_record_id("orders", record_key)
        if self.journal is not None:
            order_id = self.journal.append_order(
                idempotency_key=record_key,
                run_id=run_id,
                decision_id=decision_id,
                client_order_id=client_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                status="planned",
                reference_price=reference_price,
            )
        return OrderEvidence(
            key=full_key,
            decision_id=decision_id,
            client_order_id=client_id,
            planned_order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            reference_price=reference_price,
        )

    def submitted(self, order: OrderEvidence, broker_order_id: str) -> OrderEvidence:
        """追加券商已接受订单事件。"""
        run_id = self._require_run()
        record_key = f"{order.key}:submitted"
        submitted_id = stable_record_id("orders", record_key)
        if self.journal is not None:
            submitted_id = self.journal.append_order(
                idempotency_key=record_key,
                run_id=run_id,
                decision_id=order.decision_id,
                client_order_id=order.client_order_id,
                broker_order_id=str(broker_order_id),
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                status="submitted",
                reference_price=order.reference_price,
                metadata={"planned_order_id": order.planned_order_id},
            )
        return replace(
            order,
            submitted_order_id=submitted_id,
            broker_order_id=str(broker_order_id),
        )

    def terminal(
        self,
        order: OrderEvidence,
        *,
        status: str,
        fill: tuple[int, float] | None,
    ) -> str:
        """追加订单终态及真实成交事实，返回终态订单事件 ID。"""
        run_id = self._require_run()
        terminal_key = f"{order.key}:terminal:{status}"
        terminal_id = stable_record_id("orders", terminal_key)
        if self.journal is not None:
            terminal_id = self.journal.append_order(
                idempotency_key=terminal_key,
                run_id=run_id,
                decision_id=order.decision_id,
                client_order_id=order.client_order_id,
                broker_order_id=order.broker_order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                status=status,
                reference_price=order.reference_price,
                metadata={"submitted_order_id": order.submitted_order_id},
            )
            if fill is not None and fill[0] > 0:
                self.journal.append_fill(
                    idempotency_key=f"{order.key}:fill:{status}:{fill[0]}",
                    run_id=run_id,
                    order_id=terminal_id,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=int(fill[0]),
                    price=float(fill[1]),
                    reference_price=order.reference_price,
                    metadata={"client_order_id": order.client_order_id},
                )
        normalized_status = status.lower()
        if normalized_status in {"filled", "partial"}:
            # 状态文件尚未原子落盘，订单虽已产生副作用但本地交接未完成。
            self._awaiting_state_commit[order.client_order_id] = (
                order,
                terminal_id,
                normalized_status,
            )
        if normalized_status in _RECOVERY_HOLD_STATUSES:
            self._recovery_reasons.add(
                f"{order.client_order_id}:{normalized_status}"
            )
        return terminal_id

    def state_committed(self, state_sha256: str) -> None:
        """在 ``live_state.json`` 原子替换成功后追加本地交接事实。

        该方法绝不能在状态文件写入前调用。崩溃发生在两者之间时，账本会
        保留 ``filled`` 而没有本事件，下一次启动必须进入 recovery hold。
        """
        if (
            not isinstance(state_sha256, str)
            or len(state_sha256) != 64
            or any(char not in "0123456789abcdef" for char in state_sha256.lower())
        ):
            raise ValueError("state_sha256 必须是完整 SHA-256")
        self._state_sha256 = state_sha256.lower()
        run_id = self._require_run()
        if self.journal is None:
            self._awaiting_state_commit.clear()
            return
        committed: list[str] = []
        with self.journal.transaction() as tx:
            for client_order_id, (order, terminal_id, terminal_status) in (
                self._awaiting_state_commit.items()
            ):
                record_key = f"{order.key}:state_committed"
                tx.append_order(
                    idempotency_key=record_key,
                    run_id=run_id,
                    decision_id=order.decision_id,
                    client_order_id=client_order_id,
                    broker_order_id=order.broker_order_id,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=order.quantity,
                    status="state_committed",
                    reference_price=order.reference_price,
                    metadata={
                        "terminal_order_id": terminal_id,
                        "terminal_status": terminal_status,
                        "state_sha256": self._state_sha256,
                    },
                )
                committed.append(client_order_id)
            tx.append_state_checkpoint(
                idempotency_key=(
                    f"{self.run_key}:state-checkpoint:{self._state_sha256}"
                ),
                run_id=run_id,
                state_sha256=self._state_sha256,
                source="live_state",
            )
        for client_order_id in committed:
            self._awaiting_state_commit.pop(client_order_id, None)

    def position(
        self,
        *,
        key: str,
        order: OrderEvidence | None,
        order_event_id: str | None,
        symbol: str,
        event_type: str,
        quantity_delta: int,
        quantity_after: int,
        price: float | None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if self.journal is None:
            return
        run_id = self._require_run()
        self.journal.append_position_event(
            idempotency_key=f"{self.run_key}:position:{key}",
            run_id=run_id,
            decision_id=order.decision_id if order else None,
            order_id=order_event_id,
            symbol=symbol,
            event_type=event_type,
            quantity_delta=quantity_delta,
            quantity_after=quantity_after,
            price=price,
            metadata=metadata,
        )

    def equity(
        self,
        *,
        equity: float,
        cash: float,
        buying_power: float,
        position_count: int,
        market_level: int,
    ) -> None:
        if self.journal is None:
            return
        self.journal.append_equity_snapshot(
            idempotency_key=f"{self.run_key}:equity:final",
            run_id=self._require_run(),
            equity=equity,
            cash=cash,
            buying_power=buying_power,
            position_count=position_count,
            market_level=market_level,
            metadata={
                "phase": "execution",
                "attempt_id": self.attempt_id,
                "campaign_id": (
                    self.campaign.campaign_id if self.campaign is not None else None
                ),
                "campaign_sha256": (
                    self.campaign.sha256 if self.campaign is not None else None
                ),
                "account_fingerprint": self._identity_metadata.get(
                    "account_fingerprint"
                ),
            },
        )

    def reconciliation(
        self,
        *,
        category: str,
        status: str,
        expected: Any,
        actual: Any,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """把 Paper 收盘核对直接绑定到本次 attempt。"""
        if self.journal is None or self.campaign is None or self.attempt_id is None:
            return
        self.journal.append_reconciliation(
            idempotency_key=f"{self.run_key}:reconciliation:{category}",
            run_id=self._require_run(),
            category=category,
            status=status,
            expected=expected,
            actual=actual,
            details={
                "target_attempt_id": self.attempt_id,
                "campaign_sha256": self.campaign.sha256,
                **dict(details or {}),
            },
        )

    def _require_run(self) -> str:
        if self.run_id is None or self.run_key is None:
            raise RuntimeError("证据记录器尚未 start")
        return self.run_id
