"""OKX 现货交易 Provider — 支持 demo sandbox 和实盘.

认证方式：HMAC-SHA256 + Base64，每个私有请求附加 4 个请求头。
配置：QUANTBOT_CRYPTO_OKX_API_KEY / QUANTBOT_CRYPTO_OKX_API_SECRET / QUANTBOT_CRYPTO_OKX_PASSPHRASE / QUANTBOT_CRYPTO_OKX_DEMO
Demo sandbox：与实盘相同 URL，区别在于请求头 x-simulated-trading: 1。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from functools import lru_cache as _lru_cache
from urllib.parse import urlencode

import httpx
from loguru import logger

from quantbot.crypto.broker.types import (
    CryptoAccountOverview,
    CryptoBalance,
    CryptoOrder,
    CryptoOrderRequest,
    CryptoPosition,
    OptionTicker,
    SwapOrderRequest,
    SwapTicker,
    TradingOrderSide,
    TradingOrderStatus,
    TradingOrderType,
    TradingProviderKind,
)
from quantbot.common.config import get_settings

OKX_BASE_URL = "https://www.okx.com"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _okx_timestamp() -> str:
    """OKX 要求 ISO 格式：2020-12-08T09:08:57.715Z"""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _map_status(raw: str) -> TradingOrderStatus:
    return {
        "live":             TradingOrderStatus.SUBMITTED,
        "partially_filled": TradingOrderStatus.PARTIAL_FILLED,
        "filled":           TradingOrderStatus.FILLED,
        "canceled":         TradingOrderStatus.CANCELED,
    }.get(raw.lower(), TradingOrderStatus.UNKNOWN)


def _parse_order(data: dict) -> CryptoOrder:
    """将 OKX 订单对象解析为 CryptoOrder 模型."""
    from quantbot.common.data.fetchers.okx_fetcher import display_symbol

    inst_id = data.get("instId", "")
    fill_sz = float(data.get("fillSz", 0) or 0)
    avg_px  = float(data.get("avgPx",  0) or 0)
    px_str  = data.get("px", "") or ""

    return CryptoOrder(
        order_id=str(data.get("ordId", "")),
        symbol=inst_id,
        display=display_symbol(inst_id),
        side=TradingOrderSide(data.get("side", "buy").lower()),
        order_type=(
            TradingOrderType.MARKET
            if data.get("ordType") == "market"
            else TradingOrderType.LIMIT
        ),
        status=_map_status(data.get("state", "")),
        quantity=float(data.get("sz", 0) or 0),
        executed_quantity=fill_sz,
        submitted_price=float(px_str) if px_str else None,
        executed_price=avg_px if avg_px > 0 else None,
        submitted_at=str(data.get("cTime", "")),
        updated_at=str(data.get("uTime", "")),
    )


class OKXTradingProvider:
    """OKX 现货交易 Provider（REST API）."""

    BASE_URL = OKX_BASE_URL

    def __init__(self) -> None:
        s = get_settings()
        self._api_key    = s.okx_api_key
        self._api_secret = s.okx_api_secret
        self._passphrase = s.okx_passphrase
        self._demo       = s.okx_demo
        self._configured = bool(self._api_key and self._api_secret and self._passphrase)

    @property
    def configured(self) -> bool:
        return self._configured

    @property
    def demo(self) -> bool:
        return self._demo

    # ── 签名 ──────────────────────────────────────────────────────────────────

    def _sign_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """生成 OKX 签名请求头.

        签名 = Base64(HMAC-SHA256(timestamp + METHOD + path + body, secret))
        GET 请求的 path 包含查询字符串；POST body 为 JSON 字符串。
        """
        ts = _okx_timestamp()
        message = ts + method.upper() + path + body
        sig = base64.b64encode(
            hmac.new(
                self._api_secret.encode(),
                message.encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        headers: dict[str, str] = {
            "OK-ACCESS-KEY":        self._api_key,
            "OK-ACCESS-SIGN":       sig,
            "OK-ACCESS-TIMESTAMP":  ts,
            "OK-ACCESS-PASSPHRASE": self._passphrase,
            "Content-Type":         "application/json",
        }
        if self._demo:
            headers["x-simulated-trading"] = "1"
        return headers

    def _public_headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self._demo:
            h["x-simulated-trading"] = "1"
        return h

    # ── HTTP 工具 ─────────────────────────────────────────────────────────────

    def _check(self, payload: dict) -> dict:
        """检查 OKX API code，非 0 则抛出（优先展示 sMsg 细节）."""
        if payload.get("code") != "0":
            # 单个操作失败时 data[0] 里有更详细的 sCode/sMsg
            detail = payload.get("msg", "")
            data = payload.get("data") or []
            if data and data[0].get("sCode", "0") != "0":
                detail = data[0].get("sMsg") or detail
            raise ValueError(f"OKX: {detail} (code={payload.get('code')})")
        return payload

    def _get(self, path: str, params: dict | None = None, *, signed: bool = False) -> dict:
        qs = ("?" + urlencode(params)) if params else ""
        full_path = path + qs
        headers = self._sign_headers("GET", full_path) if signed else self._public_headers()
        resp = httpx.get(f"{self.BASE_URL}{full_path}", headers=headers, timeout=10.0)
        resp.raise_for_status()
        return self._check(resp.json())

    def _post(self, path: str, body: dict) -> dict:
        body_str = json.dumps(body)
        headers = self._sign_headers("POST", path, body_str)
        resp = httpx.post(
            f"{self.BASE_URL}{path}",
            content=body_str,
            headers=headers,
            timeout=10.0,
        )
        resp.raise_for_status()
        return self._check(resp.json())

    # ── 公开接口（无需签名）───────────────────────────────────────────────────

    def get_price(self, symbol: str) -> float:
        """获取单个交易对最新价格."""
        from quantbot.common.data.fetchers.okx_fetcher import normalize_symbol
        try:
            data = self._get("/api/v5/market/ticker", {"instId": normalize_symbol(symbol)})
            return float(data["data"][0]["last"])
        except Exception:
            return 0.0

    def get_ticker_24h(self, symbols: list[str]) -> list[dict]:
        """批量获取 24h 行情 — 单次请求 /market/tickers?instType=SPOT 后过滤."""
        from quantbot.common.data.fetchers.okx_fetcher import normalize_symbol
        requested = {normalize_symbol(s) for s in symbols}
        try:
            data = self._get("/api/v5/market/tickers", {"instType": "SPOT"})
            return [t for t in data.get("data", []) if t.get("instId") in requested]
        except Exception as exc:
            logger.error(f"[OKX] bulk tickers failed: {exc}")
            return []

    # ── 私有接口（需要签名）───────────────────────────────────────────────────

    def get_account(self) -> CryptoAccountOverview:
        """获取账户余额总览."""
        if not self._configured:
            return CryptoAccountOverview(
                provider=TradingProviderKind.OKX,
                testnet=self._demo,
                balances=[],
                updated_at=_now_iso(),
            )
        data = self._get("/api/v5/account/balance", signed=True)
        details = data["data"][0].get("details", []) if data.get("data") else []
        balances = [
            CryptoBalance(
                asset=d["ccy"],
                free=float(d.get("availBal", 0) or 0),
                locked=float(d.get("frozenBal", 0) or 0),
                total=float(d.get("cashBal", 0) or 0),
            )
            for d in details
            if float(d.get("cashBal", 0) or 0) > 0
        ]
        return CryptoAccountOverview(
            provider=TradingProviderKind.OKX,
            testnet=self._demo,
            balances=balances,
            updated_at=_now_iso(),
        )

    def get_open_orders(self, symbol: str | None = None) -> list[CryptoOrder]:
        """获取当前挂单列表."""
        from quantbot.common.data.fetchers.okx_fetcher import normalize_symbol
        params: dict = {"instType": "SPOT"}
        if symbol:
            params["instId"] = normalize_symbol(symbol)
        data = self._get("/api/v5/trade/orders-pending", params, signed=True)
        return [_parse_order(o) for o in data.get("data", [])]

    def get_order_history(self, symbol: str, limit: int = 50) -> list[CryptoOrder]:
        """获取历史订单（最近 7 天）."""
        from quantbot.common.data.fetchers.okx_fetcher import normalize_symbol
        data = self._get(
            "/api/v5/trade/orders-history",
            {"instType": "SPOT", "instId": normalize_symbol(symbol), "limit": min(limit, 100)},
            signed=True,
        )
        return [_parse_order(o) for o in data.get("data", [])]

    def submit_order(self, req: CryptoOrderRequest) -> CryptoOrder:
        """提交新订单."""
        raise RuntimeError("QuantBot 统一交易入口未准入此旧券商写入路径；使用受风控的模拟执行入口")

    def cancel_order(self, symbol: str, order_id: str) -> CryptoOrder:
        """撤销指定订单（OKX 撤单也是 POST）."""
        raise RuntimeError("QuantBot 统一交易入口未准入此旧券商写入路径；使用受风控的模拟执行入口")

    # ── 永续合约 (SWAP) ───────────────────────────────────────────────────────

    def get_swap_tickers(self) -> list[SwapTicker]:
        """获取热门永续合约行情（含资金费率）."""
        from quantbot.common.data.fetchers.okx_fetcher import POPULAR_SWAPS, display_swap_symbol
        popular_set = set(POPULAR_SWAPS)
        try:
            data = self._get("/api/v5/market/tickers", {"instType": "SWAP"})
        except Exception as exc:
            logger.error(f"[OKX] swap tickers failed: {exc}")
            return []

        raw_map: dict[str, dict] = {
            d.get("instId", ""): d
            for d in data.get("data", [])
            if d.get("instId") in popular_set
        }

        tickers: list[SwapTicker] = []
        for inst_id in POPULAR_SWAPS:
            d = raw_map.get(inst_id)
            if not d:
                continue
            last  = float(d.get("last",    0) or 0)
            open_ = float(d.get("open24h", 0) or 0)
            change_pct = (last - open_) / open_ * 100 if open_ else 0.0

            # 资金费率（公开接口，不需签名）
            funding_rate = 0.0
            next_funding_time = ""
            try:
                fr_resp = self._get("/api/v5/public/funding-rate", {"instId": inst_id})
                fr = fr_resp.get("data", [{}])[0]
                funding_rate = float(fr.get("fundingRate", 0) or 0)
                next_funding_time = str(fr.get("nextFundingTime", "") or "")
            except Exception:
                pass

            tickers.append(SwapTicker(
                inst_id=inst_id,
                display=display_swap_symbol(inst_id),
                last=last,
                mark_px=last,
                change_pct=round(change_pct, 4),
                funding_rate=funding_rate,
                next_funding_time=next_funding_time,
                open_interest=float(d.get("openInterest", 0) or 0),
                volume_usdt=float(d.get("volCcy24h", 0) or 0),
                high_24h=float(d.get("high24h", 0) or 0),
                low_24h=float(d.get("low24h",  0) or 0),
            ))
        return tickers

    def get_positions(self, inst_type: str = "SWAP") -> list[CryptoPosition]:
        """获取合约/期权持仓."""
        data = self._get("/api/v5/account/positions", {"instType": inst_type}, signed=True)
        positions: list[CryptoPosition] = []
        for d in data.get("data", []):
            pos = float(d.get("pos", 0) or 0)
            if pos == 0:
                continue
            liq_raw = float(d.get("liqPx", 0) or 0)
            positions.append(CryptoPosition(
                pos_id=str(d.get("posId", "")),
                inst_id=str(d.get("instId", "")),
                inst_type=str(d.get("instType", inst_type)),
                pos_side=str(d.get("posSide", "net")),
                pos=pos,
                avg_px=float(d.get("avgPx",  0) or 0),
                mark_px=float(d.get("markPx", 0) or 0),
                upl=float(d.get("upl",       0) or 0),
                upl_ratio=float(d.get("uplRatio", 0) or 0),
                lever=str(d.get("lever", "1")),
                liq_px=liq_raw if liq_raw > 0 else None,
                margin_mode=str(d.get("mgnMode", "cross")),
                currency=str(d.get("ccy", "")),
                updated_at=str(d.get("uTime", "")),
            ))
        return positions

    def get_open_futures_orders(self, inst_type: str = "SWAP", inst_id: str | None = None) -> list[CryptoOrder]:
        """获取合约挂单."""
        params: dict = {"instType": inst_type}
        if inst_id:
            params["instId"] = inst_id
        data = self._get("/api/v5/trade/orders-pending", params, signed=True)
        return [_parse_order(o) for o in data.get("data", [])]

    def submit_swap_order(self, req: SwapOrderRequest) -> CryptoOrder:
        """提交永续合约订单."""
        raise RuntimeError("QuantBot 统一交易入口未准入此旧券商写入路径；使用受风控的模拟执行入口")

    # ── 期权 (OPTION) ─────────────────────────────────────────────────────────

    def get_options_expiries(self, uly: str) -> list[str]:
        """获取指定标的的期权到期日列表（YYYYMMDD）.

        OKX instruments 返回的 expTime 是毫秒时间戳，需转换为 YYYYMMDD。
        """
        try:
            data = self._get("/api/v5/public/instruments",
                             {"instType": "OPTION", "uly": uly})
            expiry_set: set[str] = set()
            for d in data.get("data", []):
                exp_ts_str = str(d.get("expTime", "") or "")
                if not exp_ts_str:
                    continue
                try:
                    exp_ms = int(exp_ts_str)
                    dt = datetime.fromtimestamp(exp_ms / 1000, tz=UTC)
                    expiry_set.add(dt.strftime("%Y%m%d"))
                except (ValueError, TypeError):
                    # 回退：从 instId 解析 YYMMDD → YYYYMMDD
                    inst_id = str(d.get("instId", ""))
                    parts = inst_id.split("-")
                    if len(parts) >= 5 and len(parts[-3]) == 6:
                        expiry_set.add("20" + parts[-3])
            return sorted(expiry_set)
        except Exception as exc:
            logger.error(f"[OKX] options expiries ({uly}) failed: {exc}")
            return []

    def get_options_chain(self, uly: str, exp_time: str | None = None) -> list[OptionTicker]:
        """获取期权链（价格 + 基本信息，从 tickers 接口提取）.

        uly: e.g. "BTC-USD"
        exp_time: "YYYYMMDD"（不传则返回近月到期日数据，避免数据量过大）

        OKX /api/v5/market/opt-summary 需要特定账户权限，此处改用
        /api/v5/market/tickers?instType=OPTION&uly=BTC-USD 作为数据源。
        """
        try:
            ticker_resp = self._get("/api/v5/market/tickers",
                                    {"instType": "OPTION", "uly": uly})
        except Exception as exc:
            logger.error(f"[OKX] options chain ({uly}) failed: {exc}")
            return []

        result: list[OptionTicker] = []
        for t in ticker_resp.get("data", []):
            inst_id = str(t.get("instId", ""))
            parts = inst_id.split("-")
            # 格式: BTC-USD-YYMMDD-STRIKE-C/P（5 段）
            if len(parts) < 5:
                continue
            try:
                strike_px = float(parts[-2])
                opt_type  = parts[-1]           # C 或 P
                raw_exp   = parts[-3]           # YYMMDD (6 位)
                exp_str   = ("20" + raw_exp) if len(raw_exp) == 6 else raw_exp
            except (ValueError, IndexError):
                continue

            # 如果指定了到期日，只返回匹配的
            if exp_time and exp_str != exp_time:
                continue

            result.append(OptionTicker(
                inst_id=inst_id,
                uly=uly,
                strike_px=strike_px,
                opt_type=opt_type,
                exp_time=exp_str,
                last=float(t.get("last",   0) or 0),
                bid_px=float(t.get("bidPx", 0) or 0),
                ask_px=float(t.get("askPx", 0) or 0),
                mark_vol=0.0,    # OKX opt-summary 需特定权限，暂不提供
                delta=0.0,
                gamma=0.0,
                theta=0.0,
                vega=0.0,
                open_interest=float(t.get("openInterest", 0) or 0),
            ))

        # 按 到期日 → 行权价 → 类型 排序
        result.sort(key=lambda x: (x.exp_time, x.strike_px, x.opt_type))
        return result


@_lru_cache(maxsize=1)
def get_okx_provider() -> OKXTradingProvider:
    """获取 OKX Provider 单例."""
    return OKXTradingProvider()
