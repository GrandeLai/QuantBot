import assert from "node:assert/strict";
import test from "node:test";

import {
  defaultCryptoBacktestStrategy,
  prioritizeCryptoStrategies,
  recommendedCryptoBacktestTimeframe,
} from "./cryptoBacktestConfig.ts";

test("prioritizeCryptoStrategies brings VWAP_EMA_Trend to the front", () => {
  const ordered = prioritizeCryptoStrategies([
    { id: "ma_crossover", name: "MA Crossover", description: "", default_params: {} },
    { id: "vwap_ema_trend", name: "VWAP + EMA Trend", description: "", default_params: {} },
    { id: "grid_trading", name: "Grid", description: "", default_params: {} },
  ]);

  assert.equal(ordered[0]?.id, "vwap_ema_trend");
  assert.equal(defaultCryptoBacktestStrategy(ordered), "vwap_ema_trend");
});

test("recommendedCryptoBacktestTimeframe prefers intraday validation for VWAP_EMA_Trend", () => {
  assert.equal(recommendedCryptoBacktestTimeframe("vwap_ema_trend"), "1h");
  assert.equal(recommendedCryptoBacktestTimeframe("ma_crossover"), "1d");
});
