/**
 * WorkbenchShell — 工作台布局容器.
 *
 * 布局结构：
 *   ┌──────────┬───────────────────────────────┐
 *   │          │                               │
 *   │ Sidebar  │  Content (overflow-y-auto)    │
 *   │ (固定宽)  │  按侧栏选中项渲染单一子模块    │
 *   │          │                               │
 *   └──────────┴───────────────────────────────┘
 *
 * 侧栏可折叠：展开时 w-52（图标 + 文字），折叠时 w-12（仅图标）。
 * 移动端（< lg）：侧栏默认折叠，点击汉堡按钮展开为抽屉覆盖层。
 */

import { lazy, Suspense, useEffect, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";
import { WORKBENCH_TABS, type WorkbenchTab } from "@/workbench/navigation";
import { SECTION_DEFAULT, SIDEBAR_NAV } from "@/workbench/sidebarNav";

// ── 懒加载各子模块（只在首次访问时下载） ─────────────────────────────────────

const ResearchOverviewPanel = lazy(() => import("../ResearchOverviewPanel"));
const MarketPanel         = lazy(() => import("../MarketPanel"));
const ScreenerPanel       = lazy(() => import("../ScreenerPanel"));
const CryptoPanel         = lazy(() => import("../CryptoPanel"));
const StrategyWorkshop    = lazy(() => import("../StrategyWorkshop"));
const ValidationLab       = lazy(() => import("../ValidationLab"));
const CryptoMarketResearchPanel = lazy(() => import("../CryptoMarketResearchPanel"));
const UnifiedTradingPanel = lazy(() => import("../UnifiedTradingPanel"));
const CryptoRuntimePanel = lazy(() => import("../CryptoRuntimePanel"));
const PortfolioPanel      = lazy(() => import("../PortfolioPanel"));
const BacktestPanel       = lazy(() => import("../BacktestPanel"));

// ── 根据 tab + section 渲染对应子模块 ────────────────────────────────────────

function SectionContent({
  tab,
  section,
  onNavigate,
}: {
  tab: WorkbenchTab;
  section: string;
  onNavigate?: (tab: WorkbenchTab) => void;
}) {
  switch (`${tab}/${section}`) {
    // ── 研究中心 ────────────────────────────────────────────────────────────
    case "research/quant_research":
      return <ResearchOverviewPanel />;
    case "research/market":
      return <MarketPanel />;
    case "research/screener":
      return <ScreenerPanel />;
    case "research/chart":
      return <CryptoPanel allowedTabs={["chart"]} defaultTab="chart" />;

    // ── 策略库 ──────────────────────────────────────────────────────────────
    case "strategy/strategy_workshop":
      return (
        <StrategyWorkshop
          onNavigate={(t) => {
            if (t === "validation") onNavigate?.("validation");
          }}
        />
      );

    // ── 验证中心 ────────────────────────────────────────────────────────────
    case "validation/validation_lab":
      return <ValidationLab />;
    case "validation/crypto_research":
      return <CryptoMarketResearchPanel />;
    case "validation/backtest":
      return <BacktestPanel />;

    // ── 运行中心 ────────────────────────────────────────────────────────────
    case "run/trading":
      return <UnifiedTradingPanel />;
    case "run/crypto_strategies":
      return <CryptoRuntimePanel />;

    // ── 风险与复盘 ──────────────────────────────────────────────────────────
    case "risk_review/portfolio":
      return <PortfolioPanel />;
    case "risk_review/backtest":
      return <BacktestPanel />;
    case "risk_review/crypto_portfolio":
      return <CryptoPanel allowedTabs={["portfolio"]} defaultTab="portfolio" />;
    case "risk_review/crypto_orders":
      return <CryptoPanel allowedTabs={["orders"]} defaultTab="orders" />;

    default:
      return (
        <div className="flex items-center justify-center py-20 text-[#8b949e] text-sm">
          模块未找到：{tab}/{section}
        </div>
      );
  }
}

// ── WorkbenchShell ────────────────────────────────────────────────────────────

interface WorkbenchShellProps {
  activeTab: WorkbenchTab;
  onNavigate?: (tab: WorkbenchTab) => void;
}

export default function WorkbenchShell({ activeTab, onNavigate }: WorkbenchShellProps) {
  // 每个 Tab 各自记住上次选中的导航项，切换 Tab 时恢复
  const [activeSections, setActiveSections] = useState<Record<WorkbenchTab, string>>(
    () => ({ ...SECTION_DEFAULT }),
  );

  // 展开 / 折叠侧栏（桌面端持久；移动端默认折叠）
  const [expanded, setExpanded] = useState(true);
  // 移动端抽屉打开状态
  const [drawerOpen, setDrawerOpen] = useState(false);

  // Tab 切换时关闭移动端抽屉
  useEffect(() => {
    setDrawerOpen(false);
  }, [activeTab]);

  const navItems    = SIDEBAR_NAV[activeTab];
  const activeSection = activeSections[activeTab];
  const currentTab  = WORKBENCH_TABS.find((t) => t.key === activeTab);

  function selectSection(key: string) {
    setActiveSections((prev) => ({ ...prev, [activeTab]: key }));
    setDrawerOpen(false); // 移动端选择后关闭抽屉
  }

  // ── 侧栏内容（共用于桌面固定侧栏和移动端抽屉） ───────────────────────────

  function SidebarContent({ collapse }: { collapse: boolean }) {
    return (
      <>
        {/* Tab 标题行 */}
        <div
          className={cn(
            "flex items-center gap-2.5 px-3 py-3 border-b border-[#2A2D35] shrink-0",
            collapse && "justify-center px-0",
          )}
        >
          {currentTab && (
            <currentTab.icon
              size={16}
              className="text-[#8b949e] shrink-0"
            />
          )}
          {!collapse && (
            <span className="text-xs font-semibold text-white truncate">
              {currentTab?.label}
            </span>
          )}
        </div>

        {/* 导航列表 */}
        <nav className="flex-1 overflow-y-auto py-1">
          {navItems.map((item) => {
            const active = activeSection === item.key;
            return (
              <button
                key={item.key}
                onClick={() => selectSection(item.key)}
                title={collapse ? `${item.label} — ${item.description}` : undefined}
                className={cn(
                  "w-full text-left transition-colors",
                  collapse
                    ? "flex justify-center py-3 px-0"
                    : "flex items-start gap-2.5 px-3 py-2.5",
                  active
                    ? "bg-[#2A2D35] text-white"
                    : "text-[#8b949e] hover:text-white hover:bg-[#1a1d23]",
                )}
              >
                <item.icon size={15} className="shrink-0 mt-0.5" />
                {!collapse && (
                  <div className="min-w-0">
                    <div className="text-xs font-medium truncate">{item.label}</div>
                    <div className="text-[10px] text-[#434651] truncate mt-0.5">
                      {item.description}
                    </div>
                  </div>
                )}
              </button>
            );
          })}
        </nav>

        {/* 折叠/展开按钮（仅桌面端固定侧栏底部显示） */}
        {!drawerOpen && (
          <button
            onClick={() => setExpanded((v) => !v)}
            className={cn(
              "shrink-0 border-t border-[#2A2D35] py-2 text-[#434651] hover:text-[#8b949e] hover:bg-[#1a1d23] transition-colors",
              collapse ? "flex justify-center px-0" : "flex items-center gap-2 px-3",
            )}
          >
            {expanded ? (
              <>
                <ChevronLeft size={14} />
                {!collapse && <span className="text-[10px]">收起侧栏</span>}
              </>
            ) : (
              <>
                <ChevronRight size={14} />
                {!collapse && <span className="text-[10px]">展开侧栏</span>}
              </>
            )}
          </button>
        )}
      </>
    );
  }

  // ── 加载占位 ─────────────────────────────────────────────────────────────

  const loadingFallback = (
    <div className="flex min-h-[320px] items-center justify-center">
      <div className="rounded-2xl border border-[#2A2D35] bg-[#151619] px-5 py-4 text-sm text-[#8E9299]">
        加载中…
      </div>
    </div>
  );

  // ── 渲染 ─────────────────────────────────────────────────────────────────

  return (
    <div className="flex h-full relative">

      {/* ── 移动端遮罩 (< lg) ───────────────────────────────────────────── */}
      {drawerOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/50 lg:hidden"
          onClick={() => setDrawerOpen(false)}
        />
      )}

      {/* ── 移动端抽屉侧栏 (< lg) ──────────────────────────────────────── */}
      <aside
        className={cn(
          "fixed top-14 bottom-8 left-0 z-40 w-56 bg-[#0f1117] border-r border-[#2A2D35] flex flex-col transition-transform duration-200 lg:hidden",
          drawerOpen ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <SidebarContent collapse={false} />
      </aside>

      {/* ── 桌面端固定侧栏 (≥ lg) ─────────────────────────────────────── */}
      <aside
        className={cn(
          "hidden lg:flex flex-col shrink-0 bg-[#0f1117] border-r border-[#2A2D35] transition-all duration-200",
          expanded ? "w-52" : "w-12",
        )}
      >
        <SidebarContent collapse={!expanded} />
      </aside>

      {/* ── 内容区 ────────────────────────────────────────────────────── */}
      <div className="flex-1 min-w-0 overflow-y-auto py-4 px-5">
        {/* 移动端汉堡按钮（浮动在内容区左上角） */}
        <button
          onClick={() => setDrawerOpen((v) => !v)}
          className="lg:hidden mb-3 flex items-center gap-2 text-xs text-[#8b949e] hover:text-white transition-colors"
        >
          <span className="w-5 h-5 flex flex-col justify-center gap-1">
            <span className="block h-px bg-current rounded" />
            <span className="block h-px bg-current rounded" />
            <span className="block h-px bg-current rounded" />
          </span>
          <span>{currentTab?.label} / {navItems.find((i) => i.key === activeSection)?.label}</span>
        </button>

        <Suspense fallback={loadingFallback}>
          <SectionContent
            tab={activeTab}
            section={activeSection}
            onNavigate={onNavigate}
          />
        </Suspense>
      </div>
    </div>
  );
}
