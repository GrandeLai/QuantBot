/**
 * Risk Metrics Panel — phaseF.1.5
 * 集成风控三件套：Vol Target / Sharpe Decay / VaR / Kelly
 * API 来自 F.1.4 暴露的 /api/risk/* 端点。
 */
import { useState, useCallback } from "react";
import {
  Activity,
  AlertTriangle,
  Calculator,
  Gauge,
  PlayCircle,
  ShieldAlert,
  Target,
  TrendingDown,
} from "lucide-react";

import MetricCard from "./ui/MetricCard";
import { cn } from "../lib/utils";
import {
  fetchRiskKellyBinary,
  fetchRiskSummary,
  type DecayAlertLevel,
  type KellyResult,
  type RiskSummaryResult,
  type VolRegime,
} from "../api/client";

const REGIME_BADGE: Record<VolRegime, string> = {
  low: "bg-blue-500/20 text-blue-400 border-blue-500/40",
  normal: "bg-green-500/20 text-green-400 border-green-500/40",
  high: "bg-orange-500/20 text-orange-400 border-orange-500/40",
  crisis: "bg-red-500/20 text-red-400 border-red-500/40",
};

const ALERT_BADGE: Record<DecayAlertLevel, string> = {
  green: "bg-green-500/20 text-green-400 border-green-500/40",
  yellow: "bg-yellow-500/20 text-yellow-400 border-yellow-500/40",
  red: "bg-red-500/20 text-red-400 border-red-500/40",
};

function parseReturns(text: string): number[] {
  return text
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
    .map((s) => Number(s))
    .filter((n) => !Number.isNaN(n));
}

function fmtPct(x: number): string {
  return `${(x * 100).toFixed(2)}%`;
}

function fmtNum(x: number, digits = 3): string {
  return x.toFixed(digits);
}

