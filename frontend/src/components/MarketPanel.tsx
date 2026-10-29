/**
 * 行情面板 — 合并：走势图 / 新闻情绪 / 实时推送
 */
import { useState } from "react";
import FeatureGuideButton from "@/components/guides/FeatureGuideButton";
import ChartLayout from "./ChartLayout";
import LiveDataPanel from "./LiveDataPanel";
import SentimentPanel from "./SentimentPanel";

type Sub = "chart" | "sentiment" | "live";

const SUBS: { key: Sub; label: string }[] = [
  { key: "chart", label: "走势图" },
  { key: "sentiment", label: "新闻情绪" },
  { key: "live", label: "实时推送" },
];

export default function MarketPanel() {
  const [sub, setSub] = useState<Sub>("chart");
  const guideKey = sub === "chart" ? "market.chart" : sub === "sentiment" ? "market.sentiment" : "market.live";

  return (
    <div className="relative" style={{ display: "flex", flexDirection: "column" }}>
      <div
        style={{
          display: "flex",
          gap: 4,
          borderBottom: "1px solid #1e293b",
          paddingBottom: 8,
          marginBottom: sub === "chart" ? 8 : 12,
        }}
      >
        {SUBS.map((s) => (
          <button
            key={s.key}
            onClick={() => setSub(s.key)}
            style={{
              padding: "4px 14px",
              fontSize: 12,
              borderRadius: 4,
              border: "none",
              cursor: "pointer",
              background: sub === s.key ? "#1e3a5f" : "transparent",
              color: sub === s.key ? "#60a5fa" : "#64748b",
              fontWeight: sub === s.key ? 600 : 400,
            }}
          >
            {s.label}
          </button>
        ))}
      </div>
      {sub === "chart" && <ChartLayout />}
      {sub === "sentiment" && <SentimentPanel />}
      {sub === "live" && <LiveDataPanel />}
      <FeatureGuideButton guideKey={guideKey} />
    </div>
  );
}
