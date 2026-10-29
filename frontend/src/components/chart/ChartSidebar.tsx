/**
 * 图表侧边工具栏 — 绘图工具 / 图表操作.
 *
 * 工具说明：
 *   select    — 选择模式（默认），正常十字光标
 *   trendline — 趋势线：点击两点，在图表上添加蓝色线段
 *   text      — 文字注释：点击后弹出输入框，确认后在对应 K 线上显示箭头+文字
 *   measure   — 测量：点击两点，弹出价差%和时间差
 *   search    — 聚焦标的搜索输入框
 *   lock      — 锁定/解锁图表滚动和缩放
 *   hide      — 隐藏/显示所有绘图元素
 *   clear     — 清除全部绘图
 *   settings  — 显示图表外观设置面板
 */
import { useState, useRef, useEffect } from "react";
import {
  MousePointer2,
  TrendingUp,
  Type,
  Ruler,
  Search,
  Lock,
  Unlock,
  EyeOff,
  Eye,
  Trash2,
  Settings2,
  X,
} from "lucide-react";
import { cn } from "../../lib/utils";
import { ColorType } from "lightweight-charts";
import {
  useChartDrawingStore,
  commitTextAnnotation,
  chartRefs,
  type DrawTool,
} from "../../store/chartDrawing";

// ── 侧边图标按钮 ───────────────────────────────────────────────────────────────

interface IconBtnProps {
  icon: React.ElementType;
  active?: boolean;
  title: string;
  warn?: boolean;
  onClick: () => void;
}

function IconBtn({ icon: Icon, active, title, warn, onClick }: IconBtnProps) {
  return (
    <button
      title={title}
      onClick={onClick}
      className={cn(
        "p-2 rounded-md transition-all duration-150 relative group",
        active && !warn && "bg-blue-600 text-white",
        active && warn && "bg-amber-600 text-white",
        !active && "text-gray-400 hover:bg-[#2a2e39] hover:text-white",
      )}
    >
      <Icon size={18} />
      {/* Tooltip */}
      <span className="pointer-events-none absolute left-full ml-2 top-1/2 -translate-y-1/2 whitespace-nowrap bg-[#1e222d] text-white text-[10px] px-2 py-1 rounded border border-gray-700 opacity-0 group-hover:opacity-100 transition-opacity z-50">
        {title}
      </span>
    </button>
  );
}

// ── 文字注释弹窗 ───────────────────────────────────────────────────────────────

function TextDialog() {
  const { textDialog, setTextDialog } = useChartDrawingStore();
  const [text, setText] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (textDialog) {
      setText("");
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [textDialog]);

  if (!textDialog) return null;

  const handleConfirm = () => {
    if (text.trim()) {
      commitTextAnnotation(textDialog.time, text);
    }
    setTextDialog(null);
  };

  const style: React.CSSProperties = {
    position: "fixed",
    left: Math.min(textDialog.pageX + 8, window.innerWidth - 240),
    top: Math.min(textDialog.pageY - 20, window.innerHeight - 80),
    zIndex: 9999,
  };

  return (
    <div
      style={style}
      className="bg-[#1e222d] border border-gray-600 rounded-lg shadow-2xl p-3 w-56"
    >
      <div className="flex items-center justify-between mb-2">
        <span className="text-[10px] text-gray-400 uppercase font-bold tracking-wider">文字注释</span>
        <button
          onClick={() => setTextDialog(null)}
          className="text-gray-500 hover:text-white transition-colors"
        >
          <X size={12} />
        </button>
      </div>
      <input
        ref={inputRef}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") handleConfirm();
          if (e.key === "Escape") setTextDialog(null);
        }}
        placeholder="输入注释文字…"
        className="w-full bg-[#131722] border border-gray-700 rounded px-2 py-1.5 text-white text-xs outline-none focus:border-blue-500 placeholder-gray-600"
      />
      <div className="flex justify-end gap-2 mt-2">
        <button
          onClick={() => setTextDialog(null)}
          className="text-[10px] text-gray-400 hover:text-white px-2 py-1 transition-colors"
        >
          取消
        </button>
        <button
          onClick={handleConfirm}
          className="text-[10px] bg-blue-600 hover:bg-blue-500 text-white px-3 py-1 rounded transition-colors"
        >
          确认
        </button>
      </div>
    </div>
  );
}

// ── 测量结果悬浮提示 ───────────────────────────────────────────────────────────

function MeasureOverlay() {
  const { measureResult, setMeasureResult } = useChartDrawingStore();
  if (!measureResult) return null;

  const style: React.CSSProperties = {
    position: "fixed",
    left: Math.min(measureResult.pageX + 8, window.innerWidth - 200),
    top: Math.min(measureResult.pageY - 20, window.innerHeight - 80),
    zIndex: 9999,
  };

  const positive = measureResult.priceDelta >= 0;

  return (
    <div
      style={style}
      className="bg-[#1e222d] border border-gray-600 rounded-lg shadow-2xl p-3 text-xs min-w-[160px]"
    >
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-[10px] text-gray-400 uppercase font-bold tracking-wider">测量结果</span>
        <button
          onClick={() => setMeasureResult(null)}
          className="text-gray-500 hover:text-white transition-colors"
        >
          <X size={12} />
        </button>
      </div>
      <div className="space-y-1">
        <div className="flex justify-between">
          <span className="text-gray-400">价差</span>
          <span className={cn("font-mono font-bold", positive ? "text-green-400" : "text-red-400")}>
            {positive ? "+" : ""}
            {measureResult.priceDelta.toFixed(2)}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-400">涨跌幅</span>
          <span className={cn("font-mono font-bold", positive ? "text-green-400" : "text-red-400")}>
            {positive ? "+" : ""}
            {measureResult.pricePct.toFixed(2)}%
          </span>
        </div>
      </div>
    </div>
  );
}

