import type { WorkbenchTab } from "./navigation.ts";

export const WORKBENCH_SECTIONS: Record<WorkbenchTab, string[]> = {
  research: ["market", "screener", "crypto"],
  strategy: ["strategy_workshop"],
  validation: ["validation_lab"],
  run: ["trading", "crypto_strategies"],
  risk_review: ["portfolio", "backtest"],
};

export const CRYPTO_WORKBENCH_SECTIONS: Record<
  "research" | "validation" | "run" | "risk_review",
  string[]
> = {
  research: ["market", "chart"],
  validation: ["backtest"],
  run: ["trading", "crypto_strategies"],
  risk_review: ["portfolio", "orders"],
};
