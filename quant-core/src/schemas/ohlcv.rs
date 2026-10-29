// Example code that deserializes and serializes the model.
// extern crate serde;
// #[macro_use]
// extern crate serde_derive;
// extern crate serde_json;
//
// use generated_module::Ohlcv;
//
// fn main() {
//     let json = r#"{"answer": 42}"#;
//     let model: Ohlcv = serde_json::from_str(&json).unwrap();
// }

use serde::{Serialize, Deserialize};

/// OHLCV bar data point. One row per (symbol, timestamp).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Ohlcv {
    /// Whether prices are adjusted for splits/dividends
    pub adjusted: Option<bool>,

    /// Closing price
    pub close: f64,

    /// Highest price during the bar
    pub high: f64,

    /// Lowest price during the bar
    pub low: f64,

    /// Opening price
    pub open: f64,

    /// Asset symbol identifier
    pub symbol: String,

    /// ISO 8601 UTC timestamp at the bar start
    pub timestamp: String,

    /// Volume traded during the bar
    pub volume: f64,
}
