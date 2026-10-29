/**
 * 测试验证工坊 — 实盘前回测验证 / 交易信号.
 */
import { useState } from "react";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import FeatureGuideButton from "@/components/guides/FeatureGuideButton";
import BacktestPanel from "./BacktestPanel";
import SignalsPanel from "./SignalsPanel";

type Sub = "backtest" | "signals";

// ── 主组件 ────────────────────────────────────────────────────────────────────

export default function ValidationLab() {
  const [sub, setSub] = useState<Sub>("backtest");

  return (
    <div className="relative space-y-4">
      {/* Sub-tab navigation */}
      <div className="flex justify-center">
        <Tabs value={sub} onValueChange={(v) => setSub(v as Sub)} className="w-fit">
          <TabsList className="bg-card border border-border h-10 p-1">
            <TabsTrigger value="backtest" className="px-8">实盘前验证</TabsTrigger>
            <TabsTrigger value="signals" className="px-8">交易信号</TabsTrigger>
          </TabsList>
        </Tabs>
      </div>

      {/* Content panels */}
      {sub === "backtest" && <BacktestPanel />}
      {sub === "signals" && <SignalsPanel />}

      {sub === "signals" && <FeatureGuideButton guideKey="trading.signals" />}
    </div>
  );
}
