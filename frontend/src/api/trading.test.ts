import assert from "node:assert/strict";
import test from "node:test";

import {
  fetchOrderReport,
  fetchTradingRiskStatus,
  estimateTradingOrder,
  fetchOrderEvents,
  fetchTradingStatus,
  searchTradingSecurities,
  submitTradingOrder,
} from "./trading.ts";

test("fetchTradingStatus calls the unified trading status endpoint", async () => {
  let capturedUrl = "";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    capturedUrl = String(input);
    return new Response(
      JSON.stringify({
        provider: "mock",
        mode: "paper",
        configured: true,
        using_mock_fallback: false,
        capabilities: {
          supported_markets: ["US"],
          supported_asset_types: ["stock"],
          supported_order_types: ["market"],
          supports_us_short_selling: true,
          supports_otc: false,
          supports_us_prepost: false,
          supports_options: false,
          notes: [],
        },
      }),
      { status: 200 },
    );
  }) as typeof fetch;

  try {
    const result = await fetchTradingStatus();
    assert.equal(capturedUrl, "/api/trading/status");
    assert.equal(result.provider, "mock");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetchTradingStatus accepts futu provider status payloads", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => {
    return new Response(
      JSON.stringify({
        provider: "futu",
        mode: "paper",
        configured: false,
        using_mock_fallback: false,
        reason: "Futu provider unavailable",
        capabilities: {
          supported_markets: ["US", "HK"],
          supported_asset_types: ["stock", "etf"],
          supported_order_types: [],
          supports_us_short_selling: false,
          supports_otc: false,
          supports_us_prepost: false,
          supports_options: true,
          notes: [],
        },
      }),
      { status: 200 },
    );
  }) as typeof fetch;

  try {
    const result = await fetchTradingStatus();
    assert.equal(result.provider, "futu");
    assert.equal(result.configured, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("searchTradingSecurities uses the code-or-name search endpoint", async () => {
  let capturedUrl = "";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    capturedUrl = String(input);
    return new Response(JSON.stringify({ items: [{ symbol: "AAPL.US", name: "Apple Inc." }] }), { status: 200 });
  }) as typeof fetch;

  try {
    const result = await searchTradingSecurities("AAPL");
    assert.equal(capturedUrl, "/api/trading/securities/search?q=AAPL");
    assert.equal(result[0]?.symbol, "AAPL.US");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("estimateTradingOrder posts to the estimate endpoint", async () => {
  let capturedUrl = "";
  let capturedInit: RequestInit | undefined;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    capturedUrl = String(input);
    capturedInit = init;
    return new Response(
      JSON.stringify({
        symbol: "AAPL.US",
        side: "buy",
        order_type: "market",
        reference_price: 100,
        cash_max_qty: 10,
        sell_max_qty: 0,
      }),
      { status: 200 },
    );
  }) as typeof fetch;

  try {
    const result = await estimateTradingOrder({ symbol: "AAPL.US", side: "buy", order_type: "market" });
    assert.equal(capturedUrl, "/api/trading/orders/estimate");
    assert.equal(capturedInit?.method, "POST");
    assert.equal(result.cash_max_qty, 10);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("submitTradingOrder posts the unified order payload", async () => {
  let capturedUrl = "";
  let capturedInit: RequestInit | undefined;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    capturedUrl = String(input);
    capturedInit = init;
    return new Response(
      JSON.stringify({
        provider: "mock",
        order_id: "MOCK-000001",
        status: "filled",
        message: "accepted",
      }),
      { status: 200 },
    );
  }) as typeof fetch;

  try {
    const result = await submitTradingOrder({
      symbol: "AAPL.US",
      side: "buy",
      order_type: "market",
      quantity: 10,
    });
    assert.equal(capturedUrl, "/api/trading/orders");
    assert.equal(capturedInit?.method, "POST");
    assert.ok(String(capturedInit?.body).includes('"symbol":"AAPL.US"'));
    assert.equal(result.order_id, "MOCK-000001");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetchOrderEvents reads the OMS event timeline endpoint", async () => {
  let capturedUrl = "";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    capturedUrl = String(input);
    return new Response(
      JSON.stringify({
        items: [
          {
            event_id: "evt-000001",
            order_id: "MOCK-000001",
            event_type: "submitted",
            status: "submitted",
            message: "accepted",
            occurred_at: "2026-04-14T00:00:00+00:00",
          },
        ],
      }),
      { status: 200 },
    );
  }) as typeof fetch;

  try {
    const result = await fetchOrderEvents("MOCK-000001");
    assert.equal(capturedUrl, "/api/trading/orders/MOCK-000001/events");
    assert.equal(result[0]?.event_type, "submitted");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetchOrderReport reads the execution report endpoint", async () => {
  let capturedUrl = "";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    capturedUrl = String(input);
    return new Response(
      JSON.stringify({
        order_id: "MOCK-000001",
        status: "filled",
        submitted_quantity: 10,
        executed_quantity: 10,
        fill_ratio: 1,
        event_count: 2,
        lifecycle_seconds: 3.2,
        execution_count: 2,
        avg_execution_price: 192.84,
        first_execution_at: "2026-04-14T00:00:01+00:00",
        last_execution_at: "2026-04-14T00:00:03+00:00",
        execution_span_seconds: 2,
        price_delta: 0,
        slippage_bps: 0,
      }),
      { status: 200 },
    );
  }) as typeof fetch;

  try {
    const result = await fetchOrderReport("MOCK-000001");
    assert.equal(capturedUrl, "/api/trading/orders/MOCK-000001/report");
    assert.equal(result.fill_ratio, 1);
    assert.equal(result.execution_count, 2);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetchTradingRiskStatus reads the trading risk status endpoint", async () => {
  let capturedUrl = "";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    capturedUrl = String(input);
    return new Response(
      JSON.stringify({
        enabled: true,
        halted: false,
        max_position_count: 10,
        max_single_position_pct: 0.3,
        daily_loss_limit_pct: 0.05,
        max_order_value: 5000,
        current_today_pnl_pct: -0.01,
        open_position_count: 2,
        available_position_slots: 8,
        largest_position_symbol: "AAPL.US",
        largest_position_ratio: 0.18,
        warnings: [],
      }),
      { status: 200 },
    );
  }) as typeof fetch;

  try {
    const result = await fetchTradingRiskStatus();
    assert.equal(capturedUrl, "/api/trading/risk");
    assert.equal(result.max_order_value, 5000);
    assert.equal(result.largest_position_symbol, "AAPL.US");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
