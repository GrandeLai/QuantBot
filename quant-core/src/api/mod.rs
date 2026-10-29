//! HTTP API 路由模块.
//!
//! 每个子模块对应一组相关端点：
//! - `backtest`：MA crossover 回测（含 trade 统计）
//! - `indicators`：SMA/EMA 指标计算
//! - `optimize`：网格搜索参数优化
//! - `walk_forward`：Walk-Forward CV

pub mod backtest;
pub mod indicators;
pub mod ml_predict;
pub mod optimize;
pub mod walk_forward;

use axum::{http::StatusCode, response::IntoResponse, routing::post, Json, Router};

// ── 共享错误类型 ──────────────────────────────────────────────────────────────

/// 统一 HTTP API 错误类型.
/// 构造方式：`ApiError(StatusCode::BAD_REQUEST, "描述".into())`
pub struct ApiError(pub StatusCode, pub String);

impl IntoResponse for ApiError {
    fn into_response(self) -> axum::response::Response {
        (self.0, Json(serde_json::json!({"error": self.1}))).into_response()
    }
}

// ── 路由 ──────────────────────────────────────────────────────────────────────

/// 构建所有 API 路由，挂载在 `/api` 前缀下.
pub fn router() -> Router {
    Router::new()
        .route("/api/backtest/run", post(backtest::run_backtest))
        .route("/api/walk-forward", post(walk_forward::run_walk_forward))
        .route("/api/optimize", post(optimize::run_optimize))
        .route("/api/indicators", post(indicators::compute_indicators))
        .route("/api/ml/predict", post(ml_predict::predict))
}
