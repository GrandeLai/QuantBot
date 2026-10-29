import assert from "node:assert/strict";
import test from "node:test";

import { CRYPTO_WORKBENCH_SECTIONS, WORKBENCH_SECTIONS } from "./sections.ts";

test("topic-only tabs are removed from first-class workbench sections", () => {
  assert.deepEqual(Object.keys(WORKBENCH_SECTIONS), [
    "research",
    "strategy",
    "validation",
    "run",
    "risk_review",
  ]);

  assert.equal(WORKBENCH_SECTIONS.research.includes("options"), false);
  assert.equal(WORKBENCH_SECTIONS.research.includes("plugins"), false);
  assert.deepEqual(WORKBENCH_SECTIONS.risk_review, ["portfolio", "backtest"]);
  assert.deepEqual(WORKBENCH_SECTIONS.run, ["trading", "crypto_strategies"]);
  assert.deepEqual(CRYPTO_WORKBENCH_SECTIONS.research, ["market", "chart"]);
  assert.deepEqual(CRYPTO_WORKBENCH_SECTIONS.validation, ["backtest"]);
  assert.deepEqual(CRYPTO_WORKBENCH_SECTIONS.run, ["trading", "crypto_strategies"]);
  assert.deepEqual(CRYPTO_WORKBENCH_SECTIONS.risk_review, ["portfolio", "orders"]);
});
