/**
 * QuantBot 主应用 — 标签页导航.
 */
import {
  Suspense,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  LineChart,
  Bell,
  Search,
  Zap,
  X,
  AlertTriangle,
  Info,
} from "lucide-react";
import { cn } from "./lib/utils";
import { useStrategyStore } from "./store/strategyStore";
import WorkbenchShell from "./components/workbench/WorkbenchShell";
import {
  WORKBENCH_TABS,
  type WorkbenchTab,
} from "./workbench/navigation";

// ── 告警事件类型 ──────────────────────────────────────────────────────────────

interface AlertEvent {
  id: string;
  rule_id: string;
  rule_name: string;
  triggered_at: string;
  message: string;
  severity?: string;
}

// ── 全局搜索弹窗 ──────────────────────────────────────────────────────────────

function GlobalSearch({
  open,
  onClose,
  onNavigate,
}: {
  open: boolean;
  onClose: () => void;
  onNavigate: (tab: WorkbenchTab) => void;
}) {
  const { strategies } = useStrategyStore();
  const [q, setQ] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setQ("");
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [open]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  if (!open) return null;

  const results = q.trim()
    ? strategies.filter(
        (s) =>
          s.name.toLowerCase().includes(q.toLowerCase()) ||
          (s.description ?? "").toLowerCase().includes(q.toLowerCase()),
      )
    : strategies.slice(0, 8);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-24 bg-black/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg bg-[#161b22] border border-[#30363d] rounded-2xl shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 搜索输入 */}
        <div className="flex items-center gap-3 px-4 py-3 border-b border-[#30363d]">
          <Search size={16} className="text-[#8b949e] shrink-0" />
          <input
            ref={inputRef}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="搜索策略..."
            className="flex-1 bg-transparent text-white placeholder-[#8b949e] text-sm outline-none"
          />
          <button onClick={onClose} className="text-[#8b949e] hover:text-white">
            <X size={14} />
          </button>
        </div>

        {/* 搜索结果 */}
        <div className="max-h-80 overflow-y-auto py-2">
          {results.length === 0 ? (
            <div className="px-4 py-6 text-center text-sm text-[#8b949e]">
              {q ? `无匹配策略 "${q}"` : "暂无策略"}
            </div>
          ) : (
            results.map((s) => (
              <button
                key={s.id}
                onClick={() => {
                  onNavigate("strategy");
                  onClose();
                }}
                className="w-full flex items-center gap-3 px-4 py-3 hover:bg-[#21262d] transition-colors text-left"
              >
                <LineChart size={14} className="text-blue-400 shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium text-white truncate">{s.name}</div>
                  <div className="text-[10px] text-[#8b949e] truncate">{s.description || "无描述"}</div>
                </div>
                <span className="text-[10px] text-[#434651] shrink-0">策略</span>
              </button>
            ))
          )}
        </div>

        {/* 快捷键提示 */}
        <div className="px-4 py-2 border-t border-[#30363d] flex items-center gap-4 text-[10px] text-[#434651]">
          <span><kbd className="bg-[#21262d] px-1 rounded">↑↓</kbd> 导航</span>
          <span><kbd className="bg-[#21262d] px-1 rounded">Enter</kbd> 跳转</span>
          <span><kbd className="bg-[#21262d] px-1 rounded">Esc</kbd> 关闭</span>
        </div>
      </div>
    </div>
  );
}

// ── 通知下拉 ──────────────────────────────────────────────────────────────────

