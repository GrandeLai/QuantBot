/**
 * Trading instrument metadata.
 */
export interface Symbol {
    /**
     * Asset class taxonomy
     */
    asset_class: AssetClass;
    /**
     * Quote currency ISO 4217 code (or asset code for crypto)
     */
    currency: string;
    /**
     * Listing/trading venue
     */
    exchange: Exchange;
    /**
     * Minimum quantity increment
     */
    lot_size?: number;
    /**
     * Asset symbol identifier
     */
    symbol: string;
    /**
     * Minimum price increment
     */
    tick_size?: number;
}

/**
 * Asset class taxonomy
 */
export enum AssetClass {
    CryptoFutures = "crypto_futures",
    CryptoPerp = "crypto_perp",
    CryptoSpot = "crypto_spot",
    Etf = "etf",
    Index = "index",
    Option = "option",
    Stock = "stock",
}

/**
 * Listing/trading venue
 */
export enum Exchange {
    Arca = "ARCA",
    Hkex = "HKEX",
    Nasdaq = "NASDAQ",
    Nyse = "NYSE",
    Okx = "OKX",
    Opra = "OPRA",
    SSE = "SSE",
    Szse = "SZSE",
}
