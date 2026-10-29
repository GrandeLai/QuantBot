"""四项冻结机制的有限执行探针；只研究，不注册可交易策略。"""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import math
from zoneinfo import ZoneInfo

import pandas as pd

from quantbot.ai.corporate_actions import check_action_window, positive_amount
from quantbot.ai.development import validate_reconstructed_dividends
from quantbot.ai.domain import Allocation, DecisionBatch
from quantbot.ai.evaluation import replay
from quantbot.ai.feeds import Snapshot
from quantbot.ai.reconciliation import reconcile_replay
from quantbot.ai.replay_evidence import inspect_replay
from quantbot.evidence import hash_payload


KINDS = ("medium_term_trend", "range_mean_reversion", "low_turnover_cross_asset_trend",
         "sector_relative_strength")
SECTORS = ("XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY")
FIELDS = {
    "medium_term_trend": ("return20", "return60", "close", "sma120"),
    "range_mean_reversion": ("zscore20", "sma_gap", "efficiency20"),
    "low_turnover_cross_asset_trend": ("tr63", "tr_index", "tr_sma126"),
    "sector_relative_strength": ("tr63",),
}


def validate_actions(actions, symbols):
    """历史重建允许未知公告时刻，保留 null；数值和事件顺序必须可执行。"""
    validate_reconstructed_dividends([a for a in actions if a["type"] != "split"], symbols)
    identities, sessions = set(), set()
    for action in actions:
        identity = action["id"]
        key = (action["symbol"], action["date"])
        if not identity or identity in identities or key in sessions or action["symbol"] not in symbols:
            raise ValueError("重复或未知的研究公司行动")
        datetime.strptime(action["date"], "%Y-%m-%d")
        if action["type"] == "split" and positive_amount(action["ratio"], "ratio") == 1:
            raise ValueError("拆股比例不能为 1")
        identities.add(identity)
        sessions.add(key)


class ResearchSnapshot(Snapshot):
    """明确隔离的事后拆股重建；不伪造 available_at 或改变生产快照契约。"""

    def _price_context(self, at):
        if at in self._contexts:
            return self._contexts[at]
        day = at.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        splits = [a for a in self.actions or [] if a["type"] == "split" and a["date"] <= day]
        if not splits:
            return super()._price_context(at)
        rows = deepcopy(self.rows)
        for action in splits:
            for row in rows:
                if row["symbol"] == action["symbol"] and row["date"] < action["date"]:
                    ratio = float(action["ratio"])
                    for field in ("open", "high", "low", "close"):
                        row[field] = float(row[field]) / ratio
                    row["volume"] = float(row["volume"]) * ratio
        context = Snapshot(self.market, rows, self.config, self.source, self.calendar).context(at)
        context = replace(context, snapshot_id=hash_payload({"context": context.snapshot_id,
            "ex_post_splits": splits}), features={s: {**v, "history_price_basis": "ex_post_split_reconstruction"}
                                                 for s, v in context.features.items()})
        self._contexts[at] = context
        return context


def feature_panel(snapshot):
    """只读截至每根收盘的价格/已生效事件；总收益用于信号，撮合仍用原始价格。"""
    times = pd.DatetimeIndex(snapshot.times)
    rows = pd.DataFrame(snapshot.rows)
    if snapshot.market == "crypto":
        if not rows["bar"].eq("1d").all():
            raise ValueError("机制探针仅接受日线")
        rows["at"] = pd.to_datetime(rows.timestamp, utc=True) + pd.Timedelta(days=1)
    else:
        closes = {at.date().isoformat(): at for at in times}
        rows["at"] = rows.date.map(closes)
    if rows["at"].isna().any() or rows.duplicated(["at", "symbol"]).any():
        raise ValueError("探针行情不能因共同日期交集而静默丢失或重复")
    actions = {(a["symbol"], a["date"]): a for a in snapshot.actions or []}
    output = {}
    for symbol in snapshot.config["market"]["symbols"]:
        close = rows.loc[rows.symbol == symbol].set_index("at").close.astype(float).reindex(times)
        if close.isna().any() or not (close > 0).all():
            raise ValueError("探针不能前填或删除缺失行情")
        dividends, ratios = pd.Series(0., index=times), pd.Series(1., index=times)
        for at in times:
            action = actions.get((symbol, at.date().isoformat()))
            if action:
                if action["type"] == "dividend":
                    dividends.loc[at] = float(action["amount_per_share"])
                elif action["type"] == "split":
                    ratios.loc[at] = float(action["ratio"])
                else:
                    raise ValueError("不支持的公司行动")
        gross = (close * ratios + dividends) / close.shift(1)
        gross.iloc[0] = 1.
        tr = gross.cumprod()
        std = close.rolling(20).std(ddof=1)
        path = close.diff().abs().rolling(20).sum()
        panel = pd.DataFrame({"close": close, "return20": close.pct_change(20, fill_method=None),
            "return60": close.pct_change(60, fill_method=None), "sma120": close.rolling(120).mean(),
            "sma_gap": close.rolling(20).mean() / close.rolling(60).mean() - 1,
            "zscore20": (close-close.rolling(20).mean()) / std.where(std != 0),
            "efficiency20": (close-close.shift(20)).abs() / path.where(path != 0),
            "tr63": tr.pct_change(63, fill_method=None), "tr_index": tr,
            "tr_sma126": tr.rolling(126).mean()}, index=times)
        for at, values in panel.iterrows():
            output[(at.isoformat(), symbol)] = {k: float(v) if math.isfinite(v) else None
                                               for k, v in values.items()}
    return output


