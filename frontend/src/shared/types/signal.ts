/**
 * Strategy-generated trading signal.
 */
export interface Signal {
    /**
     * Direction the strategy proposes
     */
    action: Action;
    /**
     * Human-readable explanation, optional
     */
    reason?: string;
    /**
     * Unique signal identifier (UUID or deterministic hash)
     */
    signal_id: string;
    /**
     * Strategy id or model id that produced this signal
     */
    source?: string;
    /**
     * Confidence/sizing weight in [0, 1]
     */
    strength?: number;
    symbol:    string;
    timestamp: Date;
}

/**
 * Direction the strategy proposes
 */
export enum Action {
    Flat = "flat",
    Hold = "hold",
    Long = "long",
    Short = "short",
}
