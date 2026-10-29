// Example code that deserializes and serializes the model.
// extern crate serde;
// #[macro_use]
// extern crate serde_derive;
// extern crate serde_json;
//
// use generated_module::BacktestResult;
//
// fn main() {
//     let json = r#"{"answer": 42}"#;
//     let model: BacktestResult = serde_json::from_str(&json).unwrap();
// }

use serde::{Serialize, Deserialize};

/// Outcome of a backtest run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BacktestResult {
    /// Unique identifier for this backtest run
    pub backtest_id: String,

    /// Strategy id of the BacktestConfig used (independent reference; full config logged
    /// separately)
    pub config_strategy_id: String,

    /// Sampled equity curve points
    pub equity_curve: Vec<EquityCurve>,

    pub metrics: Metrics,

    pub ran_at: Option<String>,

    /// Executed trades, optional
    pub trades: Option<Vec<Trade>>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EquityCurve {
    pub equity: f64,

    pub timestamp: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Metrics {
    /// Max drawdown (fraction, positive)
    pub max_drawdown: f64,

    pub sharpe_ratio: f64,

    /// Cumulative return (fraction)
    pub total_return: f64,

    pub trade_count: i64,

    pub win_rate: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Trade {
    pub entry_price: f64,

    pub entry_timestamp: String,

    pub exit_price: f64,

    pub exit_timestamp: String,

    pub pnl: f64,

    pub qty: f64,

    pub side: Side,

    pub symbol: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Side {
    Long,

    Short,
}