def valid_features(kind, values):
    return all(values[key] is not None and math.isfinite(values[key]) for key in FIELDS[kind])


def entry_targets(card, values, config, *, candidate):
    """返回原始请求仓位；整数股、风险预算、最多 5 仓仍由共用风控裁决。"""
    kind, symbols = card["mechanism"], card["symbols"]
    if set(values) != set(symbols):
        raise ValueError("不能缩减冻结标的池")
    if not all(valid_features(kind, v) for v in values.values()):
        return {}, False
    maximum = config["risk"]["max_weight"]
    if kind == "medium_term_trend":
        chosen = [s for s, v in values.items() if (v["return60"] > 0 and v["close"] > v["sma120"]
                                                  if candidate else v["return20"] > 0)]
    elif kind == "range_mean_reversion":
        chosen = [s for s, v in values.items() if v["zscore20"] <= -2 and
                  (not candidate or abs(v["sma_gap"]) <= .02 and v["efficiency20"] <= .25)]
    elif kind == "low_turnover_cross_asset_trend":
        chosen = [s for s, v in values.items() if (candidate or s in ("SPY", "QQQ", "IWM"))
                  and v["tr63"] > 0 and v["tr_index"] > v["tr_sma126"]]
    elif kind == "sector_relative_strength":
        chosen = sorted((s for s, v in values.items() if s != "SPY" and
                         v["tr63"] > 0 and v["tr63"] > values["SPY"]["tr63"]),
                        key=lambda s: (-values[s]["tr63"], s))[:2]
        if not candidate:
            return {s: len(chosen)*maximum/len(SECTORS) for s in SECTORS}, True
    else:
        raise ValueError("未登记的机制")
    weight = min(maximum, config["risk"]["max_gross_exposure"]/len(chosen)) if chosen else 0
    return {s: weight for s in chosen}, True


def origins(card, times, window, *, purge=20, embargo=20):
    """窗口开头 embargo 根不入场，期末 purge 根不建标签；不跨窗口持仓。"""
    selected = [at for at in times if window["start"] <= at.date().isoformat() <= window["end"]]
    if len(selected) != window["expected_bars"] or selected[0].isoformat() != window["first_close"] \
            or selected[-1].isoformat() != window["last_close"]:
        raise ValueError("窗口日历不完整")
    horizon = card["definition"]["horizon_sessions"]
    eligible = selected[embargo:len(selected)-max(purge, horizon)]
    warmup = {"medium_term_trend": 120, "range_mean_reversion": 60,
              "low_turnover_cross_asset_trend": 126, "sector_relative_strength": 64}[card["mechanism"]]
    history = {at: i+1 for i, at in enumerate(times)}
    eligible = [at for at in eligible if history[at] >= warmup]
    if card["market"] == "crypto":
        return eligible[::card["definition"]["origin_stride_sessions"]]
    # 对照完整交易日历确定月末，不能把截断的窗口末日误当作月末。
    month_ends = {a for a, b in zip(times, times[1:]) if (a.year, a.month) != (b.year, b.month)}
    return [at for at in eligible if at in month_ends]


class ProbeProvider:
    def __init__(self, card, panel, start, targets, *, features_valid=True):
        self.card, self.panel, self.start, self.targets = card, panel, start, targets
        self.features_valid = features_valid

    def targets_at(self, at, config):
        values = {s: self.panel[(at, s)] for s in self.card["symbols"]}
        return entry_targets(self.card, values, config, candidate=True)

    def decide(self, context, account, config):
        at = context.as_of.isoformat()
        weights = self.targets if at == self.start else account.payload(context)["current_weights"]
        allocations = []
        for symbol in sorted(context.features):
            weight = weights.get(symbol, 0)
            if at != self.start and weight:
                values = self.panel[(at, symbol)]
                if self.card["mechanism"] == "medium_term_trend":
                    weight = weight if values["return20"] is not None and values["return20"] > 0 else 0
                elif self.card["mechanism"] == "range_mean_reversion":
                    weight = weight if values["zscore20"] is not None and values["zscore20"] < 0 else 0
            allocations.append(Allocation(symbol, float(weight), 1., "冻结机制；单次入场和原保护规则"))
        return DecisionBatch(tuple(allocations), "mechanism_probe", "deterministic_no_model")


