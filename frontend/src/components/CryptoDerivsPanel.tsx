/**
 * Crypto Derivatives Panel — phaseF.1.13
 * Live funding/OI snapshot from Binance/OKX + funding extreme analyzer.
 */
import { useState, useCallback } from "react";
import {
  Activity,
  AlertTriangle,
  Bitcoin,
  Coins,
  PlayCircle,
  RefreshCw,
  TrendingDown,
  TrendingUp,
  Zap,
} from "lucide-react";

import { cn } from "../lib/utils";
import {
  fetchCryptoDerivsSnapshot,
  fetchFundingStats,
  type CryptoDerivsSnapshot,
  type FundingExtremeSignal,
  type FundingRateLite,
  type FundingStatsResult,
  type OpenInterestLite,
} from "../api/client";

type Asset = "BTC" | "ETH" | "SOL";
const ASSETS: Asset[] = ["BTC", "ETH", "SOL"];

const SIGNAL_BADGE: Record<FundingExtremeSignal["signal"], string> = {
  contrarian_short: "bg-red-500/20 text-red-400 border-red-500/40",
  contrarian_long: "bg-green-500/20 text-green-400 border-green-500/40",
  neutral: "bg-gray-500/20 text-gray-300 border-gray-500/40",
};

function fmtPct(x: number, digits = 4): string {
  return `${(x * 100).toFixed(digits)}%`;
}

function fmtNum(x: number, digits = 2): string {
  return x.toLocaleString("en-US", { maximumFractionDigits: digits });
}

function parseNumberLines(text: string): number[] {
  return text
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
    .map((s) => Number(s))
    .filter((n) => !Number.isNaN(n));
}

