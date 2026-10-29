//! QuantBot Rust quant-assistant 服务入口（axum）.
//!
//! 路由结构：
//! - GET  /          → 版本 + 端点列表
//! - GET  /healthz   → 健康检查
//! - POST /api/backtest/run    → MA crossover 回测（含 trade 统计）
//! - POST /api/walk-forward    → Walk-Forward CV
//! - POST /api/optimize        → 网格搜索参数优化
//! - POST /api/indicators      → SMA/EMA 指标计算
//! - POST /api/ml/predict      → ONNX 模型推理

use std::net::SocketAddr;

use anyhow::Result;
use axum::{routing::get, Json, Router};
use chrono::Utc;
use serde::Serialize;
use tower_http::cors::CorsLayer;
use tower_http::trace::TraceLayer;
use tracing::info;
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

// ── 全局端点 ──────────────────────────────────────────────────────────────────

#[derive(Serialize)]
struct HealthResponse {
    status: &'static str,
    service: &'static str,
    version: &'static str,
    started_at: String,
}

async fn healthz() -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok",
        service: "quant-assistant",
        version: quantbot_quant::version(),
        started_at: Utc::now().to_rfc3339(),
    })
}

async fn root() -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "message": "QuantBot Quant Assistant (Rust)",
        "version": quantbot_quant::version(),
        "endpoints": [
            "GET  /healthz",
            "POST /api/backtest/run",
            "POST /api/walk-forward",
            "POST /api/optimize",
            "POST /api/indicators",
            "POST /api/ml/predict",
        ],
    }))
}

// ── 应用构建 ──────────────────────────────────────────────────────────────────

pub fn build_app() -> Router {
    Router::new()
        .route("/", get(root))
        .route("/healthz", get(healthz))
        .merge(quantbot_quant::api::router())
        .layer(CorsLayer::permissive())
        .layer(TraceLayer::new_for_http())
}

// ── 启动 ──────────────────────────────────────────────────────────────────────

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,tower_http=debug".into()),
        )
        .with(tracing_subscriber::fmt::layer())
        .init();

    let port: u16 = std::env::var("QUANTBOT_QUANT_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8002);

    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let app = build_app();

    info!("QuantBot Rust quant-assistant listening on http://{addr}");

    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;

    Ok(())
}

// ── 测试 ──────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt;

    #[tokio::test]
    async fn healthz_returns_ok_status() {
        let app = build_app();
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/healthz")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let body_str = std::str::from_utf8(&body).unwrap();
        assert!(body_str.contains("\"status\":\"ok\""));
        assert!(body_str.contains("\"service\":\"quant-assistant\""));
    }

    #[tokio::test]
    async fn root_lists_all_endpoints() {
        let app = build_app();
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let body_str = std::str::from_utf8(&body).unwrap();
        assert!(body_str.contains("/api/backtest/run"));
        assert!(body_str.contains("/api/walk-forward"));
        assert!(body_str.contains("/api/optimize"));
        assert!(body_str.contains("/api/indicators"));
        assert!(body_str.contains("/api/ml/predict"));
    }
}
