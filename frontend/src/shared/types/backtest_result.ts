/**
 * Outcome of a backtest run.
 */
export interface BacktestResult {
    /**
     * Unique identifier for this backtest run
     */
    backtest_id: string;
    /**
     * Strategy id of the BacktestConfig used (independent reference; full config logged
     * separately)
     */
    config_strategy_id: string;
    /**
     * Sampled equity curve points
     */
    equity_curve: EquityCurve[];
    metrics:      Metrics;
    ran_at?:      Date;
    /**
     * Executed trades, optional
     */
    trades?: Trade[];
}

export interface EquityCurve {
    equity:    number;
    timestamp: Date;
    [property: string]: any;
}

export interface Metrics {
    /**
     * Max drawdown (fraction, positive)
     */
    max_drawdown: number;
    sharpe_ratio: number;
    /**
     * Cumulative return (fraction)
     */
    total_return: number;
    trade_count:  number;
    win_rate?:    number;
    [property: string]: any;
}

export interface Trade {
    entry_price:     number;
    entry_timestamp: Date;
    exit_price:      number;
    exit_timestamp:  Date;
    pnl:             number;
    qty:             number;
    side:            Side;
    symbol:          string;
    [property: string]: any;
}

export enum Side {
    Long = "long",
    Short = "short",
}