class DividendReplanProvider(ProbeProvider):
    """首次计划因普通分红整组取消后，仅在当日收盘重规划一次。"""

    def __init__(self, card, panel, start, targets, actions):
        super().__init__(card, panel, start, targets)
        if card["mechanism"] != "low_turnover_cross_asset_trend" or card["market"] != "stocks":
            raise ValueError("除息重规划仅用于股票跨资产研究")
        self.actions = {a["id"]: a for a in actions}
        self.steps = 0
        self.retry = None
        self.replans = []

    def observe_execution(self, context, orders):
        self.steps += 1
        # The first observation is the signal close, the second is the first fill day.
        if self.steps != 2 or not orders:
            return
        day = context.as_of.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        allowed = {"corporate_action_replan", "corporate_action_plan_replan"}
        if not all(o["status"] == "canceled" and o.get("reason") in allowed
                   and o.get("created_at") == self.start for o in orders):
            return
        direct = [o for o in orders if o["reason"] == "corporate_action_replan"]
        if not direct:
            return
        for order in orders:
            action = self.actions.get(order.get("action_id"))
            if (not action or action["type"] != "dividend" or action["date"] != day
                    or action.get("distribution") != "ordinary_cash"):
                return
        self.retry = {"as_of": context.as_of.isoformat(),
                      "canceled_orders": [o["client_order_id"] for o in orders],
                      "action_ids": sorted({o["action_id"] for o in orders})}

    def decide(self, context, account, config):
        at = context.as_of.isoformat()
        if self.retry is not None and self.retry["as_of"] == at and not self.replans:
            if not account.positions and not account.last_exit_bar:
                targets, valid = self.targets_at(at, config)
                self.replans.append({**self.retry, "targets": targets, "features_valid": valid})
                # Recompute the whole plan from this close; do not reuse stale limits.
                return DecisionBatch(tuple(Allocation(s, float(targets.get(s, 0)), 1.,
                    "普通分红整组撤单；当日收盘重规划一次") for s in sorted(context.features)),
                    "mechanism_probe", "deterministic_no_model")
        return super().decide(context, account, config)


def run_probe(snapshot, card, panel, at, config, spec, path, *, candidate, replan=False, provider=None):
    """固定原点账户；默认不重入，显式研究可重规划一次，末值扣清算估计费。"""
    start = snapshot.times.index(at)
    horizon = card["definition"]["horizon_sessions"]
    times = snapshot.times[start:start+horizon+1]
    if len(times) != horizon+1:
        raise ValueError("探针退出右删失")
    check_action_window(snapshot.actions, snapshot.times[0].date().isoformat(), times[-1].date().isoformat())
    if provider is None:
        values = {s: panel[(at.isoformat(), s)] for s in card["symbols"]}
        targets, valid = entry_targets(card, values, config, candidate=candidate)
        if replan and not candidate:
            raise ValueError("重规划实验必须使用完整跨资产目标，不能改变资产对照")
        provider = (DividendReplanProvider(card, panel, at.isoformat(), targets, snapshot.actions or [])
                    if replan else ProbeProvider(card, panel, at.isoformat(), targets))
        provider.features_valid = valid
    elif replan or provider.card != card or provider.start != at.isoformat():
        raise ValueError("自定义研究决策须匹配冻结卡片与原点，重规划由该决策负责")
    targets = provider.targets
    result = replay(snapshot, spec, config, provider, times=times, journal_path=path)
    audit = reconcile_replay(inspect_replay(path)["records"])
    if audit["status"] != "reconciled" or result["errors"]:
        raise ValueError("探针执行/独立账目核验失败")
    account = result["account"]
    replans = getattr(provider, "replans", [])
    valid = provider.features_valid and all(r["features_valid"] for r in replans)
    held = account["equity"]-account["cash"]-account["dividend_receivable"]
    sell_cost = max(0, held)*config["execution"]["cost_bps"]/10000
    fills = [o for o in result["orders"] if o["status"] == "filled"]
    return {"signal_at": at.isoformat(), "end_at": times[-1].isoformat(), "feature_valid": valid,
            "replans": replans,
            "requested_targets": targets, "net_return": (account["equity"]-sell_cost)/result["initial_equity"]-1,
            "buy_fills": sum(o["side"] == "buy" for o in fills), "fills": len(fills),
            "estimated_final_sell_cost": sell_cost, "remaining_positions": account["positions"],
            "costs": account["estimated_execution_cost"], "dividend_income": account["dividend_income"],
            "risk_hold": result["risk_hold"], "reconciliation": {k: audit[k] for k in
                ("status", "account_snapshots", "discrepancies")}, "journal": str(path),
            "cancellations": [o.get("reason") for o in result["orders"] if o["status"] == "canceled"],
            "exit_reasons": [o.get("reason", "target_reduction") for o in fills if o["side"] == "sell"]}
