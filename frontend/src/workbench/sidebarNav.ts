/**
 * 侧栏导航配置 — 每个 WorkbenchTab 对应的二级导航项列表.
 */

import {
  Activity,
  BarChart2,
  BookOpen,
  Brain,
  Briefcase,
  FlaskConical,
  List,
  PlayCircle,
  TrendingUp,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { WorkbenchTab } from "./navigation";

export interface SidebarNavItem {
  key: string;
  label: string;
  icon: LucideIcon;
  description: string;
}

/** 每个 Tab 的侧栏导航项（按展示顺序排列）. */
export const SIDEBAR_NAV: Record<WorkbenchTab, SidebarNavItem[]> = {
  research: [
    {
      key: "quant_research",
      label: "量化研究",
      icon: Brain,
      description: "价格行为 · 多因子 · 风险证据",
    },
    {
      key: "market",
      label: "行情总览",
      icon: Activity,
      description: "多市场实时行情",
    },
    {
      key: "screener",
      label: "因子筛选",
      icon: List,
      description: "资产筛选与评分",
    },
    {
      key: "chart",
      label: "K线图表",
      icon: BarChart2,
      description: "加密资产历史图表",
    },
  ],

  strategy: [
    {
      key: "strategy_workshop",
      label: "策略工坊",
      icon: BookOpen,
      description: "创建与编辑策略代码",
    },
  ],

  validation: [
    {
      key: "validation_lab",
      label: "验证实验室",
      icon: FlaskConical,
      description: "Walk-Forward 验证",
    },
    {
      key: "crypto_research",
      label: "市场机制研究",
      icon: Brain,
      description: "资金费率 · 规则研究",
    },
    {
      key: "backtest",
      label: "实盘前验证",
      icon: PlayCircle,
      description: "股票 / 加密通用回测",
    },
  ],

  run: [
    { key: "trading", label: "AI 量化交易", icon: Brain, description: "股票 / 加密现货 · 统一决策与风控" },
    { key: "crypto_strategies", label: "加密策略", icon: TrendingUp, description: "规则策略 · 本地 paper 验证" },
  ],

  risk_review: [
    {
      key: "portfolio",
      label: "持仓组合",
      icon: Briefcase,
      description: "持仓与净值曲线",
    },
    {
      key: "backtest",
      label: "历史回测",
      icon: PlayCircle,
      description: "策略历史表现",
    },
    {
      key: "crypto_portfolio",
      label: "加密持仓",
      icon: TrendingUp,
      description: "加密资产组合",
    },
    {
      key: "crypto_orders",
      label: "加密订单",
      icon: List,
      description: "历史委托记录",
    },
  ],
};

/** 每个 Tab 进入时默认选中的导航项. */
export const SECTION_DEFAULT: Record<WorkbenchTab, string> = {
  research: "quant_research",
  strategy: "strategy_workshop",
  validation: "validation_lab",
  run: "trading",
  risk_review: "portfolio",
};
