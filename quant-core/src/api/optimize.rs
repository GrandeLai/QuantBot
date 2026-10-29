//! POST /api/optimize — 网格搜索参数优化端点.

use axum::{http::StatusCode, Json};

use super::ApiError;
use serde::{Deserialize, Serialize};

use crate::optimizer::{grid_search_ma_crossover, GridSearchConfig, OptimizeResult};

#[derive(Deserialize)]
pub struct OptimizeRequest {
    pub closes: Vec<f64>,
    pub initial_cash: f64,
    pub fast_periods: Vec<usize>,
    pub slow_periods: Vec<usize>,
    #[serde(default)]
    pub risk_free_rate: f64,
    #[serde(default = "default_periods")]
    pub periods_per_year: f64,
    /// 返回 Top-N 结果（0 = 全部）
    #[serde(default)]
    pub top_n: usize,
}

fn default_periods() -> f64 {
    252.0
}

#[derive(Serialize)]
pub struct OptimizeResponse {
    pub total_combinations: usize,
    pub results: Vec<OptimizeResult>,
}

pub async fn run_optimize(
    Json(req): Json<OptimizeRequest>,
) -> Result<Json<OptimizeResponse>, ApiError> {
    if req.closes.len() < 2 {
        return Err(ApiError(StatusCode::BAD_REQUEST, "closes 至少需要 2 个数据点".into()));
    }
    if req.fast_periods.is_empty() || req.slow_periods.is_empty() {
        return Err(ApiError(
            StatusCode::BAD_REQUEST,
            "fast_periods 和 slow_periods 不能为空".into(),
        ));
    }

    let config = GridSearchConfig {
        fast_periods: req.fast_periods,
        slow_periods: req.slow_periods,
    };

    let mut results = grid_search_ma_crossover(
        &req.closes,
        req.initial_cash,
        &config,
        req.risk_free_rate,
        req.periods_per_year,
    );

    let total = results.len();
    if req.top_n > 0 {
        results.truncate(req.top_n);
    }

    Ok(Json(OptimizeResponse {
        total_combinations: total,
        results,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{body::to_bytes, http::Request};
    use tower::ServiceExt;

    #[tokio::test]
    async fn optimize_returns_sorted_results() {
        let router = axum::Router::new()
            .route("/api/optimize", axum::routing::post(run_optimize));

        let closes: Vec<f64> = (0..50).map(|i| 100.0 + i as f64 * 0.5).collect();
        let body = serde_json::json!({
            "closes": closes,
            "initial_cash": 10000.0,
            "fast_periods": [3, 5, 7],
            "slow_periods": [10, 15],
            "top_n": 3,
        });

        let response = router
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/optimize")
                    .header("Content-Type", "application/json")
                    .body(axum::body::Body::from(serde_json::to_string(&body).unwrap()))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
        let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert!(json["total_combinations"].as_u64().unwrap() > 0);
        let results = json["results"].as_array().unwrap();
        assert!(results.len() <= 3);
        // 前端按 sharpe 降序
        if results.len() >= 2 {
            let s0 = results[0]["sharpe_ratio"].as_f64().unwrap();
            let s1 = results[1]["sharpe_ratio"].as_f64().unwrap();
            assert!(s0 >= s1, "results should be sorted by sharpe desc");
        }
    }
}