function NotificationDropdown({
  events,
  loading,
  onClose,
}: {
  events: AlertEvent[];
  loading: boolean;
  onClose: () => void;
}) {
  const dropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        onClose();
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [onClose]);

  return (
    <div
      ref={dropdownRef}
      className="absolute right-0 top-full mt-2 w-80 bg-[#161b22] border border-[#30363d] rounded-xl shadow-2xl z-50 overflow-hidden"
    >
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#30363d]">
        <span className="text-sm font-semibold text-white">通知</span>
        <button onClick={onClose} className="text-[#8b949e] hover:text-white">
          <X size={14} />
        </button>
      </div>

      <div className="max-h-72 overflow-y-auto">
        {loading ? (
          <div className="px-4 py-6 text-center text-sm text-[#8b949e]">加载中…</div>
        ) : events.length === 0 ? (
          <div className="px-4 py-8 text-center">
            <Bell size={24} className="mx-auto text-[#434651] mb-2" />
            <p className="text-sm text-[#8b949e]">暂无告警通知</p>
            <p className="text-[10px] text-[#434651] mt-1">暂无未读通知</p>
          </div>
        ) : (
          events.map((ev) => (
            <div
              key={ev.id}
              className="flex items-start gap-3 px-4 py-3 border-b border-[#21262d] hover:bg-[#21262d] transition-colors"
            >
              <AlertTriangle size={14} className="text-yellow-400 shrink-0 mt-0.5" />
              <div className="flex-1 min-w-0">
                <div className="text-xs font-medium text-white truncate">{ev.rule_name}</div>
                <div className="text-[10px] text-[#8b949e] mt-0.5 line-clamp-2">{ev.message}</div>
                <div className="text-[9px] text-[#434651] mt-1">
                  {new Date(ev.triggered_at).toLocaleString("zh-CN", { timeStyle: "short", dateStyle: "short" })}
                </div>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

// ── 用户菜单 ──────────────────────────────────────────────────────────────────

function UserMenu({ onClose }: { onClose: () => void }) {
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) onClose();
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [onClose]);

  return (
    <div
      ref={menuRef}
      className="absolute right-0 top-full mt-2 w-56 bg-[#161b22] border border-[#30363d] rounded-xl shadow-2xl z-50 overflow-hidden"
    >
      <div className="px-4 py-3 border-b border-[#30363d]">
        <div className="text-sm font-semibold text-white">QuantBot 用户</div>
        <div className="text-[10px] text-[#8b949e] mt-0.5">本地模式 · 数据不上传</div>
      </div>
      <div className="py-1">
        {[
          { label: "平台版本", value: "v0.3.0" },
          { label: "数据存储", value: "本地 DuckDB" },
          { label: "运行模式", value: "本地优先" },
        ].map(({ label, value }) => (
          <div key={label} className="flex items-center justify-between px-4 py-2">
            <span className="text-xs text-[#8b949e]">{label}</span>
            <span className="text-xs font-mono text-[#434651]">{value}</span>
          </div>
        ))}
      </div>
      <div className="border-t border-[#30363d] px-4 py-2">
        <div className="flex items-center gap-2 text-[10px] text-[#434651]">
          <Info size={11} />
          © 2026 QuantBot — 本地量化平台
        </div>
      </div>
    </div>
  );
}

// ── 主应用 ────────────────────────────────────────────────────────────────────

export default function App() {
  const [activeTab, setActiveTab] = useState<WorkbenchTab>("run");
  const [searchOpen, setSearchOpen] = useState(false);
  const [notifOpen, setNotifOpen] = useState(false);
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const [alertEvents, setAlertEvents] = useState<AlertEvent[]>([]);
  const [alertLoading, setAlertLoading] = useState(false);
  const [unreadCount, setUnreadCount] = useState(0);

  // ⌘K 快捷键
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setSearchOpen((v) => !v);
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  // 点击通知铃：拉取告警事件
  const handleBellClick = async () => {
    if (notifOpen) {
      setNotifOpen(false);
      return;
    }
    setNotifOpen(true);
    setUserMenuOpen(false);
    setAlertLoading(true);
    try {
      const res = await fetch("/api/alerts/events?limit=20");
      if (res.ok) {
        const data = (await res.json()) as { events: AlertEvent[] };
        setAlertEvents(data.events);
        setUnreadCount(0);
      }
    } catch { /* 静默 */ } finally {
      setAlertLoading(false);
    }
  };

  const handleUserMenuClick = () => {
    setUserMenuOpen((v) => !v);
    setNotifOpen(false);
  };

  const navigate = (tab: WorkbenchTab) => setActiveTab(tab);

  return (
    <>
      <GlobalSearch open={searchOpen} onClose={() => setSearchOpen(false)} onNavigate={navigate} />

      <div className="flex flex-col h-screen bg-[#0B0C0E] text-[#E1E4E8] font-sans selection:bg-white/10">

        {/* ── 顶部导航栏 ───────────────────────────────── */}
        <header className="h-14 border-b border-[#2A2D35] bg-[#151619] flex items-center justify-between px-6 shrink-0 sticky top-0 z-40">
          <div className="flex items-center gap-8">
            {/* Logo */}
            <div className="flex items-center gap-2 shrink-0">
              <div className="w-8 h-8 bg-white rounded-lg flex items-center justify-center">
                <Zap size={18} className="text-black fill-black" />
              </div>
              <span className="text-lg font-bold text-white tracking-tight">QuantBot</span>
            </div>

            {/* Tab 导航 */}
            <nav className="flex items-center gap-1">
              {WORKBENCH_TABS.map((tab) => (
                <button
                  key={tab.key}
                  onClick={() => setActiveTab(tab.key)}
                  className={cn(
                    "px-4 py-1.5 rounded-md text-sm font-medium transition-all flex items-center gap-2",
                    activeTab === tab.key
                      ? "bg-[#2A2D35] text-white"
                      : "text-[#8E9299] hover:text-white hover:bg-[#1C1E22]",
                  )}
                >
                  <tab.icon size={14} />
                  {tab.label}
                </button>
              ))}
            </nav>
          </div>

          {/* 右侧工具区 */}
          <div className="flex items-center gap-4">
            {/* 搜索框 */}
            <button
              onClick={() => setSearchOpen(true)}
              className="hidden lg:flex items-center gap-2 px-3 py-1.5 bg-[#1C1E22] rounded-lg border border-[#2A2D35] text-[#8E9299] hover:border-[#4A4D55] hover:text-white transition-colors"
            >
              <Search size={14} />
              <span className="text-xs">搜索...</span>
              <span className="text-[10px] bg-[#2A2D35] px-1.5 py-0.5 rounded border border-[#3A3D45]">
                ⌘K
              </span>
            </button>

            {/* 通知铃 */}
            <div className="relative">
              <button
                onClick={() => void handleBellClick()}
                className="text-[#8E9299] hover:text-white transition-colors relative p-1"
              >
                <Bell size={20} />
                {unreadCount > 0 && (
                  <span className="absolute -top-0.5 -right-0.5 w-2 h-2 bg-[#FF4D4D] rounded-full border-2 border-[#151619]" />
                )}
                {unreadCount === 0 && (
                  <span className="absolute top-0.5 right-0.5 w-1.5 h-1.5 bg-[#FF4D4D] rounded-full border border-[#151619]" />
                )}
              </button>
              {notifOpen && (
                <NotificationDropdown
                  events={alertEvents}
                  loading={alertLoading}
                  onClose={() => setNotifOpen(false)}
                />
              )}
            </div>

            {/* 用户头像 */}
            <div className="relative">
              <button
                onClick={handleUserMenuClick}
                className="w-8 h-8 rounded-full bg-gradient-to-br from-[#4A4D55] to-[#1C1E22] border border-[#2A2D35] flex items-center justify-center text-white text-xs font-bold cursor-pointer hover:border-[#4A4D55] transition-colors"
              >
                QB
              </button>
              {userMenuOpen && <UserMenu onClose={() => setUserMenuOpen(false)} />}
            </div>
          </div>
        </header>

        {/* ── 主内容 ──────────────────────────────────── */}
        <main className="flex-1 overflow-hidden">
          <Suspense
            fallback={
              <div className="flex min-h-[420px] items-center justify-center">
                <div className="rounded-2xl border border-[#2A2D35] bg-[#151619] px-5 py-4 text-sm text-[#8E9299]">
                  正在加载 {WORKBENCH_TABS.find((tab) => tab.key === activeTab)?.label ?? "模块"}…
                </div>
              </div>
            }
          >
            <WorkbenchShell activeTab={activeTab} onNavigate={navigate} />
          </Suspense>
        </main>

        {/* ── 底部状态栏 ──────────────────────────────── */}
        <footer className="h-8 border-t border-[#2A2D35] bg-[#151619] flex items-center justify-between px-4 shrink-0">
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-1.5">
              <div className="w-1.5 h-1.5 rounded-full bg-[#00C087] animate-pulse" />
              <span className="text-[10px] text-[#8E9299] uppercase font-mono">本地优先</span>
            </div>
            <span className="text-[10px] text-[#8E9299] uppercase font-mono">QuantBot v0.3</span>
          </div>
          <span className="text-[10px] text-[#8E9299] uppercase font-mono">量化交易平台</span>
        </footer>
      </div>
    </>
  );
}
