/**
 * Configuration for a backtest run.
 */
export interface BacktestConfig {
    /**
     * Bar frequency
     */
    data_frequency?: DataFrequency;
    /**
     * Inclusive end date (ISO 8601 date)
     */
    end_date: Date;
    /**
     * Per-trade fee rate (fraction, e.g. 0.001 = 10 bps)
     */
    fee_rate?: number;
    /**
     * How the simulator fills orders
     */
    fill_model?: FillModel;
    /**
     * Starting cash
     */
    initial_capital: number;
    /**
     * Assumed slippage in basis points
     */
    slippage_bps?: number;
    /**
     * Inclusive start date (ISO 8601 date)
     */
    start_date: Date;
    /**
     * Strategy identifier (Rhai script name or native trait id)
     */
    strategy_id: string;
    /**
     * Strategy-specific parameter object; schema strategy-defined
     */
    strategy_params?: { [key: string]: any };
    /**
     * List of symbols traded in the backtest
     */
    universe: string[];
}

/**
 * Bar frequency
 */
export enum DataFrequency {
    The15M = "15m",
    The1D = "1d",
    The1H = "1h",
    The1M = "1m",
    The30M = "30m",
    The5M = "5m",
}

/**
 * How the simulator fills orders
 */
export enum FillModel {
    NextClose = "next_close",
    NextOpen = "next_open",
    Vwap = "vwap",
}
