"""本地模拟券商（SimBroker）：不需要任何 API 密钥的模拟盘。

================== 它解决什么问题 ==================
在拿到 Alpaca 密钥之前，就可以用 `python main.py sim` 启动每日 cron，
让完整的实盘流程（信号 → 订单 → 状态 → 权益落盘 → 绩效报告）
真实地跑起来、开始积累 `report` 命令所需的运行数据。
密钥到位后可单独启用 paper，账户状态和证据分别积累。

成交模型（诚实的近似）：
    市价单立即以"实盘缓存中该股票最新收盘价 ± 5bp 滑点"成交。
    runner 在下单前刚刷新过 data_cache_live，该价格是可得的最新价
    （盘中运行时通常是昨日收盘或当日早盘价）。这比 Alpaca 真实撮合
    乐观/悲观的方向不定，但量级与回测假设一致。

状态持久化：
    runtime/sim/<账户SHA256>/sim_broker_state.json —— 现金、持仓、单调订单
    序号、client ID 映射、订单终态与成交结果；原子写入。须与策略账本成套保留，
    不得通过删除状态文件绕过恢复检查。
====================================================
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pandas as pd

from .base import AccountInfo, Broker, BrokerOrderSnapshot, BrokerPosition

SLIPPAGE = 0.0005  # 与回测假设一致的 5bp 单边滑点
STATE_VERSION = 2


class SimBroker(Broker):
    """本地持久化的模拟券商。"""

    def __init__(self, cfg: dict, state_path: str | Path | None = None):
        from ..live.paths import local_account_fingerprint, reject_legacy_state, runtime_paths

        self.cfg = cfg
        if state_path is None:
            reject_legacy_state(cfg, "sim")
            self._fingerprint = local_account_fingerprint(cfg, "sim")
            paths = runtime_paths(cfg, "sim", self._fingerprint)
            self.state_path = paths.broker_state_file
            self.cache_dir = paths.cache_dir
        else:
            self.state_path = Path(state_path)
            self._fingerprint = hashlib.sha256(
                f"quantbot.price_action:sim:{self.state_path.resolve()}".encode("utf-8")
            ).hexdigest()
            self.cache_dir = Path(cfg.get("live", {}).get("cache_dir", "data_cache_live"))
        # 行情来源：实盘缓存目录（runner 每次运行前会刷新它）
        self._load()
        print(f"[broker] 本地模拟盘（SimBroker），权益 ${self.equity():,.0f}，"
              f"状态文件 {self.state_path}")

    # ---------- 状态 ----------

    def _load(self) -> None:
        if self.state_path.exists():
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.cash: float = float(raw["cash"])
            self.positions: dict[str, dict] = dict(raw["positions"])
            self._next_id = int(raw.get("next_order_seq", 0))
            self._orders: dict[str, dict] = {
                str(order_id): dict(record)
                for order_id, record in raw.get("orders", {}).items()
            }
        else:
            self.cash = float(self.cfg["backtest"]["initial_capital"])
            self.positions = {}
            self._next_id = 0
            self._orders = {}

        # v1 状态没有订单流水；v2 从持久化流水恢复成交查询和幂等映射。
        self._fills: dict[str, tuple[int, float]] = {}
        self._client_order_ids: dict[str, str] = {}
        for order_id, record in self._orders.items():
            client_order_id = record.get("client_order_id")
            if client_order_id:
                client_order_id = str(client_order_id)
                previous = self._client_order_ids.setdefault(client_order_id, order_id)
                if previous != order_id:
                    raise RuntimeError(
                        f"SimBroker 状态损坏：client_order_id {client_order_id!r} "
                        "对应多个订单"
                    )
            if record.get("status") == "filled":
                qty = int(record.get("fill_qty", 0))
                price = float(record.get("fill_price", 0.0))
                if qty > 0 and price > 0:
                    self._fills[order_id] = (qty, price)

            # 兼容手工迁移过来的 v2 文件：即使 next_order_seq 缺失，也不重用
            # 已经出现过的 sim-N。
            prefix, separator, suffix = order_id.partition("-")
            if prefix == "sim" and separator and suffix.isdigit():
                self._next_id = max(self._next_id, int(suffix))

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f"{self.state_path.name}.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "version": STATE_VERSION,
                    "cash": round(self.cash, 2),
                    "positions": self.positions,
                    "next_order_seq": self._next_id,
                    "orders": self._orders,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.state_path)

    # ---------- 行情 ----------

    def _latest_close(self, symbol: str) -> float | None:
        """实盘缓存中该股票的最新收盘价。"""
        path = self.cache_dir / f"{symbol}.parquet"
        if not path.exists():
            return None
        df = pd.read_parquet(path)
        if len(df) == 0:
            return None
        return float(df["close"].iloc[-1])

    # ---------- 账户 ----------

    def equity(self) -> float:
        total = self.cash
        for sym, p in self.positions.items():
            px = self._latest_close(sym) or p["avg_price"]
            total += p["shares"] * px   # 空头 shares 为负，自然做减法
        return total

    def get_account(self) -> AccountInfo:
        eq = self.equity()
        return AccountInfo(equity=eq, cash=self.cash, buying_power=max(0.0, self.cash))

    def account_fingerprint(self) -> str:
        return self._fingerprint

    def get_positions(self) -> dict[str, BrokerPosition]:
        out = {}
        for sym, p in self.positions.items():
            px = self._latest_close(sym) or p["avg_price"]
            out[sym] = BrokerPosition(
                symbol=sym, shares=int(p["shares"]),
                avg_price=float(p["avg_price"]),
                market_value=p["shares"] * px,
            )
        return out

    def _order_snapshot(self, order_id: str, record: dict) -> BrokerOrderSnapshot:
        qty_signed = int(record.get("qty_signed", 0))
        fill_price = record.get("fill_price")
        return BrokerOrderSnapshot(
            order_id=order_id,
            client_order_id=(
                str(record["client_order_id"])
                if record.get("client_order_id")
                else None
            ),
            symbol=str(record.get("symbol", "")),
            side="buy" if qty_signed > 0 else "sell",
            quantity=abs(qty_signed),
            filled_quantity=int(record.get("fill_qty", 0)),
            filled_avg_price=(
                None if fill_price in (None, "") else float(fill_price)
            ),
            status=str(record.get("status", "unknown")).lower(),
        )

    def get_order_by_client_id(
        self,
        client_order_id: str,
    ) -> BrokerOrderSnapshot | None:
        """读取持久化模拟订单；未知键明确表示不存在。"""
        if not isinstance(client_order_id, str) or not client_order_id.strip():
            raise ValueError("client_order_id 必须是非空字符串")
        order_id = self._client_order_ids.get(client_order_id)
        if order_id is None:
            return None
        record = self._orders.get(order_id)
        if record is None:
            raise RuntimeError(
                f"SimBroker 状态损坏：client_order_id {client_order_id!r} "
                "引用不存在的订单"
            )
        return self._order_snapshot(order_id, record)

    def list_open_orders(self) -> list[BrokerOrderSnapshot]:
        """返回所有非可信终态订单，包含崩溃留下的 ``created`` 订单。"""
        snapshots = [
            self._order_snapshot(order_id, record)
            for order_id, record in self._orders.items()
        ]
        return [snapshot for snapshot in snapshots if snapshot.is_open]

    # ---------- 撮合 ----------

    def _allocate_order_id(self) -> str:
        """分配不会因进程重启而重用的模拟券商订单号。"""
        self._next_id += 1
        order_id = f"sim-{self._next_id}"
        while order_id in self._orders:
            self._next_id += 1
            order_id = f"sim-{self._next_id}"
        return order_id

    def _execute(
        self,
        symbol: str,
        qty_signed: int,
        client_order_id: str | None = None,
    ) -> str:
        """立即撮合一笔市价单（qty_signed：正买负卖）。

        确定性 ``client_order_id`` 已出现过时只返回原订单号，不重复记账；
        若同一个幂等键被用于不同订单意图，则拒绝执行而不是猜测调用方意图。
        """
        if qty_signed == 0:
            raise ValueError("SimBroker 下单股数必须大于 0")
        if client_order_id is not None:
            if not isinstance(client_order_id, str) or not client_order_id.strip():
                raise ValueError("client_order_id 必须是非空字符串")
            existing_id = self._client_order_ids.get(client_order_id)
            if existing_id is not None:
                existing = self._orders[existing_id]
                same_intent = (
                    existing.get("symbol") == symbol
                    and int(existing.get("qty_signed", 0)) == qty_signed
                )
                if not same_intent:
                    raise ValueError(
                        f"client_order_id {client_order_id!r} 已用于不同订单"
                    )
                print(
                    f"[broker] SimBroker 复用幂等订单 {client_order_id} "
                    f"→ {existing_id}"
                )
                return existing_id

        oid = self._allocate_order_id()
        record = {
            "client_order_id": client_order_id,
            "symbol": symbol,
            "qty_signed": qty_signed,
            "status": "created",
            "fill_qty": 0,
            "fill_price": None,
        }
        self._orders[oid] = record
        if client_order_id is not None:
            self._client_order_ids[client_order_id] = oid
        # 在读取行情和修改账户前先持久化订单号/幂等键。即使进程随即崩溃，
        # 重启也不会复用订单号，更不会把同一 client ID 当成一笔新单。
        self._save()

        px = self._latest_close(symbol)
        if px is None:
            # 无行情无法撮合：视为拒单（wait_order 将返回 False）
            record["status"] = "rejected"
            self._save()
            print(f"[broker] SimBroker 拒单 {symbol}：实盘缓存无行情")
            return oid
        # 滑点方向：买入付高价、卖出得低价
        fill_px = px * (1 + SLIPPAGE) if qty_signed > 0 else px * (1 - SLIPPAGE)
        self.cash -= qty_signed * fill_px
        pos = self.positions.get(symbol, {"shares": 0, "avg_price": 0.0})
        new_shares = pos["shares"] + qty_signed
        if new_shares == 0:
            self.positions.pop(symbol, None)
        else:
            # 同向加仓更新均价；减仓/反向保持原均价（简化，够用）
            if pos["shares"] * qty_signed > 0 or pos["shares"] == 0:
                total_cost = pos["avg_price"] * abs(pos["shares"]) \
                    + fill_px * abs(qty_signed)
                pos["avg_price"] = total_cost / (abs(pos["shares"]) + abs(qty_signed))
            pos["shares"] = new_shares
            self.positions[symbol] = pos
        fill = (abs(qty_signed), round(fill_px, 4))
        self._fills[oid] = fill
        record.update(status="filled", fill_qty=fill[0], fill_price=fill[1])
        self._save()
        print(f"[broker] SimBroker 成交 {symbol} {qty_signed:+d} @ {fill_px:.2f}")
        return oid

    def market_buy(
        self,
        symbol: str,
        shares: int,
        client_order_id: str | None = None,
    ) -> str:
        return self._execute(symbol, abs(shares), client_order_id)

    def market_sell(
        self,
        symbol: str,
        shares: int,
        client_order_id: str | None = None,
    ) -> str:
        return self._execute(symbol, -abs(shares), client_order_id)

    def wait_order(self, order_id: str, timeout: float = 60.0) -> bool:
        return order_id in self._fills   # 无行情拒单时不在成交表中

    def cancel_order(self, order_id: str) -> bool:
        record = self._orders.get(order_id)
        if record is None:
            return False
        snapshot = self._order_snapshot(order_id, record)
        if snapshot.is_terminal:
            return snapshot.status == "canceled"
        # 正常订单即时撮合；这里只会处理进程崩溃留在 created 的保留单。
        record["status"] = "canceled"
        self._save()
        return True

    def order_fill(self, order_id: str) -> tuple[int, float] | None:
        return self._fills.get(order_id)

    def get_last_price(self, symbol: str) -> float | None:
        return self._latest_close(symbol)

    def is_market_open(self) -> bool:
        """近似交易时段判断：美东工作日 09:30-16:00（不识别假日）。

        假日误触发无害：数据刷新后信号基准日与上次相同，
        幂等性检查会直接跳过本次运行。
        """
        now = pd.Timestamp.now(tz="America/New_York")
        if now.weekday() >= 5:
            return False
        minutes = now.hour * 60 + now.minute
        return 9 * 60 + 30 <= minutes < 16 * 60
