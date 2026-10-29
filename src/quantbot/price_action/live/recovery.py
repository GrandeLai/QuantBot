"""启动前的只读订单恢复门禁。

本模块只比较只追加账本与券商事实，不提交、撤销、补发订单，也不修改
``live_state.json``。任何未知、工作订单或未完成的本地状态交接都返回 HOLD；
人工收敛完成前，runner 不得进入行情刷新和交易阶段。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..broker.base import Broker, BrokerOrderSnapshot
from ..review.journal import (
    InvalidStateCheckpointError,
    PendingOrderRecovery,
    ReviewJournal,
)

SAFE = "SAFE"
HOLD = "HOLD"


@dataclass(frozen=True)
class RecoveryIssue:
    code: str
    message: str
    client_order_id: str | None = None
    broker_order_id: str | None = None


@dataclass(frozen=True)
class RecoveryReport:
    status: str
    issues: tuple[RecoveryIssue, ...]
    pending_orders: tuple[PendingOrderRecovery, ...]
    open_orders: tuple[BrokerOrderSnapshot, ...]

    @property
    def safe(self) -> bool:
        return self.status == SAFE


class RecoveryHoldError(RuntimeError):
    """启动恢复无法证明账户安全，禁止进入交易阶段。"""

    def __init__(self, report: RecoveryReport):
        self.report = report
        summary = "；".join(issue.message for issue in report.issues[:3])
        if len(report.issues) > 3:
            summary += f"；另有 {len(report.issues) - 3} 项"
        super().__init__(f"PABot 启动恢复门禁 HOLD：{summary or '状态未知'}")

    @classmethod
    def from_issue(cls, code: str, message: str) -> RecoveryHoldError:
        return cls(
            RecoveryReport(
                status=HOLD,
                issues=(RecoveryIssue(code=code, message=message),),
                pending_orders=(),
                open_orders=(),
            )
        )


def _same_intent(
    pending: PendingOrderRecovery,
    snapshot: BrokerOrderSnapshot,
) -> bool:
    return (
        snapshot.client_order_id == pending.client_order_id
        and snapshot.symbol.upper() == pending.symbol.upper()
        and snapshot.side.lower() == pending.side.lower()
        and snapshot.quantity == pending.quantity
    )


def inspect_startup_recovery(
    journal: ReviewJournal | None,
    broker: Broker,
    *,
    state_path: str | Path | None = None,
) -> RecoveryReport:
    """只读核对全部自有未决订单和账户内全部工作订单。"""
    issues: list[RecoveryIssue] = []
    pending_orders: list[PendingOrderRecovery] = []
    known_client_ids: set[str] = set()

    if journal is None:
        issues.append(
            RecoveryIssue(
                code="journal_unavailable",
                message="交易证据账本未启用，无法证明历史订单已收敛",
            )
        )
    else:
        checkpoint_error = False
        try:
            pending_orders = journal.list_pending_order_recoveries()
            known_client_ids = journal.known_client_order_ids()
            checkpoint = journal.latest_state_checkpoint()
        except InvalidStateCheckpointError as exc:
            checkpoint = None
            checkpoint_error = True
            issues.append(
                RecoveryIssue(
                    code="state_checkpoint_invalid",
                    message=f"账本状态指纹不完整：{exc}",
                )
            )
        except Exception as exc:  # noqa: BLE001 - 账本读取未知必须 fail-closed
            checkpoint = None
            checkpoint_error = True
            issues.append(
                RecoveryIssue(
                    code="journal_read_unknown",
                    message=f"读取交易证据账本失败：{type(exc).__name__}: {exc}",
                )
            )

        if state_path is not None:
            path = Path(state_path)
            exists = path.exists() or path.is_symlink()
            if checkpoint is None and exists and not checkpoint_error:
                issues.append(
                    RecoveryIssue(
                        code="state_checkpoint_missing",
                        message="本地策略状态存在，但账本没有可验证的状态指纹",
                    )
                )
            elif checkpoint is not None:
                if path.is_symlink() or not path.is_file():
                    issues.append(
                        RecoveryIssue(
                            code="state_file_missing_or_unsafe",
                            message="账本存在状态指纹，但本地状态不是普通文件",
                        )
                    )
                else:
                    try:
                        actual_state_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                    except OSError as exc:
                        issues.append(
                            RecoveryIssue(
                                code="state_file_read_unknown",
                                message=f"读取本地策略状态失败：{type(exc).__name__}: {exc}",
                            )
                        )
                    else:
                        if actual_state_hash != checkpoint.state_sha256:
                            issues.append(
                                RecoveryIssue(
                                    code="state_checkpoint_mismatch",
                                    message=(
                                        "本地策略状态与最近账本指纹不一致，"
                                        "可能发生回滚或离线改写"
                                    ),
                                )
                            )

    # 对每一笔自有未决订单做定点查询。即使券商已经终态，本模块也不会
    # 自动补写账本或修复策略状态，只报告 HOLD 交给显式恢复流程。
    for pending in pending_orders:
        try:
            snapshot = broker.get_order_by_client_id(pending.client_order_id)
        except Exception as exc:  # noqa: BLE001 - 券商读取未知必须 HOLD
            issues.append(
                RecoveryIssue(
                    code="owned_order_lookup_unknown",
                    message=(
                        f"无法确认自有订单 {pending.client_order_id}："
                        f"{type(exc).__name__}: {exc}"
                    ),
                    client_order_id=pending.client_order_id,
                    broker_order_id=pending.broker_order_id,
                )
            )
            continue
        if snapshot is None:
            issues.append(
                RecoveryIssue(
                    code="owned_order_not_found",
                    message=(
                        f"账本订单 {pending.client_order_id} 尚未交接，"
                        "券商明确未找到；需要人工关闭该意图"
                    ),
                    client_order_id=pending.client_order_id,
                    broker_order_id=pending.broker_order_id,
                )
            )
            continue
        if not _same_intent(pending, snapshot):
            issues.append(
                RecoveryIssue(
                    code="owned_order_intent_mismatch",
                    message=f"自有订单 {pending.client_order_id} 的券商意图与账本不一致",
                    client_order_id=pending.client_order_id,
                    broker_order_id=snapshot.order_id,
                )
            )
        elif snapshot.is_open:
            issues.append(
                RecoveryIssue(
                    code="owned_order_open",
                    message=(
                        f"自有订单 {pending.client_order_id} 仍在工作中"
                        f"（{snapshot.status}）"
                    ),
                    client_order_id=pending.client_order_id,
                    broker_order_id=snapshot.order_id,
                )
            )
        else:
            issues.append(
                RecoveryIssue(
                    code="owned_order_local_handoff_pending",
                    message=(
                        f"自有订单 {pending.client_order_id} 券商已为终态"
                        f"（{snapshot.status}），但账本/本地状态尚未交接"
                    ),
                    client_order_id=pending.client_order_id,
                    broker_order_id=snapshot.order_id,
                )
            )

    try:
        open_orders = broker.list_open_orders()
    except Exception as exc:  # noqa: BLE001 - 空列表不能冒充读取失败
        open_orders = []
        issues.append(
            RecoveryIssue(
                code="open_orders_read_unknown",
                message=f"读取券商工作订单失败：{type(exc).__name__}: {exc}",
            )
        )

    for order in open_orders:
        owned = (
            order.client_order_id is not None
            and order.client_order_id in known_client_ids
        )
        issues.append(
            RecoveryIssue(
                code="owned_open_order" if owned else "external_open_order",
                message=(
                    f"账户存在{'自有' if owned else '外部/人工'}工作订单 "
                    f"{order.order_id}（{order.symbol} {order.side} {order.status}）"
                ),
                client_order_id=order.client_order_id,
                broker_order_id=order.order_id,
            )
        )

    return RecoveryReport(
        status=HOLD if issues else SAFE,
        issues=tuple(issues),
        pending_orders=tuple(pending_orders),
        open_orders=tuple(open_orders),
    )


def assert_safe_to_trade(report: RecoveryReport) -> None:
    if not report.safe:
        raise RecoveryHoldError(report)
