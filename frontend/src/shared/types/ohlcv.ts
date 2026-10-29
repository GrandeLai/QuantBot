/**
 * OHLCV bar data point. One row per (symbol, timestamp).
 */
export interface Ohlcv {
    /**
     * Whether prices are adjusted for splits/dividends
     */
    adjusted?: boolean;
    /**
     * Closing price
     */
    close: number;
    /**
     * Highest price during the bar
     */
    high: number;
    /**
     * Lowest price during the bar
     */
    low: number;
    /**
     * Opening price
     */
    open: number;
    /**
     * Asset symbol identifier
     */
    symbol: string;
    /**
     * ISO 8601 UTC timestamp at the bar start
     */
    timestamp: Date;
    /**
     * Volume traded during the bar
     */
    volume: number;
}
