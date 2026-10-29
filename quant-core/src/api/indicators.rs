//! POST /api/indicators — SMA/EMA 指标计算端点.

use axum::{http::StatusCode, Json};

use super::ApiError;
use serde::{Deserialize, Serialize};

use crate::indicators::{ema, sma};

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum IndicatorType {
    Sma,
    Ema,
}

#[derive(Deserialize)]
pub struct IndicatorsRequest {
    pub closes: Vec<f64>,
    pub indicator: IndicatorType,
    pub period: usize,
}

#[derive(Serialize)]
pub struct IndicatorsResponse {
    pub indicator: String,
    pub period: usize,
    /// None 表示热身期（数据不足）
    pub values: Vec<Option<f64>>,
}

pub async fn compute_indicators(
    Json(req): Json<IndicatorsRequest>,
) -> Result<Json<IndicatorsResponse>, ApiError> {
    if req.closes.is_empty() {
        return Err(ApiError(StatusCode::BAD_REQUEST, "closes 不能为空".into()));
    }
    if req.period == 0 {
        return Err(ApiError(StatusCode::BAD_REQUEST, "period 必须 ≥ 1".into()));
    }

    let (indicator_name, values) = match req.indicator {
        IndicatorType::Sma => ("sma".to_string(), sma(&req.closes, req.period)),
        IndicatorType::Ema => ("ema".to_string(), ema(&req.closes, req.period)),
    };

    Ok(Json(IndicatorsResponse {
        indicator: indicator_name,
        period: req.period,
        values,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{body::to_bytes, http::Request};
    use tower::ServiceExt;

    #[tokio::test]
    async fn indicators_sma_returns_values() {
        let router = axum::Router::new()
            .route("/api/indicators", axum::routing::post(compute_indicators));

        let closes: Vec<f64> = (0..10).map(|i| 100.0 + i as f64).collect();
        let body = serde_json::json!({
            "closes": closes,
            "indicator": "sma",
            "period": 3,
        });

        let response = router
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/indicators")
                    .header("Content-Type", "application/json")
                    .body(axum::body::Body::from(serde_json::to_string(&body).unwrap()))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
        let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(json["indicator"], "sma");
        assert_eq!(json["period"], 3);
        let values = json["values"].as_array().unwrap();
        assert_eq!(values.len(), 10);
        // 前两个为 null（热身期）
        assert!(values[0].is_null());
        assert!(values[1].is_null());
        // 第三个有值
        assert!(values[2].is_number());
    }

    #[tokio::test]
    async fn indicators_ema_returns_values() {
        let router = axum::Router::new()
            .route("/api/indicators", axum::routing::post(compute_indicators));

        let closes: Vec<f64> = (0..10).map(|i| 100.0 + i as f64).collect();
        let body = serde_json::json!({
            "closes": closes,
            "indicator": "ema",
            "period": 3,
        });

        let response = router
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/indicators")
                    .header("Content-Type", "application/json")
                    .body(axum::body::Body::from(serde_json::to_string(&body).unwrap()))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
        let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(json["indicator"], "ema");
        let values = json["values"].as_array().unwrap();
        assert_eq!(values.len(), 10);
    }
}