// ── 图表设置面板 ───────────────────────────────────────────────────────────────

function SettingsPanel({ onClose }: { onClose: () => void }) {
  const [bg, setBg] = useState("#0f172a");
  const [gridColor, setGridColor] = useState("#1e293b");

  const applySettings = () => {
    const api = chartRefs.api;
    if (!api) return;
    api.applyOptions({
      layout: { background: { type: ColorType.Solid, color: bg } },
      grid: {
        vertLines: { color: gridColor },
        horzLines: { color: gridColor },
      },
    });
    onClose();
  };

  return (
    <div className="absolute left-full ml-2 top-0 w-52 bg-[#1e222d] border border-gray-700 rounded-xl shadow-2xl p-4 z-50">
      <div className="flex items-center justify-between mb-3">
        <span className="text-[10px] text-gray-400 uppercase font-bold tracking-wider">图表设置</span>
        <button onClick={onClose} className="text-gray-500 hover:text-white transition-colors">
          <X size={12} />
        </button>
      </div>
      <div className="space-y-3">
        <div>
          <label className="text-[10px] text-gray-400 block mb-1">背景颜色</label>
          <div className="flex items-center gap-2">
            <input
              type="color"
              value={bg}
              onChange={(e) => setBg(e.target.value)}
              className="w-8 h-7 rounded cursor-pointer border-0 bg-transparent"
            />
            <span className="text-xs text-gray-400 font-mono">{bg}</span>
          </div>
        </div>
        <div>
          <label className="text-[10px] text-gray-400 block mb-1">网格颜色</label>
          <div className="flex items-center gap-2">
            <input
              type="color"
              value={gridColor}
              onChange={(e) => setGridColor(e.target.value)}
              className="w-8 h-7 rounded cursor-pointer border-0 bg-transparent"
            />
            <span className="text-xs text-gray-400 font-mono">{gridColor}</span>
          </div>
        </div>
        <button
          onClick={applySettings}
          className="w-full py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs rounded transition-colors"
        >
          应用
        </button>
        <button
          onClick={() => {
            chartRefs.api?.applyOptions({
              layout: { background: { type: ColorType.Solid, color: "#0f172a" } },
              grid: {
                vertLines: { color: "#1e293b" },
                horzLines: { color: "#1e293b" },
              },
            });
            setBg("#0f172a");
            setGridColor("#1e293b");
          }}
          className="w-full py-1.5 text-gray-400 hover:text-white text-xs transition-colors"
        >
          恢复默认
        </button>
      </div>
    </div>
  );
}

// ── 主组件 ────────────────────────────────────────────────────────────────────

export default function ChartSidebar() {
  const {
    activeTool,
    isLocked,
    isHidden,
    pendingPoint,
    setActiveTool,
    toggleLock,
    toggleHidden,
    triggerFocusSearch,
    clearAll,
  } = useChartDrawingStore();

  const [showSettings, setShowSettings] = useState(false);

  const selectTool = (tool: DrawTool) => {
    setActiveTool(activeTool === tool ? "select" : tool);
  };

  return (
    <aside className="w-12 border-r border-gray-800 bg-[#131722] flex flex-col items-center py-3 gap-1 relative">
      {/* ── 绘图模式工具 */}
      <IconBtn
        icon={MousePointer2}
        title="选择"
        active={activeTool === "select"}
        onClick={() => setActiveTool("select")}
      />
      <IconBtn
        icon={TrendingUp}
        title={pendingPoint && activeTool === "trendline" ? "点击第二个点" : "趋势线"}
        active={activeTool === "trendline"}
        onClick={() => selectTool("trendline")}
      />
      <IconBtn
        icon={Type}
        title="文字注释"
        active={activeTool === "text"}
        onClick={() => selectTool("text")}
      />
      <IconBtn
        icon={Ruler}
        title={pendingPoint && activeTool === "measure" ? "点击第二个点" : "测量"}
        active={activeTool === "measure"}
        onClick={() => selectTool("measure")}
      />
      <IconBtn
        icon={Search}
        title="搜索标的"
        active={false}
        onClick={triggerFocusSearch}
      />

      <div className="h-px w-8 bg-gray-800 my-1" />

      {/* ── 视图控制 */}
      <IconBtn
        icon={isLocked ? Lock : Unlock}
        title={isLocked ? "解锁图表" : "锁定图表"}
        active={isLocked}
        warn={isLocked}
        onClick={toggleLock}
      />
      <IconBtn
        icon={isHidden ? Eye : EyeOff}
        title={isHidden ? "显示绘图" : "隐藏绘图"}
        active={isHidden}
        warn={isHidden}
        onClick={toggleHidden}
      />
      <IconBtn
        icon={Trash2}
        title="清除全部绘图"
        active={false}
        onClick={clearAll}
      />

      {/* ── 设置（底部） */}
      <div className="mt-auto relative">
        <IconBtn
          icon={Settings2}
          title="图表设置"
          active={showSettings}
          onClick={() => setShowSettings((v) => !v)}
        />
        {showSettings && <SettingsPanel onClose={() => setShowSettings(false)} />}
      </div>

      {/* ── 待确认点提示 */}
      {pendingPoint && (activeTool === "trendline" || activeTool === "measure") && (
        <div className="absolute -right-1 top-0 w-1 h-full bg-blue-500/40 pointer-events-none" />
      )}

      {/* ── 全局 Overlay（fixed 定位，不受 sidebar 裁剪） */}
      <TextDialog />
      <MeasureOverlay />
    </aside>
  );
}
