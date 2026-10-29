import test from "node:test";
import assert from "node:assert/strict";
import { FEATURE_GUIDE_KEYS, featureGuides } from "./featureGuides.ts";

const EXPECTED_KEYS = [
  "market.chart",
  "market.sentiment",
  "market.live",
  "market.chart.main",
  "market.chart.watchlist.default",
  "market.chart.watchlist.custom",
  "market.chart.order",
  "market.chart.news",
  "market.chart.positions",
  "strategy.code",
  "strategy.optimize",
  "strategy.ml",
  "trading.execution",
  "trading.signals",
  "trading.execution.positions",
  "trading.execution.orders.today",
  "trading.execution.orders.history",
  "trading.execution.executions.today",
  "trading.execution.executions.history",
  "trading.execution.cashflows",
  "backtest.workspace",
  "options.payoff",
  "options.sensitivity",
  "options.scenario",
  "system.alerts",
  "screener.results",
  "screener.market",
  "screener.detail",
  "screener.analyze",
] as const;

test("feature guide registry covers all expected feature keys", () => {
  assert.deepEqual([...FEATURE_GUIDE_KEYS].sort(), [...EXPECTED_KEYS].sort());
});

test("every guide has usable step-by-step content", () => {
  for (const key of FEATURE_GUIDE_KEYS) {
    const guide = featureGuides[key];
    assert.ok(guide.title.trim().length > 0, `${key} should have a title`);
    assert.ok(guide.summary.trim().length > 0, `${key} should have a summary`);
    assert.ok(guide.goal.trim().length > 0, `${key} should have a goal`);
    assert.ok(guide.steps.length > 0, `${key} should have at least one step`);
    for (const step of guide.steps) {
      assert.ok(step.title.trim().length > 0, `${key} step should have a title`);
      assert.ok(step.description.trim().length > 0, `${key} step should have a description`);
    }
  }
});
