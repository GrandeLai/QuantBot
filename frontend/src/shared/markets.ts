/**
 * Shared product market universe.
 *
 * Keep this aligned with `quantbot.common.data.universe` until the schema
 * pipeline owns this contract end-to-end.
 */

export type ProductId = "quant-assistant" | "stock-assistant";
export type DataSource = "auto" | "yfinance" | "akshare";
export type AssetType =
  | "stock"
  | "future"
  | "option"
  | "crypto"
  | "etf"
  | "forex"
  | "bond";

export interface SupportedInstrument {
  symbol: string;
  name: string;
  exchange: string;
  asset_type: AssetType;
  currency: string;
  default_source: DataSource;
  products: ProductId[];
  aliases: string[];
  description: string;
}

export const MARKET_UNIVERSE: readonly SupportedInstrument[] = [
  {
    symbol: "AAPL",
    name: "Apple Inc.",
    exchange: "NASDAQ",
    asset_type: "stock",
    currency: "USD",
    default_source: "yfinance",
    products: ["quant-assistant", "stock-assistant"],
    aliases: ["APPLE"],
    description: "US mega-cap equity for stock decisions and strategy research.",
  },
  {
    symbol: "MSFT",
    name: "Microsoft Corporation",
    exchange: "NASDAQ",
    asset_type: "stock",
    currency: "USD",
    default_source: "yfinance",
    products: ["quant-assistant", "stock-assistant"],
    aliases: ["MICROSOFT"],
    description: "US mega-cap equity.",
  },
  {
    symbol: "NVDA",
    name: "NVIDIA Corporation",
    exchange: "NASDAQ",
    asset_type: "stock",
    currency: "USD",
    default_source: "yfinance",
    products: ["quant-assistant", "stock-assistant"],
    aliases: ["NVIDIA"],
    description: "US semiconductor equity.",
  },
  {
    symbol: "SPY",
    name: "SPDR S&P 500 ETF Trust",
    exchange: "NYSE",
    asset_type: "etf",
    currency: "USD",
    default_source: "yfinance",
    products: ["quant-assistant", "stock-assistant"],
    aliases: ["S&P500", "SP500"],
    description: "US broad-market ETF benchmark.",
  },
  {
    symbol: "BTC-USDT",
    name: "Bitcoin / USDT Spot",
    exchange: "OKX",
    asset_type: "crypto",
    currency: "USDT",
    default_source: "auto",
    products: ["quant-assistant", "stock-assistant"],
    aliases: ["BTCUSDT", "BTC/USDT", "BTC-USD"],
    description: "Crypto spot market pair.",
  },
  {
    symbol: "ETH-USDT",
    name: "Ethereum / USDT Spot",
    exchange: "OKX",
    asset_type: "crypto",
    currency: "USDT",
    default_source: "auto",
    products: ["quant-assistant", "stock-assistant"],
    aliases: ["ETHUSDT", "ETH/USDT", "ETH-USD"],
    description: "Crypto spot market pair.",
  },
  {
    symbol: "SOL-USDT",
    name: "Solana / USDT Spot",
    exchange: "OKX",
    asset_type: "crypto",
    currency: "USDT",
    default_source: "auto",
    products: ["quant-assistant", "stock-assistant"],
    aliases: ["SOLUSDT", "SOL/USDT"],
    description: "Crypto spot market pair.",
  },
  {
    symbol: "0700.HK",
    name: "Tencent Holdings",
    exchange: "HKEX",
    asset_type: "stock",
    currency: "HKD",
    default_source: "yfinance",
    products: ["quant-assistant", "stock-assistant"],
    aliases: ["TENCENT", "700.HK"],
    description: "Hong Kong equity example.",
  },
  {
    symbol: "600519",
    name: "Kweichow Moutai",
    exchange: "SSE",
    asset_type: "stock",
    currency: "CNY",
    default_source: "akshare",
    products: ["quant-assistant", "stock-assistant"],
    aliases: ["贵州茅台"],
    description: "A-share equity example.",
  },
] as const;

export function instrumentsForProduct(product: ProductId): SupportedInstrument[] {
  return MARKET_UNIVERSE.filter((instrument) => instrument.products.includes(product));
}

export function findInstrument(symbol: string): SupportedInstrument | undefined {
  const normalized = symbol.trim().toUpperCase();
  return MARKET_UNIVERSE.find(
    (instrument) =>
      instrument.symbol.toUpperCase() === normalized ||
      instrument.aliases.some((alias) => alias.toUpperCase() === normalized),
  );
}
