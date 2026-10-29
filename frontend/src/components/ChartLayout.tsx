/**
 * 走势图完整布局 — 仿 TradeFlow Pro 专业交易终端.
 * 结构：[侧边栏] [主图区] [关注列表 / 下单面板] + [底部面板] + [状态栏]
 */
import ChartSidebar from "./chart/ChartSidebar";
import ChartMainArea from "./chart/ChartMainArea";
import ChartWatchlist from "./chart/ChartWatchlist";
import ChartOrderEntry from "./chart/ChartOrderEntry";
import ChartBottomPanels from "./chart/ChartBottomPanels";

/**
 * 高度计算：
 *   App header: 56px (h-14)
 *   App footer: 32px (h-8)
 *   main padding-top: 16px
 *   main padding-bottom: 16px
 *   MarketPanel sub-tab bar: ~46px
 *   Total offset: 56+32+16+16+46 = 166px
 */
export default function ChartLayout() {
  return (
    <div
      style={{
        height: "calc(100vh - 166px)",
        display: "flex",
        flexDirection: "column",
        background: "#131722",
        borderRadius: 8,
        overflow: "hidden",
        border: "1px solid #1f2937",
      }}
    >
      {/* ── 主内容区 ───────────────────────────────────── */}
      <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
        {/* 侧边工具栏 */}
        <ChartSidebar />

        {/* 图表 + 底部面板 */}
        <main style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
          {/* 主图区 + 右侧面板 */}
          <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
            <ChartMainArea />

            {/* 右侧：关注列表 + 下单面板 */}
            <aside
              style={{
                width: 300,
                borderLeft: "1px solid #1f2937",
                display: "flex",
                flexDirection: "column",
                minHeight: 0,
                background: "#131722",
              }}
            >
              <ChartWatchlist />
              <ChartOrderEntry />
            </aside>
          </div>

          {/* 底部面板 */}
          <ChartBottomPanels />
        </main>
      </div>

      {/* ── 状态栏 ────────────────────────────────────── */}
      <div
        style={{
          height: 24,
          background: "#1e222d",
          borderTop: "1px solid #1f2937",
          display: "flex",
          alignItems: "center",
          padding: "0 16px",
          justifyContent: "space-between",
          fontSize: 10,
          color: "#6b7280",
          flexShrink: 0,
        }}
      >
        <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
            <div
              style={{ width: 6, height: 6, borderRadius: "50%", background: "#22c55e" }}
            />
            <span>已连接</span>
          </div>
          <span>本地优先</span>
        </div>
        <div style={{ display: "flex", gap: 16 }}>
          <span>QuantBot v0.1</span>
        </div>
      </div>
    </div>
  );
}
