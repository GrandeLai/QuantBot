/**
 * 多周期切换组件.
 */
import { TIMEFRAMES, type Timeframe } from "../store/chartStore";

interface Props {
  value: Timeframe;
  onChange: (t: Timeframe) => void;
}

const LABELS: Record<Timeframe, string> = {
  "1m": "1分",
  "5m": "5分",
  "15m": "15分",
  "1h": "1时",
  "4h": "4时",
  "1d": "日线",
  "1w": "周线",
};

export default function TimeframeSelector({ value, onChange }: Props) {
  return (
    <div style={{ display: "flex", gap: 4 }}>
      {TIMEFRAMES.map((tf) => (
        <button
          key={tf}
          onClick={() => onChange(tf)}
          style={{
            padding: "4px 10px",
            fontSize: 12,
            borderRadius: 4,
            border: "none",
            cursor: "pointer",
            background: value === tf ? "#3b82f6" : "#1e293b",
            color: value === tf ? "#fff" : "#94a3b8",
            fontWeight: value === tf ? 600 : 400,
            transition: "background 0.15s",
          }}
        >
          {LABELS[tf]}
        </button>
      ))}
    </div>
  );
}