export default function RiskMetricsPanel() {
  const [returnsText, setReturnsText] = useState<string>("");
  const [summary, setSummary] = useState<RiskSummaryResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Kelly calculator state
  const [winRate, setWinRate] = useState<string>("0.55");
  const [payoffRatio, setPayoffRatio] = useState<string>("1.5");
  const [kellyResult, setKellyResult] = useState<KellyResult | null>(null);
  const [kellyLoading, setKellyLoading] = useState(false);
  const [kellyError, setKellyError] = useState<string | null>(null);

  const onAnalyze = useCallback(async () => {
    const arr = parseReturns(returnsText);
    if (arr.length < 30) {
      setError("Need at least 30 return samples from a real portfolio or strategy.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await fetchRiskSummary(arr);
      setSummary(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [returnsText]);

  const onComputeKelly = useCallback(async () => {
    const w = Number(winRate);
    const p = Number(payoffRatio);
    if (Number.isNaN(w) || Number.isNaN(p)) {
      setKellyError("Win rate and payoff ratio must be numbers.");
      return;
    }
    setKellyLoading(true);
    setKellyError(null);
    try {
      const r = await fetchRiskKellyBinary(w, p);
      setKellyResult(r);
    } catch (e) {
      setKellyError(e instanceof Error ? e.message : String(e));
    } finally {
      setKellyLoading(false);
    }
  }, [winRate, payoffRatio]);

  return (
    <div className="bg-[#0E1014] border border-[#2A2D35] rounded-xl p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <ShieldAlert size={20} className="text-[#00C087]" />
          <h2 className="text-white text-lg font-bold">Risk Metrics</h2>
          <span className="text-[#8E9299] text-xs font-mono">phaseF.1.5</span>
        </div>
      </div>

      {/* 输入区 */}
      <div className="space-y-3">
        <label className="text-[#8E9299] text-xs font-bold uppercase tracking-wider">
          Returns Series (one per line, or comma-separated)
        </label>
        <textarea
          value={returnsText}
          onChange={(e) => setReturnsText(e.target.value)}
          placeholder="0.0123&#10;-0.0045&#10;0.0078&#10;..."
          rows={4}
          className="w-full bg-[#151619] border border-[#2A2D35] rounded-lg px-3 py-2 text-white text-sm font-mono focus:border-[#00C087] focus:outline-none resize-y"
        />
        <div className="flex gap-2">
          <button
            type="button"
            onClick={onAnalyze}
            disabled={loading}
            className={cn(
              "flex items-center gap-1.5 px-4 py-2 text-sm rounded-lg transition-colors",
              loading
                ? "bg-[#1C1E22] text-[#8E9299] cursor-not-allowed"
                : "bg-[#00C087] hover:bg-[#00A574] text-black font-bold",
            )}
          >
            <PlayCircle size={14} />
            {loading ? "Analyzing..." : "Analyze Risk"}
          </button>
        </div>
        {error && (
          <div className="flex items-center gap-2 px-3 py-2 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">
            <AlertTriangle size={14} />
            {error}
          </div>
        )}
      </div>

      {/* Vol Target */}
      {summary && (
        <section className="space-y-3">
          <h3 className="text-white text-sm font-bold uppercase tracking-wider flex items-center gap-2">
            <Target size={16} className="text-[#00C087]" />
            Vol Target
          </h3>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <MetricCard
              title="Realized Vol"
              value={fmtPct(summary.vol_target.realized_vol)}
              icon={Activity}
            />
            <MetricCard
              title="Target Vol"
              value={fmtPct(summary.vol_target.target_vol)}
              icon={Target}
            />
            <MetricCard
              title="Scale Factor"
              value={fmtNum(summary.vol_target.scale_factor)}
              subValue={summary.vol_target.scale_factor < 1 ? "reduce position" : "full position"}
              icon={Gauge}
            />
            <div className="bg-[#151619] border border-[#2A2D35] rounded-xl p-5 flex flex-col justify-between">
              <span className="text-[#8E9299] text-[11px] font-bold font-mono uppercase tracking-wider">
                Regime
              </span>
              <div className="mt-4">
                <span
                  className={cn(
                    "inline-block px-3 py-1 rounded-full border text-sm font-bold uppercase",
                    REGIME_BADGE[summary.vol_target.regime],
                  )}
                >
                  {summary.vol_target.regime}
                </span>
              </div>
            </div>
          </div>
        </section>
      )}

      {/* Sharpe Decay */}
      {summary && (
        <section className="space-y-3">
          <h3 className="text-white text-sm font-bold uppercase tracking-wider flex items-center gap-2">
            <TrendingDown size={16} className="text-[#00C087]" />
            Sharpe Decay
          </h3>
          {summary.sharpe_decay ? (
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
              <MetricCard
                title="Recent Sharpe"
                value={fmtNum(summary.sharpe_decay.recent_sharpe)}
                icon={Activity}
              />
              <MetricCard
                title="Baseline μ ± σ"
                value={fmtNum(summary.sharpe_decay.baseline_mean)}
                subValue={`σ=${fmtNum(summary.sharpe_decay.baseline_std)}`}
                icon={Activity}
              />
              <MetricCard
                title="Z-Score"
                value={fmtNum(summary.sharpe_decay.z_score)}
                subValue={`n=${summary.sharpe_decay.n_baseline_samples}`}
                icon={Gauge}
              />
              <div className="bg-[#151619] border border-[#2A2D35] rounded-xl p-5 flex flex-col justify-between">
                <span className="text-[#8E9299] text-[11px] font-bold font-mono uppercase tracking-wider">
                  Alert Level
                </span>
                <div className="mt-4">
                  <span
                    className={cn(
                      "inline-block px-3 py-1 rounded-full border text-sm font-bold uppercase",
                      ALERT_BADGE[summary.sharpe_decay.alert_level],
                    )}
                  >
                    {summary.sharpe_decay.alert_level}
                  </span>
                </div>
              </div>
            </div>
          ) : (
            <div className="text-[#8E9299] text-sm italic">
              Not enough history (need ≥ 325 samples for default windows). Load more returns.
            </div>
          )}
        </section>
      )}

      {/* VaR / CVaR */}
      {summary && (
        <section className="space-y-3">
          <h3 className="text-white text-sm font-bold uppercase tracking-wider flex items-center gap-2">
            <AlertTriangle size={16} className="text-[#00C087]" />
            Value at Risk / Expected Shortfall
          </h3>
          <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
            <MetricCard
              title="Worst Loss"
              value={fmtPct(summary.var.worst_loss)}
              icon={TrendingDown}
            />
            <MetricCard
              title="VaR 95"
              value={fmtPct(summary.var.var_95 ?? 0)}
              icon={AlertTriangle}
            />
            <MetricCard
              title="CVaR 95"
              value={fmtPct(summary.var.cvar_95 ?? 0)}
              icon={AlertTriangle}
            />
            <MetricCard
              title="VaR 99"
              value={fmtPct(summary.var.var_99 ?? 0)}
              icon={AlertTriangle}
            />
            <MetricCard
              title="CVaR 99"
              value={fmtPct(summary.var.cvar_99 ?? 0)}
              icon={AlertTriangle}
            />
          </div>
        </section>
      )}

      {/* Kelly Calculator */}
      <section className="space-y-3 pt-3 border-t border-[#2A2D35]">
        <h3 className="text-white text-sm font-bold uppercase tracking-wider flex items-center gap-2">
          <Calculator size={16} className="text-[#00C087]" />
          Kelly Calculator (Binary)
        </h3>
        <div className="flex flex-wrap gap-3 items-end">
          <div className="flex flex-col gap-1">
            <label className="text-[#8E9299] text-[11px] font-bold uppercase">Win Rate</label>
            <input
              type="number"
              step="0.01"
              min="0"
              max="1"
              value={winRate}
              onChange={(e) => setWinRate(e.target.value)}
              className="w-28 bg-[#151619] border border-[#2A2D35] rounded-lg px-3 py-2 text-white text-sm font-mono focus:border-[#00C087] focus:outline-none"
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[#8E9299] text-[11px] font-bold uppercase">Payoff Ratio</label>
            <input
              type="number"
              step="0.1"
              min="0"
              value={payoffRatio}
              onChange={(e) => setPayoffRatio(e.target.value)}
              className="w-28 bg-[#151619] border border-[#2A2D35] rounded-lg px-3 py-2 text-white text-sm font-mono focus:border-[#00C087] focus:outline-none"
            />
          </div>
          <button
            type="button"
            onClick={onComputeKelly}
            disabled={kellyLoading}
            className={cn(
              "flex items-center gap-1.5 px-4 py-2 text-sm rounded-lg transition-colors",
              kellyLoading
                ? "bg-[#1C1E22] text-[#8E9299] cursor-not-allowed"
                : "bg-[#00C087] hover:bg-[#00A574] text-black font-bold",
            )}
          >
            <Calculator size={14} />
            {kellyLoading ? "..." : "Compute"}
          </button>
        </div>
        {kellyError && (
          <div className="flex items-center gap-2 px-3 py-2 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">
            <AlertTriangle size={14} />
            {kellyError}
          </div>
        )}
        {kellyResult && (
          <div className="grid grid-cols-3 gap-3">
            <MetricCard
              title="Full Kelly"
              value={fmtPct(kellyResult.full_kelly)}
              icon={Calculator}
            />
            <MetricCard
              title={`Fractional (×${kellyResult.fraction})`}
              value={fmtPct(kellyResult.fractional_kelly)}
              icon={Calculator}
            />
            <MetricCard
              title={`Capped (≤${fmtPct(kellyResult.cap)})`}
              value={fmtPct(kellyResult.capped_kelly)}
              icon={ShieldAlert}
            />
          </div>
        )}
      </section>
    </div>
  );
}
