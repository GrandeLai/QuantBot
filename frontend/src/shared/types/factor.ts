/**
 * Single computed factor value at a point in time.
 */
export interface Factor {
    /**
     * Factor identifier, e.g. 'sma_20', 'rsi_14'
     */
    factor_id: string;
    symbol:    string;
    timestamp: Date;
    /**
     * Computed factor value (NaN encoded as JSON null)
     */
    value: number;
    /**
     * Factor implementation version, optional
     */
    version?: string;
}
