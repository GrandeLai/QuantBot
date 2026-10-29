export interface CryptoResearchSummaryLike {
  symbol: string;
  feature_count: number;
  rows: number;
  validation_windows: number;
  mean_accuracy: number;
  mean_strategy_return: number;
  latest_class_probabilities: Record<string, number>;
  reversal_probability: number;
  reversal_signal: string;
  reversal_evidence: string[];
  feature_importance: Record<string, number>;
  window_metrics: Array<{
    accuracy: number;
    strategy_return: number;
  }>;
}

export interface CryptoResearchViewModel {
  directionLabel: string;
  topFactors: Array<{ name: string; valuePct: string }>;
  windowRows: Array<{ label: string; accuracyPct: string; returnPct: string }>;
}

export function buildCryptoResearchViewModel(
  summary: CryptoResearchSummaryLike,
): CryptoResearchViewModel {
  const probs = summary.latest_class_probabilities;
  const directionLabel =
    (probs["1"] ?? 0) >= Math.max(probs["0"] ?? 0, probs["-1"] ?? 0)
      ? "偏多"
      : (probs["-1"] ?? 0) >= Math.max(probs["0"] ?? 0, probs["1"] ?? 0)
        ? "偏空"
        : "观望";

  const topFactors = Object.entries(summary.feature_importance)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5)
    .map(([name, value]) => ({
      name,
      valuePct: `${(value * 100).toFixed(1)}%`,
    }));

  const windowRows = summary.window_metrics.slice(0, 5).map((window, index) => ({
    label: `窗口 ${index + 1}`,
    accuracyPct: `${(window.accuracy * 100).toFixed(1)}%`,
    returnPct: `${(window.strategy_return * 100).toFixed(2)}%`,
  }));

  return {
    directionLabel,
    topFactors,
    windowRows,
  };
}
