/**
 * Shared loading / empty / error state visuals for assistant views.
 */
import { AlertTriangle, Inbox, Loader2, ServerOff } from "lucide-react";

import { cn } from "../../lib/utils";

export function LoadingState({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        "flex items-center gap-2 px-4 py-6 text-[#8E9299] text-sm",
        className,
      )}
    >
      <Loader2 size={16} className="animate-spin" />
      加载中…
    </div>
  );
}

interface ErrorOrEmptyProps {
  error: string;
  onRetry?: () => void;
}

/**
 * Smart error renderer — turns 404 / "advisor backend not implemented"
 * into a friendly empty state instead of leaking the raw HTTP error.
 */
export function ErrorOrEmptyState({ error, onRetry }: ErrorOrEmptyProps) {
  const isUnimplemented = /404|Request failed: 4|not found/i.test(error);
  if (isUnimplemented) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 px-4 py-10 text-center bg-[#151619] border border-dashed border-[#2A2D35] rounded-lg">
        <ServerOff size={28} className="text-[#8E9299]" />
        <div className="text-white text-sm font-bold">Advisor 后端尚未接入</div>
        <p className="text-[#8E9299] text-xs max-w-md">
          此面板依赖 <code className="font-mono text-[#00C087]">/api/advisor/*</code> 接口，
          stock-assistant 后端目前未实现该路由。
          后续会在独立任务 <code className="font-mono">phaseF.assistant-advisor-backend</code> 中提供。
        </p>
      </div>
    );
  }
  return (
    <div className="flex items-start gap-2 px-3 py-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">
      <AlertTriangle size={14} className="mt-0.5 shrink-0" />
      <div className="flex-1">
        <div className="font-bold mb-1">加载失败</div>
        <div className="font-mono text-xs break-all">{error}</div>
      </div>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="ml-auto px-2 py-1 text-xs bg-red-500/20 hover:bg-red-500/30 border border-red-500/40 rounded text-red-300"
        >
          重试
        </button>
      )}
    </div>
  );
}

export function EmptyState({ message }: { message: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-4 py-8 text-center bg-[#151619] border border-dashed border-[#2A2D35] rounded-lg">
      <Inbox size={24} className="text-[#8E9299]" />
      <p className="text-[#8E9299] text-sm">{message}</p>
    </div>
  );
}

interface KPICardProps {
  title: string;
  value: string;
  subValue?: string;
}

export function KPICard({ title, value, subValue }: KPICardProps) {
  return (
    <div className="bg-[#151619] border border-[#2A2D35] rounded-xl p-4 flex flex-col gap-1.5 hover:border-[#00C087]/40 transition-colors">
      <span className="text-[#8E9299] text-[11px] font-bold font-mono uppercase tracking-wider">
        {title}
      </span>
      <span className="text-2xl font-bold text-white tracking-tight">{value}</span>
      {subValue && <span className="text-[#8E9299] text-xs">{subValue}</span>}
    </div>
  );
}
