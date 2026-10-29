// Example code that deserializes and serializes the model.
// extern crate serde;
// #[macro_use]
// extern crate serde_derive;
// extern crate serde_json;
//
// use generated_module::Factor;
//
// fn main() {
//     let json = r#"{"answer": 42}"#;
//     let model: Factor = serde_json::from_str(&json).unwrap();
// }

use serde::{Serialize, Deserialize};

/// Single computed factor value at a point in time.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Factor {
    /// Factor identifier, e.g. 'sma_20', 'rsi_14'
    pub factor_id: String,

    pub symbol: String,

    pub timestamp: String,

    /// Computed factor value (NaN encoded as JSON null)
    pub value: f64,

    /// Factor implementation version, optional
    pub version: Option<String>,
}
