/**
 * 策略工坊 — 策略全生命周期管理（代码编辑 → 参数优化 → 实盘前验证）
 */
import { useEffect, useRef, useState } from "react";
import type { editor as MonacoEditor } from "monaco-editor";
import {
  BarChart3,
  BrainCircuit,
  CheckCircle2,
  ChevronRight,
  Code2,
  Database,
  FileCode,
  Play,
  Plus,
  RefreshCw,
  Save,
  Settings,
  Terminal,
  Trash2,
  X,
} from "lucide-react";
import {
  listStrategies,
  listTemplates,
  getStrategy,
  getTemplateCode,
  createStrategy,
  updateStrategy,
  deleteStrategy,
} from "../api/client";
import FeatureGuideButton from "@/components/guides/FeatureGuideButton";
import { useStrategyStore } from "../store/strategyStore";
import { cn } from "../lib/utils";
import StrategyEditor from "./StrategyEditor";
import MLStrategyPanel from "./MLStrategyPanel";
import OptimizationPanel from "./OptimizationPanel";

// ── 子标签 ────────────────────────────────────────────────────────────────────

type Sub = "code" | "optimize" | "ml";

const SUBS: { key: Sub; label: string }[] = [
  { key: "code",     label: "代码编辑" },
  { key: "optimize", label: "参数优化" },
  { key: "ml",       label: "机器学习" },
];

// ── 侧栏条目 ──────────────────────────────────────────────────────────────────

interface SidebarItemProps {
  icon: React.ElementType;
  label: string;
  active?: boolean;
  indent?: number;
  hasChildren?: boolean;
  isOpen?: boolean;
  badge?: string;
  onClick?: () => void;
  children?: React.ReactNode;
}

function SidebarItem({
  icon: Icon,
  label,
  active,
  indent = 0,
  hasChildren,
  isOpen,
  badge,
  onClick,
  children,
}: SidebarItemProps) {
  return (
    <div>
      <div
        className={cn(
          "flex items-center gap-3 py-2 text-sm cursor-pointer transition-all hover:bg-[#161b22] group",
          active
            ? "bg-blue-600/10 text-blue-400 font-semibold border-r-2 border-blue-500"
            : "text-[#8b949e] hover:text-white",
        )}
        style={{ paddingLeft: indent > 0 ? `${indent * 16 + 24}px` : "24px", paddingRight: "16px" }}
        onClick={onClick}
      >
        <Icon
          className={cn(
            "w-4 h-4 shrink-0 transition-transform group-hover:scale-110",
            active ? "text-blue-400" : "text-[#8b949e] group-hover:text-white",
          )}
        />
        <span className="truncate flex-1">{label}</span>
        {badge && (
          <span className="text-[9px] font-bold px-1.5 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400 shrink-0">
            {badge}
          </span>
        )}
        {hasChildren && (
          <ChevronRight
            className={cn(
              "w-3.5 h-3.5 text-[#8b949e] transition-transform duration-200",
              isOpen && "rotate-90",
            )}
          />
        )}
      </div>
      {hasChildren && isOpen && children}
    </div>
  );
}

// ── 控制台 ────────────────────────────────────────────────────────────────────

interface LogLine { level: "INFO" | "SUCCESS" | "WARN" | "ERROR"; text: string; }
const LOG_COLOR: Record<string, string> = {
  INFO:    "text-blue-400",
  SUCCESS: "text-green-400",
  WARN:    "text-yellow-400",
  ERROR:   "text-red-400",
};

// ── 主组件 ────────────────────────────────────────────────────────────────────

interface Props {
  onNavigate?: (tab: "market" | "strategy" | "validation" | "options" | "screener" | "crypto") => void;
}

