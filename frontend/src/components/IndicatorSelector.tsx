/**
 * 技术指标叠加层开关组件.
 */
import type { OverlayKey } from "../store/chartStore";

interface IndicatorOption {
  key: OverlayKey | "volume";
  label: string;
  color: string;
}

const OPTIONS: IndicatorOption[] = [
  { key: "volume", label: "成交量", color: "#475569" },
  { key: "ema", label: "EMA(20)", color: "#f59e0b" },
  { key: "bbands", label: "布林带", color: "#818cf8" },
];

interface Props {
  activeOverlays: Set<OverlayKey>;
  showVolume: boolean;
  onToggleOverlay: (key: OverlayKey) => void;
  onToggleVolume: () => void;
}

export default function IndicatorSelector({
  activeOverlays,
  showVolume,
  onToggleOverlay,
  onToggleVolume,
}: Props) {
  const isActive = (key: OverlayKey | "volume") => {
    if (key === "volume") return showVolume;
    return activeOverlays.has(key);
  };

  const handleToggle = (key: OverlayKey | "volume") => {
    if (key === "volume") onToggleVolume();
    else onToggleOverlay(key);
  };

  return (
    <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
      <span style={{ fontSize: 12, color: "#64748b", marginRight: 4 }}>指标:</span>
      {OPTIONS.map((opt) => (
        <button
          key={opt.key}
          onClick={() => handleToggle(opt.key)}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 5,
            padding: "3px 8px",
            fontSize: 11,
            borderRadius: 4,
            border: `1px solid ${isActive(opt.key) ? opt.color : "#334155"}`,
            background: isActive(opt.key) ? `${opt.color}22` : "transparent",
            color: isActive(opt.key) ? opt.color : "#64748b",
            cursor: "pointer",
            transition: "all 0.15s",
          }}
        >
          <span
            style={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              background: isActive(opt.key) ? opt.color : "#334155",
              flexShrink: 0,
            }}
          />
          {opt.label}
        </button>
      ))}
    </div>
  );
}
