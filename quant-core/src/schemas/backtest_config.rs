// Example code that deserializes and serializes the model.
// extern crate serde;
// #[macro_use]
// extern crate serde_derive;
// extern crate serde_json;
//
// use generated_module::BacktestConfig;
//
// fn main() {
//     let json = r#"{"answer": 42}"#;
//     let model: BacktestConfig = serde_json::from_str(&json).unwrap();
// }

use serde::{Serialize, Deserialize};
use std::collections::HashMap;

/// Configuration for a backtest run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BacktestConfig {
    /// Bar frequency
    pub data_frequency: Option<DataFrequency>,

    /// Inclusive end date (ISO 8601 date)
    pub end_date: String,

    /// Per-trade fee rate (fraction, e.g. 0.001 = 10 bps)
    pub fee_rate: Option<f64>,

    /// How the simulator fills orders
    pub fill_model: Option<FillModel>,

    /// Starting cash
    pub initial_capital: f64,

    /// Assumed slippage in basis points
    pub slippage_bps: Option<f64>,

    /// Inclusive start date (ISO 8601 date)
    pub start_date: String,

    /// Strategy identifier (Rhai script name or native trait id)
    pub strategy_id: String,

    /// Strategy-specific parameter object; schema strategy-defined
    pub strategy_params: Option<HashMap<String, Option<serde_json::Value>>>,

    /// List of symbols traded in the backtest
    pub universe: Vec<String>,
}

/// Bar frequency
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum DataFrequency {
    #[serde(rename = "15m")]
    The15M,

    #[serde(rename = "1d")]
    The1D,

    #[serde(rename = "1h")]
    The1H,

    #[serde(rename = "1m")]
    The1M,

    #[serde(rename = "30m")]
    The30M,

    #[serde(rename = "5m")]
    The5M,
}

/// How the simulator fills orders
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FillModel {
    #[serde(rename = "next_close")]
    NextClose,

    #[serde(rename = "next_open")]
    NextOpen,

    Vwap,
}
