/**
 * 策略编辑器状态管理 — Zustand store.
 */
import { create } from "zustand";
import type { StrategyMeta, TemplateSummary } from "../api/client";

interface StrategyState {
  strategies: StrategyMeta[];
  templates: TemplateSummary[];
  /** 当前编辑中的策略 ID（null = 新建） */
  selectedId: string | null;
  editorCode: string;
  editorName: string;
  editorDescription: string;
  isDirty: boolean;

  setStrategies: (s: StrategyMeta[]) => void;
  setTemplates: (t: TemplateSummary[]) => void;
  selectStrategy: (id: string | null) => void;
  setEditorCode: (code: string) => void;
  setEditorName: (name: string) => void;
  setEditorDescription: (desc: string) => void;
  markClean: () => void;
}

const DEFAULT_CODE = `"""
在此编写您的策略代码.

可继承 BaseStrategy 并实现 on_bar 方法：
"""
from quantpilot.strategy.base import BaseStrategy, StrategyContext
from quantpilot.data.models import OHLCVBar


class MyStrategy(BaseStrategy):
    name = "我的策略"
    description = "策略描述"
    version = "1.0.0"
    default_params = {"period": 20}

    def on_bar(self, bar: OHLCVBar, context: StrategyContext) -> None:
        # 在此处理每根 K 线
        pass
`;

export const useStrategyStore = create<StrategyState>((set) => ({
  strategies: [],
  templates: [],
  selectedId: null,
  editorCode: DEFAULT_CODE,
  editorName: "新策略",
  editorDescription: "",
  isDirty: false,

  setStrategies: (strategies) => set({ strategies }),
  setTemplates: (templates) => set({ templates }),
  selectStrategy: (selectedId) => set({ selectedId }),
  setEditorCode: (editorCode) => set({ editorCode, isDirty: true }),
  setEditorName: (editorName) => set({ editorName, isDirty: true }),
  setEditorDescription: (editorDescription) => set({ editorDescription, isDirty: true }),
  markClean: () => set({ isDirty: false }),
}));
