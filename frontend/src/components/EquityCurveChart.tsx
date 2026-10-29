/**
 * EquityCurveChart — 纯 SVG 权益曲线折线图.
 * 无外部图表依赖；响应式 viewBox 布局.
 */
import { useState } from "react";
import { cn } from "@/lib/utils";

interface EquityCurveChartProps {
  data: number[];        // equity curve values (portfolio value at each bar)
  initialCash: number;   // baseline horizontal line
  className?: string;
}

function formatMoney(v: number): string {
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(1)}K`;
  return `$${v.toFixed(0)}`;
}

export default function EquityCurveChart({ data, initialCash, className }: EquityCurveChartProps) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  // constants
  const W = 600, H = 160;
  const PAD = { l: 60, r: 16, t: 12, b: 12 };
  const innerW = W - PAD.l - PAD.r;
  const innerH = H - PAD.t - PAD.b;

  if (data.length < 2) {
    return (
      <div className={cn("flex items-center justify-center h-40 text-[#8b949e] text-sm", className)}>
        暂无权益曲线数据
      </div>
    );
  }

  const minVal = Math.min(...data);
  const maxVal = Math.max(...data);
  const range = maxVal - minVal || 1;

  const toX = (i: number) => PAD.l + (i / (data.length - 1)) * innerW;
  const toY = (v: number) => PAD.t + (1 - (v - minVal) / range) * innerH;

  const points = data.map((v, i) => `${toX(i)},${toY(v)}`).join(" ");
  const profitable = data[data.length - 1] >= initialCash;
  const lineColor = profitable ? "#22c55e" : "#ef4444";
  const fillColor = profitable ? "rgba(34,197,94,0.08)" : "rgba(239,68,68,0.08)";

  // filled area path (close to bottom)
  const areaPoints = [
    `${PAD.l},${H - PAD.b}`,
    ...data.map((v, i) => `${toX(i)},${toY(v)}`),
    `${PAD.l + innerW},${H - PAD.b}`,
  ].join(" ");

  // baseline Y position (clamp to chart area)
  const baseY = Math.max(PAD.t, Math.min(H - PAD.b, toY(initialCash)));

  const handleMouseMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const svgEl = e.currentTarget;
    const rect = svgEl.getBoundingClientRect();
    const scaleX = W / rect.width;
    const xInSvg = (e.clientX - rect.left) * scaleX;
    const fraction = Math.max(0, Math.min(1, (xInSvg - PAD.l) / innerW));
    const idx = Math.round(fraction * (data.length - 1));
    setHoverIdx(idx);
  };

  // Y-axis labels: min, initial, max
  const yLabels = [
    { v: maxVal, y: toY(maxVal) },
    { v: initialCash, y: baseY },
    { v: minVal, y: toY(minVal) },
  ];

  return (
    <div className={cn("relative", className)}>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        className="overflow-visible"
        onMouseMove={handleMouseMove}
        onMouseLeave={() => setHoverIdx(null)}
      >
        {/* area fill */}
        <polygon points={areaPoints} fill={fillColor} />

        {/* baseline dashed line */}
        <line
          x1={PAD.l} y1={baseY} x2={PAD.l + innerW} y2={baseY}
          stroke="#30363d" strokeWidth="1" strokeDasharray="4 4"
        />

        {/* equity line */}
        <polyline points={points} fill="none" stroke={lineColor} strokeWidth="1.5" strokeLinejoin="round" />

        {/* Y-axis labels */}
        {yLabels.map(({ v, y }, i) => (
          <text key={i} x={PAD.l - 6} y={y} textAnchor="end" dominantBaseline="middle"
            fontSize="9" fill="#8b949e" fontFamily="monospace">
            {formatMoney(v)}
          </text>
        ))}

        {/* hover crosshair */}
        {hoverIdx !== null && (
          <>
            <line
              x1={toX(hoverIdx)} y1={PAD.t}
              x2={toX(hoverIdx)} y2={H - PAD.b}
              stroke="#8b949e" strokeWidth="1" strokeDasharray="3 3"
            />
            <circle cx={toX(hoverIdx)} cy={toY(data[hoverIdx])} r="3"
              fill={lineColor} stroke="#0d1117" strokeWidth="1.5" />
          </>
        )}
      </svg>

      {/* tooltip */}
      {hoverIdx !== null && (() => {
        const v = data[hoverIdx];
        const pnl = initialCash !== 0 ? ((v - initialCash) / initialCash) * 100 : 0;
        return (
          <div
            className="absolute top-0 pointer-events-none bg-[#161b22] border border-[#30363d] rounded-lg px-2 py-1.5 text-[10px] font-mono"
            style={{
              left: `${(toX(hoverIdx) / W) * 100}%`,
              transform: hoverIdx > data.length * 0.7 ? "translateX(-110%)" : "translateX(8px)",
            }}
          >
            <div className="text-white font-bold">{formatMoney(v)}</div>
            <div className={pnl >= 0 ? "text-green-400" : "text-red-400"}>
              {pnl >= 0 ? "+" : ""}{pnl.toFixed(2)}%
            </div>
            <div className="text-[#8b949e]">Bar {hoverIdx + 1}</div>
          </div>
        );
      })()}
    </div>
  );
}
