import assert from "node:assert/strict";
import test from "node:test";

import {
  buildCryptoResearchViewModel,
  type CryptoResearchSummaryLike,
} from "./cryptoResearchViewModel.ts";

test("buildCryptoResearchViewModel derives readable top-factor and window summaries", () => {
  const summary: CryptoResearchSummaryLike = {
    symbol: "BTC-USDT",
    feature_count: 12,
    rows: 180,
    validation_windows: 3,
    mean_accuracy: 0.56,
    mean_strategy_return: 0.034,
    latest_class_probabilities: { "-1": 0.15, "0": 0.25, "1": 0.60 },
    reversal_probability: 0.22,
    reversal_signal: "none",
    reversal_evidence: ["ema_20", "macd_hist", "adx_14"],
    feature_importance: {
      ema_20: 0.28,
      macd_hist: 0.22,
      adx_14: 0.18,
      plus_di_14: 0.11,
      atr_14: 0.09,
      volume_ratio: 0.05,
    },
    window_metrics: [
      { accuracy: 0.55, strategy_return: 0.03 },
      { accuracy: 0.58, strategy_return: 0.04 },
      { accuracy: 0.54, strategy_return: 0.02 },
    ],
  };

  const view = buildCryptoResearchViewModel(summary);

  assert.equal(view.directionLabel, "偏多");
  assert.equal(view.topFactors.length, 5);
  assert.deepEqual(view.topFactors[0], { name: "ema_20", valuePct: "28.0%" });
  assert.equal(view.windowRows[0].label, "窗口 1");
  assert.equal(view.windowRows[0].accuracyPct, "55.0%");
  assert.equal(view.windowRows[0].returnPct, "3.00%");
});
