import assert from "node:assert/strict";
import test from "node:test";

import {
  fetchCryptoResearchLatestSummary,
  fetchCryptoResearchLatestOptimization,
  fetchCryptoResearchOptimization,
  fetchCryptoResearchSummary,
} from "./client.ts";

test("fetchCryptoResearchSummary posts the expected research payload", async () => {
  let capturedUrl = "";
  let capturedInit: RequestInit | undefined;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    capturedUrl = String(input);
    capturedInit = init;
    return new Response(
      JSON.stringify({
        symbol: "BTC-USDT",
        base_timeframe: "15m",
        higher_timeframes: ["1h", "4h", "1d", "1w"],
        rows: 100,
        dataset_version: "demo",
        feature_count: 12,
        feature_columns: ["ema_20"],
        validation_windows: 2,
        window_metrics: [],
        mean_accuracy: 0.55,
        mean_strategy_return: 0.03,
        latest_class_signal: 1,
        latest_class_probabilities: { "-1": 0.2, "0": 0.3, "1": 0.5 },
        feature_importance: { ema_20: 0.4 },
        reversal_probability: 0.18,
        reversal_signal: "none",
        reversal_evidence: ["ema_20"],
      }),
      { status: 200 },
    );
  }) as typeof fetch;

  try {
    const result = await fetchCryptoResearchSummary("BTC-USDT");
    assert.equal(capturedUrl, "/api/crypto/research/train");
    assert.equal(capturedInit?.method, "POST");
    assert.ok(String(capturedInit?.body).includes('"symbol":"BTC-USDT"'));
    assert.equal(result.symbol, "BTC-USDT");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetchCryptoResearchLatestSummary reads the cached latest endpoint", async () => {
  let capturedUrl = "";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    capturedUrl = String(input);
    return new Response(
      JSON.stringify({
        symbol: "BTC-USDT",
        base_timeframe: "15m",
        higher_timeframes: ["1h", "4h", "1d", "1w"],
        rows: 100,
        dataset_version: "cached",
        feature_count: 12,
        feature_columns: ["ema_20"],
        validation_windows: 2,
        window_metrics: [],
        mean_accuracy: 0.55,
        mean_strategy_return: 0.03,
        latest_class_signal: 1,
        latest_class_probabilities: { "-1": 0.2, "0": 0.3, "1": 0.5 },
        feature_importance: { ema_20: 0.4 },
        reversal_probability: 0.18,
        reversal_signal: "none",
        reversal_evidence: ["ema_20"],
      }),
      { status: 200 },
    );
  }) as typeof fetch;

  try {
    const result = await fetchCryptoResearchLatestSummary("BTC-USDT");
    assert.equal(capturedUrl, "/api/crypto/research/latest?symbol=BTC-USDT&base_timeframe=15m");
    assert.equal(result.dataset_version, "cached");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetchCryptoResearchOptimization posts the expected optimization payload", async () => {
  let capturedUrl = "";
  let capturedInit: RequestInit | undefined;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    capturedUrl = String(input);
    capturedInit = init;
    return new Response(
      JSON.stringify({
        symbol: "BTC-USDT",
        strategy_id: "vwap_ema_trend",
        base_timeframe: "15m",
        higher_timeframes: ["1h", "4h", "1d", "1w"],
        best_params: { fast_period: 5 },
        window_count: 4,
        mean_accuracy: 0.0,
        mean_strategy_return: 0.12,
        max_drawdown: 0.08,
        window_metrics: [],
      }),
      { status: 200 },
    );
  }) as typeof fetch;

  try {
    const result = await fetchCryptoResearchOptimization("BTC-USDT");
    assert.equal(capturedUrl, "/api/crypto/research/optimize");
    assert.equal(capturedInit?.method, "POST");
    assert.ok(String(capturedInit?.body).includes('"strategy_id":"vwap_ema_trend"') === false);
    assert.ok(String(capturedInit?.body).includes('"symbol":"BTC-USDT"'));
    assert.equal(result.strategy_id, "vwap_ema_trend");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetchCryptoResearchLatestOptimization reads the cached optimization endpoint", async () => {
  let capturedUrl = "";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    capturedUrl = String(input);
    return new Response(
      JSON.stringify({
        symbol: "BTC-USDT",
        strategy_id: "vwap_ema_trend",
        best_params: { fast_period: 5 },
      }),
      { status: 200 },
    );
  }) as typeof fetch;

  try {
    const result = await fetchCryptoResearchLatestOptimization("BTC-USDT", "vwap_ema_trend");
    assert.equal(
      capturedUrl,
      "/api/crypto/research/optimize/latest?symbol=BTC-USDT&base_timeframe=15m&strategy_id=vwap_ema_trend",
    );
    assert.equal(result.strategy_id, "vwap_ema_trend");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
