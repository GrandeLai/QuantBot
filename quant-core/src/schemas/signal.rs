// Example code that deserializes and serializes the model.
// extern crate serde;
// #[macro_use]
// extern crate serde_derive;
// extern crate serde_json;
//
// use generated_module::Signal;
//
// fn main() {
//     let json = r#"{"answer": 42}"#;
//     let model: Signal = serde_json::from_str(&json).unwrap();
// }

use serde::{Serialize, Deserialize};

/// Strategy-generated trading signal.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Signal {
    /// Direction the strategy proposes
    pub action: Action,

    /// Human-readable explanation, optional
    pub reason: Option<String>,

    /// Unique signal identifier (UUID or deterministic hash)
    pub signal_id: String,

    /// Strategy id or model id that produced this signal
    pub source: Option<String>,

    /// Confidence/sizing weight in [0, 1]
    pub strength: Option<f64>,

    pub symbol: String,

    pub timestamp: String,
}

/// Direction the strategy proposes
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Action {
    Flat,

    Hold,

    Long,

    Short,
}