export default function StrategyWorkshop({ onNavigate }: Props) {
  const editorRef = useRef<MonacoEditor.IStandaloneCodeEditor | null>(null);
  const {
    strategies, templates, selectedId, editorCode, editorName,
    editorDescription, isDirty,
    setStrategies, setTemplates, selectStrategy,
    setEditorCode, setEditorName, setEditorDescription, markClean,
  } = useStrategyStore();

  const [saving, setSaving] = useState(false);
  const [isTemplate, setIsTemplate] = useState(false);
  const [sub, setSub] = useState<Sub>("code");
  const [showConsole, setShowConsole] = useState(true);
  const [logs, setLogs] = useState<LogLine[]>([
    { level: "INFO",    text: "Initializing QuantBot engine..." },
    { level: "INFO",    text: "Loading strategy definitions..." },
    { level: "SUCCESS", text: "Engine ready. Waiting for backtest command." },
  ]);
  const [openSections, setOpenSections] = useState<Record<string, boolean>>({
    tradingAlgos: true, mlModels: false, analysis: false,
  });
  const toggleSection = (key: string) => setOpenSections((p) => ({ ...p, [key]: !p[key] }));
  const pushLog = (level: LogLine["level"], text: string) =>
    setLogs((prev) => [...prev.slice(-50), { level, text }]);

  useEffect(() => {
    void (async () => {
      try {
        const [strats, tmplts] = await Promise.all([listStrategies(), listTemplates()]);
        setStrategies(strats);
        setTemplates(tmplts);
        pushLog("SUCCESS", `已加载 ${strats.length} 个策略，${tmplts.length} 个模板`);
      } catch {
        pushLog("WARN", "后端未启动，策略列表为空");
      }
    })();
  }, [setStrategies, setTemplates]);

  const handleSelectStrategy = async (id: string) => {
    setIsTemplate(false);
    selectStrategy(id);
    try {
      const rec = await getStrategy(id);
      setEditorCode(rec.code);
      setEditorName(rec.meta.name);
      setEditorDescription(rec.meta.description);
      markClean();
      setSub("code");
      pushLog("INFO", `已加载策略: ${rec.meta.name}`);
    } catch (e) {
      pushLog("ERROR", e instanceof Error ? e.message : "加载失败");
    }
  };

  const handleLoadTemplate = async (templateId: string) => {
    setIsTemplate(true);
    selectStrategy(null);
    try {
      const code = await getTemplateCode(templateId);
      const tmpl = templates.find((t) => t.id === templateId);
      setEditorCode(code);
      setEditorName(`${tmpl?.name ?? templateId} (副本)`);
      setEditorDescription(tmpl?.description ?? "");
      markClean();
      setSub("code");
      pushLog("INFO", `已加载模板: ${tmpl?.name ?? templateId}`);
    } catch (e) {
      pushLog("ERROR", e instanceof Error ? e.message : "加载失败");
    }
  };

  const handleNewStrategy = () => {
    setIsTemplate(false);
    selectStrategy(null);
    setEditorCode(
      `from quantpilot.strategy.base import BaseStrategy, StrategyContext\nfrom quantpilot.data.models import OHLCVBar\n\n\nclass MyStrategy(BaseStrategy):\n    name = "我的策略"\n    description = ""\n    default_params = {}\n\n    def on_bar(self, bar: OHLCVBar, context: StrategyContext) -> None:\n        pass\n`,
    );
    setEditorName("新策略");
    setEditorDescription("");
    markClean();
    setSub("code");
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      if (selectedId && !isTemplate) {
        await updateStrategy(selectedId, { name: editorName, description: editorDescription, code: editorCode });
        pushLog("SUCCESS", `已保存: ${editorName}`);
      } else {
        const result = await createStrategy(editorName, editorDescription, editorCode);
        const strats = await listStrategies();
        setStrategies(strats);
        selectStrategy(result.id);
        setIsTemplate(false);
        pushLog("SUCCESS", `已创建策略 (id: ${result.id})`);
      }
      markClean();
    } catch (e) {
      pushLog("ERROR", e instanceof Error ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!selectedId || !confirm(`确认删除策略 "${editorName}"？`)) return;
    try {
      await deleteStrategy(selectedId);
      const strats = await listStrategies();
      setStrategies(strats);
      handleNewStrategy();
      pushLog("INFO", `已删除策略: ${editorName}`);
    } catch (e) {
      pushLog("ERROR", e instanceof Error ? e.message : "删除失败");
    }
  };

  const handleRunBacktest = () => {
    pushLog("INFO", `跳转至回测引擎: ${editorName}`);
    onNavigate?.("validation");
  };

  const saveStateLabel = saving ? "Saving…" : isDirty ? "Unsaved changes" : "Saved";
  const saveStateClass = saving ? "text-blue-400" : isDirty ? "text-yellow-500" : "text-green-500";
  const disableSave = saving || (!isDirty && selectedId !== null && !isTemplate);
  const guideKey =
    sub === "code"
      ? "strategy.code"
      : sub === "optimize"
        ? "strategy.optimize"
        : "strategy.ml";

  return (
    <div
      className="relative flex bg-[#0d1117] rounded-xl overflow-hidden border border-[#30363d]"
      style={{ height: "calc(100vh - 120px)" }}
    >
      {/* ── 左侧栏 */}
      <div className="w-64 shrink-0 border-r border-[#30363d] bg-[#0d1117] flex flex-col">
        <div className="flex-1 overflow-y-auto custom-scrollbar py-6">

          <div className="px-6 py-2 flex items-center justify-between text-[10px] font-bold text-[#8b949e] uppercase tracking-[0.2em]">
            <span>我的策略</span>
            <Plus className="w-3.5 h-3.5 cursor-pointer hover:text-white transition-colors" onClick={handleNewStrategy} />
          </div>

          {strategies.length === 0 ? (
            <p className="px-6 py-2 text-xs text-[#8b949e]/50">暂无策略</p>
          ) : (
            strategies.map((s) => (
              <SidebarItem
                key={s.id}
                icon={FileCode}
                label={s.name}
                active={selectedId === s.id && !isTemplate}
                onClick={() => void handleSelectStrategy(s.id)}
              />
            ))
          )}

          <div className="mt-8">
            <div className="px-6 py-2 text-[10px] font-bold text-[#8b949e] uppercase tracking-[0.2em]">
              Templates
            </div>
            <SidebarItem
              icon={Code2}
              label="Trading Algorithms"
              hasChildren
              isOpen={openSections.tradingAlgos}
              onClick={() => toggleSection("tradingAlgos")}
            >
              {templates.map((t) => (
                <SidebarItem
                  key={t.id}
                  icon={BarChart3}
                  label={t.name}
                  indent={1}
                  active={isTemplate && editorName.startsWith(t.name)}
                  onClick={() => void handleLoadTemplate(t.id)}
                />
              ))}
              {templates.length === 0 && (
                <p className="px-10 py-1.5 text-xs text-[#8b949e]/40">暂无模板</p>
              )}
            </SidebarItem>
            <SidebarItem
              icon={BrainCircuit}
              label="Machine Learning"
              hasChildren
              isOpen={openSections.mlModels}
              onClick={() => toggleSection("mlModels")}
            >
              <SidebarItem icon={BarChart3} label="LSTM Predictor" indent={1} onClick={() => setSub("ml")} />
              <SidebarItem icon={BarChart3} label="XGBoost Classifier" indent={1} onClick={() => setSub("ml")} />
            </SidebarItem>
            <SidebarItem
              icon={Database}
              label="Trading Analyse"
              hasChildren
              isOpen={openSections.analysis}
              onClick={() => toggleSection("analysis")}
            >
              <SidebarItem icon={BarChart3} label="参数优化" indent={1} onClick={() => setSub("optimize")} />
            </SidebarItem>
          </div>
        </div>

        <div className="p-4 border-t border-[#30363d] flex flex-col gap-1">
          <button
            onClick={() => setSub("optimize")}
            className="flex items-center gap-3 h-9 px-3 rounded-md text-sm text-[#8b949e] hover:text-white hover:bg-[#161b22] transition-colors"
          >
            <Settings className="w-4 h-4" /> Settings
          </button>
          <button
            onClick={() => setShowConsole((v) => !v)}
            className={cn(
              "flex items-center gap-3 h-9 px-3 rounded-md text-sm transition-colors",
              showConsole ? "text-white bg-[#161b22]" : "text-[#8b949e] hover:text-white hover:bg-[#161b22]",
            )}
          >
            <Terminal className="w-4 h-4" /> Debug Console
          </button>
        </div>
      </div>

      {/* ── 右侧主区 */}
      <div className="flex-1 flex flex-col min-w-0 bg-[#0d1117]">

        {/* 策略头部 */}
        <div className="px-8 pt-6 pb-0 space-y-4 shrink-0">
          <div className="flex items-end justify-between gap-4">
            <div>
              <div className="flex items-center gap-2 text-[11px] font-bold text-[#8b949e] uppercase tracking-[0.2em] mb-1">
                <Code2 className="w-3 h-3" /> Strategy Workshop
              </div>
              <h1 className="text-3xl font-bold text-white tracking-tight">
                {editorName || "My Strategy"}
              </h1>
            </div>
            <div className="flex items-center gap-3 pb-1">
              <button
                onClick={handleRunBacktest}
                className="h-10 px-5 bg-white text-black rounded-lg font-bold hover:bg-[#e1e4e8] transition-colors flex items-center gap-2 text-sm whitespace-nowrap"
              >
                <Play className="w-4 h-4 fill-current" /> Run Backtest
              </button>
            </div>
          </div>

          {/* 策略信息卡 */}
          <div className="flex items-center gap-5 px-4 py-3 bg-[#161b22]/40 border border-[#30363d] rounded-xl">
            <div className="flex-none w-56">
              <label className="text-[10px] uppercase font-bold text-[#8b949e] mb-1.5 block tracking-wider">
                Strategy Name
              </label>
              <input
                value={editorName}
                onChange={(e) => setEditorName(e.target.value)}
                className="h-9 w-full bg-[#0d1117] border border-[#30363d] rounded-lg px-3 text-sm text-white outline-none focus:border-blue-500/50 transition-colors"
              />
            </div>
            <div className="flex-1">
              <label className="text-[10px] uppercase font-bold text-[#8b949e] mb-1.5 block tracking-wider">
                Description
              </label>
              <input
                value={editorDescription}
                onChange={(e) => setEditorDescription(e.target.value)}
                placeholder="Strategy description here (optional)"
                className="h-9 w-full bg-[#0d1117] border border-[#30363d] rounded-lg px-3 text-sm text-[#8b949e] placeholder-[#8b949e]/40 outline-none focus:border-blue-500/50 transition-colors"
              />
            </div>
            <div className="flex items-end gap-3 pb-0.5 shrink-0">
              <div className={cn("flex items-center gap-1.5 text-[11px] font-medium whitespace-nowrap", saveStateClass)}>
                <CheckCircle2 className="w-3.5 h-3.5" /> {saveStateLabel}
              </div>
              <button
                onClick={() => void handleSave()}
                disabled={disableSave}
                className="flex items-center gap-1.5 h-8 px-3 text-[#8b949e] hover:text-white text-sm rounded-md hover:bg-[#21262d] transition-colors disabled:opacity-50"
              >
                <Save className="w-3.5 h-3.5" /> {saving ? "Saving…" : isDirty ? "Save Now" : "Saved"}
              </button>
              {selectedId && !isTemplate && (
                <button
                  onClick={() => void handleDelete()}
                  className="flex items-center gap-1.5 h-8 px-3 text-red-400 hover:text-red-300 text-sm rounded-md hover:bg-red-500/10 transition-colors"
                >
                  <Trash2 className="w-3.5 h-3.5" /> Delete
                </button>
              )}
            </div>
          </div>

          {/* 子标签栏 */}
          <div className="flex gap-0 border-b border-[#30363d]">
            {SUBS.map((s) => (
              <button
                key={s.key}
                onClick={() => setSub(s.key)}
                className={cn(
                  "px-5 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px",
                  sub === s.key
                    ? "border-white text-white"
                    : "border-transparent text-[#8b949e] hover:text-white",
                )}
              >
                {s.label}
              </button>
            ))}
          </div>
        </div>

        {/* ── 内容区 */}
        {sub === "code" ? (
          <div className="flex-1 flex flex-col min-h-0 px-8 py-5 gap-4">
            <div className="flex-1 min-h-0 border border-[#30363d] bg-[#161b22]/40 rounded-xl overflow-hidden flex flex-col shadow-2xl">
              <div className="flex items-center justify-between px-4 h-10 border-b border-[#30363d] bg-[#161b22]/60 shrink-0">
                <div className="flex items-center gap-2 text-xs font-bold text-[#8b949e] uppercase tracking-widest">
                  <FileCode className="w-3.5 h-3.5" /> main.py
                </div>
                <div className="flex items-center gap-4 text-[10px] text-[#8b949e]/60 font-mono uppercase tracking-wider">
                  <div className="flex items-center gap-1.5">
                    <div className="w-1.5 h-1.5 rounded-full bg-blue-500/50" /> Python 3.10
                  </div>
                  <div className="h-3 w-px bg-[#30363d]" />
                  <span>UTF-8</span>
                </div>
              </div>
              <div className="flex-1 min-h-0">
                <StrategyEditor
                  value={editorCode}
                  onChange={setEditorCode}
                  height="100%"
                  onMount={(editor) => { editorRef.current = editor; }}
                />
              </div>
            </div>

            {showConsole && (
              <div className="h-44 border border-[#30363d] bg-[#161b22]/40 rounded-xl flex flex-col overflow-hidden shrink-0">
                <div className="flex items-center justify-between px-4 h-10 border-b border-[#30363d] bg-[#161b22]/60 shrink-0">
                  <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-widest text-[#8b949e]">
                    <Terminal className="w-3.5 h-3.5" /> Debug Console
                  </div>
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => setLogs([
                        { level: "INFO",    text: "Console cleared." },
                        { level: "SUCCESS", text: "Engine ready." },
                      ])}
                      className="h-7 w-7 flex items-center justify-center text-[#8b949e] hover:text-white rounded transition-colors"
                      title="Clear"
                    >
                      <RefreshCw className="w-3.5 h-3.5" />
                    </button>
                    <button
                      onClick={() => setShowConsole(false)}
                      className="h-7 w-7 flex items-center justify-center text-[#8b949e] hover:text-white rounded transition-colors"
                      title="Close"
                    >
                      <X className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
                <div className="flex-1 overflow-y-auto custom-scrollbar p-4 font-mono text-xs text-[#8b949e] space-y-1">
                  {logs.map((log, i) => (
                    <div key={i} className="flex gap-2">
                      <span className={cn("font-bold shrink-0", LOG_COLOR[log.level])}>[{log.level}]</span>
                      <span>{log.text}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        ) : (
          <div className="flex-1 overflow-y-auto custom-scrollbar px-8 py-5">
            {sub === "optimize" && <OptimizationPanel />}
            {sub === "ml"       && <MLStrategyPanel />}
          </div>
        )}
      </div>
      <FeatureGuideButton guideKey={guideKey} className="bottom-5 right-5" />
    </div>
  );
}
