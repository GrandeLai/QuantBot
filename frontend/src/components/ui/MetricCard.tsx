/**
 * MetricCard — KPI 指标卡片，可复用于各 Tab.
 */
import { ArrowUpRight, ArrowDownRight } from "lucide-react";
import { cn } from "../../lib/utils";

interface MetricCardProps {
  title: string;
  value: string;
  subValue?: string;
  /** 百分比数字，正为涨，负为跌，undefined 则不显示趋势 */
  trend?: number;
  icon: React.ElementType;
}

export default function MetricCard({
  title,
  value,
  subValue,
  trend,
  icon: Icon,
}: MetricCardProps) {
  return (
    <div className="bg-[#151619] border border-[#2A2D35] rounded-xl p-5 flex flex-col justify-between relative overflow-hidden group hover:border-[#00C087]/30 transition-all">
      <div className="flex justify-between items-start">
        <span className="text-[#8E9299] text-[11px] font-bold font-mono uppercase tracking-wider">
          {title}
        </span>
        <div className="p-2 bg-[#1C1E22] rounded-lg text-[#8E9299] group-hover:text-white transition-colors">
          <Icon size={16} />
        </div>
      </div>

      <div className="mt-4 flex flex-col">
        <span className="text-2xl font-bold text-white tracking-tight">{value}</span>
        <div className="flex items-center gap-1.5 mt-1">
          {trend !== undefined && (
            <span
              className={cn(
                "text-xs font-bold flex items-center gap-0.5",
                trend >= 0 ? "text-[#00C087]" : "text-[#FF4D4D]",
              )}
            >
              {trend >= 0 ? <ArrowUpRight size={12} /> : <ArrowDownRight size={12} />}
              {Math.abs(trend).toFixed(2)}%
            </span>
          )}
          {subValue && <span className="text-[#8E9299] text-xs">{subValue}</span>}
        </div>
      </div>

      {/* hover 底部高亮线 */}
      <div className="absolute bottom-0 left-0 w-full h-0.5 bg-gradient-to-r from-transparent via-[#00C087]/40 to-transparent opacity-0 group-hover:opacity-100 transition-opacity" />
    </div>
  );
}