function FundingTable({
  binance,
  okx,
}: {
  binance: FundingRateLite | null;
  okx: FundingRateLite | null;
}) {
  return (
    <div className="bg-[#151619] border border-[#2A2D35] rounded-xl overflow-hidden">
      <div className="px-4 py-2 bg-[#1C1E22] text-[#8E9299] text-[11px] font-bold uppercase">
        Funding Rate (8h)
      </div>
      <div className="divide-y divide-[#2A2D35]">
        {[
          { ex: "Binance", data: binance },
          { ex: "OKX", data: okx },
        ].map((row) => (
          <div key={row.ex} className="px-4 py-3 flex items-center justify-between">
            <span className="text-white text-sm font-mono w-20">{row.ex}</span>
            {row.data ? (
              <>
                <span className="text-[#8E9299] text-xs flex-1 ml-3">{row.data.raw_symbol}</span>
                <span
                  className={cn(
                    "text-sm font-mono font-bold",
                    row.data.funding_rate >= 0 ? "text-green-400" : "text-red-400",
                  )}
                >
                  {fmtPct(row.data.funding_rate)}
                </span>
              </>
            ) : (
              <span className="text-[#8E9299] text-xs italic">unavailable</span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function OITable({
  binance,
  okx,
}: {
  binance: OpenInterestLite | null;
  okx: OpenInterestLite | null;
}) {
  return (
    <div className="bg-[#151619] border border-[#2A2D35] rounded-xl overflow-hidden">
      <div className="px-4 py-2 bg-[#1C1E22] text-[#8E9299] text-[11px] font-bold uppercase">
        Open Interest
      </div>
      <div className="divide-y divide-[#2A2D35]">
        {[
          { ex: "Binance", data: binance },
          { ex: "OKX", data: okx },
        ].map((row) => (
          <div key={row.ex} className="px-4 py-3 flex items-center justify-between">
            <span className="text-white text-sm font-mono w-20">{row.ex}</span>
            {row.data ? (
              <>
                <span className="text-[#8E9299] text-xs flex-1 ml-3">{row.data.raw_symbol}</span>
                <span className="text-white text-sm font-mono">
                  {fmtNum(row.data.open_interest)}
                </span>
                {row.data.open_interest_value !== null && (
                  <span className="text-[#8E9299] text-xs ml-3">
                    ≈ ${fmtNum(row.data.open_interest_value, 0)}
                  </span>
                )}
              </>
            ) : (
              <span className="text-[#8E9299] text-xs italic">unavailable</span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

export default function CryptoDerivsPanel() {
  const [asset, setAsset] = useState<Asset>("BTC");
  const [snapshot, setSnapshot] = useState<CryptoDerivsSnapshot | null>(null);
  const [snapshotLoading, setSnapshotLoading] = useState(false);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);

  // Funding extreme analyzer state
  const [historyText, setHistoryText] = useState<string>("");
  const [currentInput, setCurrentInput] = useState<string>("");
  const [zThreshold, setZThreshold] = useState<string>("2.0");
  const [fundingStats, setFundingStats] = useState<FundingStatsResult | null>(null);
  const [fundingLoading, setFundingLoading] = useState(false);
  const [fundingError, setFundingError] = useState<string | null>(null);

  const onRefreshSnapshot = useCallback(async () => {
    setSnapshotLoading(true);
    setSnapshotError(null);
    try {
      const result = await fetchCryptoDerivsSnapshot(asset);
      setSnapshot(result);
    } catch (e) {
      setSnapshotError(e instanceof Error ? e.message : String(e));
    } finally {
      setSnapshotLoading(false);
    }
  }, [asset]);

  const onAnalyzeFunding = useCallback(async () => {
    const history = parseNumberLines(historyText);
    if (history.length < 30) {
      setFundingError("Need at least 30 funding samples (paste from a public source like Coinglass).");
      return;
    }
    const current = currentInput.trim() ? Number(currentInput) : null;
    if (current !== null && Number.isNaN(current)) {
      setFundingError("Current funding must be a valid number.");
      return;
    }
    const z = Number(zThreshold);
    if (Number.isNaN(z) || z <= 0) {
      setFundingError("Z threshold must be a positive number.");
      return;
    }
    setFundingLoading(true);
    setFundingError(null);
    try {
      const result = await fetchFundingStats(history, current, z);
      setFundingStats(result);
    } catch (e) {
      setFundingError(e instanceof Error ? e.message : String(e));
    } finally {
      setFundingLoading(false);
    }
  }, [historyText, currentInput, zThreshold]);

  const fillCurrentFromSnapshot = useCallback(() => {
    if (!snapshot) return;
    const fr = snapshot.funding.binance ?? snapshot.funding.okx;
    if (fr) setCurrentInput(String(fr.funding_rate));
  }, [snapshot]);

  return (
    <div className="bg-[#0E1014] border border-[#2A2D35] rounded-xl p-6 space-y-6">
      <div className="flex items-center gap-2">
        <Coins size={20} className="text-[#00C087]" />
        <h2 className="text-white text-lg font-bold">Crypto Derivatives</h2>
        <span className="text-[#8E9299] text-xs font-mono">phaseF.1.13</span>
      </div>

      {/* Asset selector */}
      <div className="flex items-center gap-2">
        {ASSETS.map((a) => (
          <button
            key={a}
            type="button"
            onClick={() => setAsset(a)}
            className={cn(
              "flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg border transition-colors",
              asset === a
                ? "bg-[#00C087]/10 border-[#00C087] text-[#00C087] font-bold"
                : "bg-[#151619] border-[#2A2D35] text-[#8E9299] hover:text-white",
            )}
          >
            {a === "BTC" ? <Bitcoin size={14} /> : <Coins size={14} />}
            {a}
          </button>
        ))}
        <button
          type="button"
          onClick={onRefreshSnapshot}
          disabled={snapshotLoading}
          className={cn(
            "ml-auto flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg transition-colors",
            snapshotLoading
              ? "bg-[#1C1E22] text-[#8E9299] cursor-not-allowed"
              : "bg-[#00C087] hover:bg-[#00A574] text-black font-bold",
          )}
        >
          <RefreshCw size={14} className={snapshotLoading ? "animate-spin" : ""} />
          {snapshotLoading ? "Fetching..." : "Refresh Snapshot"}
        </button>
      </div>

      {snapshotError && (
        <div className="flex items-center gap-2 px-3 py-2 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">
          <AlertTriangle size={14} />
          {snapshotError}
        </div>
      )}

      {snapshot && (
        <>
          {Object.keys(snapshot.errors).length > 0 && (
            <div className="flex items-start gap-2 px-3 py-2 bg-yellow-500/10 border border-yellow-500/30 rounded-lg text-yellow-400 text-xs">
              <AlertTriangle size={14} className="mt-0.5" />
              <div>
                <div className="font-bold mb-1">Partial data — some sources failed:</div>
                {Object.entries(snapshot.errors).map(([k, v]) => (
                  <div key={k} className="font-mono">
                    {k}: {v}
                  </div>
                ))}
              </div>
            </div>
          )}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            <FundingTable
              binance={snapshot.funding.binance}
              okx={snapshot.funding.okx}
            />
            <OITable
              binance={snapshot.open_interest.binance}
              okx={snapshot.open_interest.okx}
            />
          </div>
        </>
      )}

      {/* Funding extreme analyzer */}
      <section className="space-y-3 pt-3 border-t border-[#2A2D35]">
        <h3 className="text-white text-sm font-bold uppercase tracking-wider flex items-center gap-2">
          <Zap size={16} className="text-[#00C087]" />
          Funding Extreme Analyzer
        </h3>
        <p className="text-[#8E9299] text-xs">
          Paste 30+ historical funding rates (one per line). Provide current to get a contrarian signal.
        </p>
        <textarea
          value={historyText}
          onChange={(e) => setHistoryText(e.target.value)}
          placeholder="0.00010&#10;0.00012&#10;0.00008&#10;..."
          rows={4}
          className="w-full bg-[#151619] border border-[#2A2D35] rounded-lg px-3 py-2 text-white text-sm font-mono focus:border-[#00C087] focus:outline-none resize-y"
        />
        <div className="flex flex-wrap gap-2 items-end">
          <div className="flex flex-col gap-1">
            <label className="text-[#8E9299] text-[11px] font-bold uppercase">Current Funding</label>
            <input
              type="text"
              value={currentInput}
              onChange={(e) => setCurrentInput(e.target.value)}
              placeholder="0.0001"
              className="w-32 bg-[#151619] border border-[#2A2D35] rounded-lg px-3 py-2 text-white text-sm font-mono focus:border-[#00C087] focus:outline-none"
            />
          </div>
          <button
            type="button"
            onClick={fillCurrentFromSnapshot}
            disabled={!snapshot}
            className={cn(
              "px-3 py-2 text-xs rounded-lg border transition-colors",
              snapshot
                ? "bg-[#1C1E22] border-[#2A2D35] hover:border-[#00C087] text-white"
                : "bg-[#1C1E22] border-[#2A2D35] text-[#8E9299] cursor-not-allowed",
            )}
          >
            Use Snapshot Value
          </button>
          <div className="flex flex-col gap-1">
            <label className="text-[#8E9299] text-[11px] font-bold uppercase">Z Threshold</label>
            <input
              type="number"
              step="0.1"
              min="0.1"
              value={zThreshold}
              onChange={(e) => setZThreshold(e.target.value)}
              className="w-24 bg-[#151619] border border-[#2A2D35] rounded-lg px-3 py-2 text-white text-sm font-mono focus:border-[#00C087] focus:outline-none"
            />
          </div>
          <button
            type="button"
            onClick={onAnalyzeFunding}
            disabled={fundingLoading}
            className={cn(
              "flex items-center gap-1.5 px-4 py-2 text-sm rounded-lg transition-colors",
              fundingLoading
                ? "bg-[#1C1E22] text-[#8E9299] cursor-not-allowed"
                : "bg-[#00C087] hover:bg-[#00A574] text-black font-bold",
            )}
          >
            <PlayCircle size={14} />
            {fundingLoading ? "Analyzing..." : "Analyze"}
          </button>
        </div>
        {fundingError && (
          <div className="flex items-center gap-2 px-3 py-2 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400 text-sm">
            <AlertTriangle size={14} />
            {fundingError}
          </div>
        )}

        {fundingStats && (
          <div className="space-y-3">
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-2 text-xs">
              <div className="bg-[#151619] border border-[#2A2D35] rounded-lg px-3 py-2">
                <div className="text-[#8E9299] uppercase font-bold mb-1">Mean</div>
                <div className="text-white font-mono">{fmtPct(fundingStats.stats.mean, 4)}</div>
              </div>
              <div className="bg-[#151619] border border-[#2A2D35] rounded-lg px-3 py-2">
                <div className="text-[#8E9299] uppercase font-bold mb-1">Std</div>
                <div className="text-white font-mono">{fmtPct(fundingStats.stats.std, 4)}</div>
              </div>
              <div className="bg-[#151619] border border-[#2A2D35] rounded-lg px-3 py-2">
                <div className="text-[#8E9299] uppercase font-bold mb-1">P5 / P95</div>
                <div className="text-white font-mono text-[11px]">
                  {fmtPct(fundingStats.stats.p5, 4)} / {fmtPct(fundingStats.stats.p95, 4)}
                </div>
              </div>
              <div className="bg-[#151619] border border-[#2A2D35] rounded-lg px-3 py-2">
                <div className="text-[#8E9299] uppercase font-bold mb-1">Samples</div>
                <div className="text-white font-mono">{fundingStats.n_samples}</div>
              </div>
            </div>
            {fundingStats.signal && (
              <div className="bg-[#151619] border border-[#2A2D35] rounded-lg px-4 py-3 flex items-center justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-2">
                  {fundingStats.signal.signal === "contrarian_short" ? (
                    <TrendingDown size={18} className="text-red-400" />
                  ) : fundingStats.signal.signal === "contrarian_long" ? (
                    <TrendingUp size={18} className="text-green-400" />
                  ) : (
                    <Activity size={18} className="text-gray-400" />
                  )}
                  <span
                    className={cn(
                      "px-3 py-1 rounded-full border text-xs font-bold uppercase",
                      SIGNAL_BADGE[fundingStats.signal.signal],
                    )}
                  >
                    {fundingStats.signal.signal.replace("_", " ")}
                  </span>
                </div>
                <div className="flex gap-4 text-xs font-mono">
                  <span className="text-[#8E9299]">
                    Z: <span className="text-white">{fundingStats.signal.z_score.toFixed(3)}</span>
                  </span>
                  <span className="text-[#8E9299]">
                    Pctile:{" "}
                    <span className="text-white">{fundingStats.signal.percentile.toFixed(1)}</span>
                  </span>
                </div>
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
