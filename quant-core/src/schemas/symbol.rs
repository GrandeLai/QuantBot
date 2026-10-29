// Example code that deserializes and serializes the model.
// extern crate serde;
// #[macro_use]
// extern crate serde_derive;
// extern crate serde_json;
//
// use generated_module::Symbol;
//
// fn main() {
//     let json = r#"{"answer": 42}"#;
//     let model: Symbol = serde_json::from_str(&json).unwrap();
// }

use serde::{Serialize, Deserialize};

/// Trading instrument metadata.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Symbol {
    /// Asset class taxonomy
    pub asset_class: AssetClass,

    /// Quote currency ISO 4217 code (or asset code for crypto)
    pub currency: String,

    /// Listing/trading venue
    pub exchange: Exchange,

    /// Minimum quantity increment
    pub lot_size: Option<f64>,

    /// Asset symbol identifier
    pub symbol: String,

    /// Minimum price increment
    pub tick_size: Option<f64>,
}

/// Asset class taxonomy
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AssetClass {
    #[serde(rename = "crypto_futures")]
    CryptoFutures,

    #[serde(rename = "crypto_perp")]
    CryptoPerp,

    #[serde(rename = "crypto_spot")]
    CryptoSpot,

    Etf,

    Index,

    Option,

    Stock,
}

/// Listing/trading venue
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum Exchange {
    #[serde(rename = "ARCA")]
    Arca,

    #[serde(rename = "HKEX")]
    Hkex,

    #[serde(rename = "NASDAQ")]
    Nasdaq,

    #[serde(rename = "NYSE")]
    Nyse,

    #[serde(rename = "OKX")]
    Okx,

    #[serde(rename = "OPRA")]
    Opra,

    #[serde(rename = "SSE")]
    Sse,

    #[serde(rename = "SZSE")]
    Szse,
}
