"""显式合成输入，帮助用户理解持仓期权数据契约；不用于评估收益。"""

from datetime import timedelta

from quantbot.ai.feeds import stock_calendar


def example_input():
    calendar = stock_calendar("2025-01-02", "2025-04-01")
    days = [s.date() for s in calendar.sessions_in_range("2025-01-02", "2025-04-01")[:45]]
    prices = [round(100 + i * 1.7 if i <= 12 else 120.4 - (i - 12) * 1.15, 2) for i in range(len(days))]
    prices = [round(p - (1.2 if i >= 9 else 0), 2) for i, p in enumerate(prices)]
    payload = {"schema_version": 1, "source": "QuantBot explicit synthetic options example",
               "data_kind": "synthetic", "price_basis": "raw",
               "captured_at": (calendar.session_close(str(days[-1])) + timedelta(days=1)).isoformat(),
               "bars": [], "contracts": [], "quotes": [], "dividends": [], "actions": []}
    for i, (day, spot) in enumerate(zip(days, prices, strict=True)):
        payload["bars"].append({"date": str(day), "symbol": "DEMO", "open": spot,
            "high": round(spot + 1, 2), "low": round(spot - 1, 2), "close": spot, "volume": 1000000,
            "available_at": calendar.session_close(str(day)).isoformat()})
    for end in (12, 27, 44):
        for strike in (85, 90, 95, 100, 105, 110, 115, 120):
            for kind in ("call", "put"):
                cid = f"DEMO-{days[end]}-{kind}-{strike}"
                payload["contracts"].append({"id": cid, "symbol": "DEMO", "kind": kind, "strike": strike,
                    "expiry": str(days[end]), "last_trade_date": str(days[end]), "multiplier": 100,
                    "deliverable_shares": 100, "exercise_style": "american", "settlement": "physical", "currency": "USD",
                    "available_at": "2024-12-01T00:00:00+00:00"})
                for i, (day, spot) in enumerate(zip(days, prices, strict=True)):
                    if i > end:
                        break
                    intrinsic = max(0, spot - strike if kind == "call" else strike - spot)
                    for phase in ("open", "close"):
                        at = calendar.session_open(str(day)) if phase == "open" else calendar.session_close(str(day))
                        # 仅生成带标签的假报价，不把理论价格混入历史回测。
                        time_value = (days[end] - day).days * 0.045 + 0.15 if i < end or phase == "open" else 0
                        mid = intrinsic + time_value
                        payload["quotes"].append({"contract_id": cid, "date": str(day), "phase": phase,
                            "bid": round(max(0, mid - 0.02), 4), "ask": round(mid + 0.02, 4),
                            "bid_size": 100, "ask_size": 100, "open_interest": 200, "iv": 0.28,
                            "quote_at": at.isoformat(), "available_at": at.isoformat()})
    payload["dividends"] = [{"id": "DEMO-D1", "symbol": "DEMO", "ex_date": str(days[9]),
        "pay_date": str(days[18]), "amount": 1.2, "available_at": "2024-12-20T00:00:00+00:00"}]
    return {"data": payload, "portfolio": {"cash": 5000, "positions": {"DEMO": 100}},
            "policy": {"min_dte": 7, "target_dte": 21, "max_dte": 45, "roll_dte": 3}}
