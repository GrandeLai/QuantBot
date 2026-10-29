import test from "node:test";
import assert from "node:assert/strict";

import { WORKBENCH_TABS } from "./navigation.ts";

test("workbench labels match the workflow-first IA", () => {
  assert.deepEqual(
    WORKBENCH_TABS.map((tab) => tab.label),
    ["研究中心", "策略库", "验证中心", "运行中心", "风险与复盘"],
  );
});

test("workbench tabs follow the profit workflow order", () => {
  assert.deepEqual(
    WORKBENCH_TABS.map((tab) => tab.key),
    ["research", "strategy", "validation", "run", "risk_review"],
  );
});
