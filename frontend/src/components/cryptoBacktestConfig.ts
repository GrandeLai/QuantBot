/**
 * Helpers for the crypto backtest experience.
 */

export interface CryptoBacktestStrategySummary {
  id: string;
  name: string;
  description: string;
  default_params: Record<string, unknown>;
}

const DEFAULT_STRATEGY_ID = "vwap_ema_trend";

export function prioritizeCryptoStrategies<T extends CryptoBacktestStrategySummary>(strategies: T[]): T[] {
  return [...strategies].sort((left, right) => {
    if (left.id === DEFAULT_STRATEGY_ID) return -1;
    if (right.id === DEFAULT_STRATEGY_ID) return 1;
    return left.name.localeCompare(right.name);
  });
}

export function defaultCryptoBacktestStrategy(strategies: CryptoBacktestStrategySummary[]): string {
  return prioritizeCryptoStrategies(strategies)[0]?.id ?? "";
}

export function recommendedCryptoBacktestTimeframe(strategyId: string): string {
  return strategyId === DEFAULT_STRATEGY_ID ? "1h" : "1d";
}
