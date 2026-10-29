//! POST /api/ml/predict — ONNX 模型推理端点.
//!
//! 从 `common/data-store/models/<model_id>/` 加载模型并执行单样本推理。
//! 模型目录约定：
//!   - `meta.json`：模型元数据
//!   - `model.onnx`：ONNX 模型文件

use std::path::PathBuf;

use axum::{http::StatusCode, Json};
use serde::{Deserialize, Serialize};

use super::ApiError;
use crate::ml_runner::MlRunner;

// ── 请求 / 响应结构 ───────────────────────────────────────────────────────────

#[derive(Deserialize)]
pub struct MlPredictRequest {
    /// 模型 ID，对应 `common/data-store/models/<model_id>/` 目录名.
    pub model_id: String,
    /// 输入特征向量（顺序与 meta.json features[] 一致）.
    pub features: Vec<f64>,
}

#[derive(Serialize)]
pub struct MlPredictResponse {
    /// 回显请求中的 model_id.
    pub model_id: String,
    /// 实际使用的特征数量.
    pub n_features: usize,
    /// 模型输出向量（通常长度为 1 或 n_classes）.
    pub predictions: Vec<f64>,
}

// ── 路径工具 ──────────────────────────────────────────────────────────────────

/// 返回模型基目录（compile-time 相对路径）.
/// 结构：`common/data-store/models/`
fn models_base_dir() -> PathBuf {
    if let Some(path) = std::env::var_os("QUANTBOT_MODEL_DIR") {
        return PathBuf::from(path);
    }
    let mut root = std::env::current_dir().unwrap_or_default();
    // Cargo tests start in the crate; quantbot core starts in the project root.
    if root.file_name().is_some_and(|name| name == "quant-core") {
        root.pop();
    }
    root.join("shared/fixtures/models")
}

// ── 处理器 ────────────────────────────────────────────────────────────────────

pub async fn predict(
    Json(req): Json<MlPredictRequest>,
) -> Result<Json<MlPredictResponse>, ApiError> {
    // 路径遍历防护：model_id 不得含 '/' 或 '..'
    if req.model_id.contains('/') || req.model_id.contains("..") || req.model_id.is_empty() {
        return Err(ApiError(
            StatusCode::BAD_REQUEST,
            "model_id 不合法（不能含 '/' 或 '..'）".into(),
        ));
    }

    let model_dir = models_base_dir().join(&req.model_id);

    let runner =
        MlRunner::load(&model_dir).map_err(|e| ApiError(StatusCode::BAD_REQUEST, e))?;

    let n_features = req.features.len();
    let predictions = runner
        .predict(&req.features)
        .map_err(|e| ApiError(StatusCode::BAD_REQUEST, e))?;

    Ok(Json(MlPredictResponse {
        model_id: req.model_id,
        n_features,
        predictions,
    }))
}

// ── 测试 ──────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{body::to_bytes, http::Request};
    use tower::ServiceExt;

    fn make_router() -> axum::Router {
        axum::Router::new().route("/api/ml/predict", axum::routing::post(predict))
    }

    #[tokio::test]
    async fn ml_predict_test_linear_returns_correct_output() {
        let router = make_router();

        let body = serde_json::json!({
            "model_id": "test_linear",
            "features": [1.0, 2.0],
        });

        let response = router
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/ml/predict")
                    .header("Content-Type", "application/json")
                    .body(axum::body::Body::from(serde_json::to_string(&body).unwrap()))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);

        let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&bytes).unwrap();

        assert_eq!(json["model_id"], "test_linear");
        assert_eq!(json["n_features"], 2);

        let predictions = json["predictions"].as_array().unwrap();
        assert_eq!(predictions.len(), 1);

        let expected = 0.5_f64 * 1.0 + 0.3 * 2.0 + 0.1; // = 1.2
        let actual = predictions[0].as_f64().unwrap();
        let diff = (actual - expected).abs();
        assert!(
            diff < 1e-5,
            "prediction {actual:.6} vs expected {expected:.6} (diff {diff:.3e})"
        );
    }

    #[tokio::test]
    async fn ml_predict_rejects_path_traversal() {
        let router = make_router();

        let body = serde_json::json!({
            "model_id": "../etc/passwd",
            "features": [1.0],
        });

        let response = router
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/ml/predict")
                    .header("Content-Type", "application/json")
                    .body(axum::body::Body::from(serde_json::to_string(&body).unwrap()))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn ml_predict_rejects_unknown_model() {
        let router = make_router();

        let body = serde_json::json!({
            "model_id": "nonexistent_model_xyz",
            "features": [1.0, 2.0],
        });

        let response = router
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/ml/predict")
                    .header("Content-Type", "application/json")
                    .body(axum::body::Body::from(serde_json::to_string(&body).unwrap()))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    }
}
