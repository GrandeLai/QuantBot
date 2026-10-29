//! POST /api/backtest/run — MA crossover 回测端点.

use axum::{http::StatusCode, Json};

use super::ApiError;
use serde::{Deserialize, Serialize};

use crate::{
    reports::calculate_report,
    run_ma_crossover_backtest_with_stats,
};

// ── 请求 / 响应结构 ───────────────────────────────────────────────────────────

/// 输入 bar（最小化：只要 close；time 可选用于日期信息）.
#[derive(Deserialize)]
pub struct Bar {
    pub close: f64,
    /// ISO 8601 时间戳（可选，用于 start_date/end_date）
    pub time: Option<String>,
}

#[derive(Deserialize)]
pub struct BacktestRunRequest {
    pub symbol: String,
    /// OHLCV bars 序列（至少需要 `close` 字段）
    pub bars: Vec<Bar>,
    /// MA 策略参数
    pub fast_period: usize,
    pub slow_period: usize,
    pub initial_cash: f64,
    #[serde(default)]
    pub risk_free_rate: f64,
    #[serde(default = "default_periods")]
    pub periods_per_year: f64,
}

fn default_periods() -> f64 {
    252.0
}

#[derive(Serialize)]
pub struct BacktestMetrics {
    pub total_return: f64,
    pub annual_return: f64,
    pub max_drawdown: f64,
    pub volatility: f64,
    pub sharpe_ratio: f64,
    pub sortino_ratio: f64,
    pub calmar_ratio: f64,
    pub win_rate: f64,
    pub profit_factor: f64,
    pub total_trades: usize,
    pub win_trades: usize,
    pub loss_trades: usize,
    pub avg_win: f64,
    pub avg_loss: f64,
    pub initial_cash: f64,
    pub final_value: f64,
    pub start_date: Option<String>,
    pub end_date: Option<String>,
    pub trading_days: usize,
}

#[derive(Serialize)]
pub struct BacktestRunResponse {
    pub symbol: String,
    pub fast_period: usize,
    pub slow_period: usize,
    pub bars_processed: usize,
    pub equity_curve: Vec<f64>,
    pub metrics: BacktestMetrics,
}

// ── 处理器 ────────────────────────────────────────────────────────────────────

pub async fn run_backtest(
    Json(req): Json<BacktestRunRequest>,
) -> Result<Json<BacktestRunResponse>, ApiError> {
    if req.bars.len() < 2 {
        return Err(ApiError(StatusCode::BAD_REQUEST, "bars 至少需要 2 根".into()));
    }

    let closes: Vec<f64> = req.bars.iter().map(|b| b.close).collect();
    let start_date = req.bars.first().and_then(|b| b.time.clone());
    let end_date = req.bars.last().and_then(|b| b.time.clone());

    let (equity_curve, stats) = run_ma_crossover_backtest_with_stats(
        &closes,
        req.fast_period,
        req.slow_period,
        req.initial_cash,
    )
    .map_err(|e| ApiError(StatusCode::BAD_REQUEST, e))?;

    let report = calculate_report(
        &equity_curve,
        req.initial_cash,
        req.risk_free_rate,
        req.periods_per_year,
    );

    let final_value = equity_curve.last().copied().unwrap_or(req.initial_cash);

    Ok(Json(BacktestRunResponse {
        symbol: req.symbol,
        fast_period: req.fast_period,
        slow_period: req.slow_period,
        bars_processed: req.bars.len(),
        equity_curve,
        metrics: BacktestMetrics {
            total_return: report.total_return,
            annual_return: report.annual_return,
            max_drawdown: report.max_drawdown,
            volatility: report.volatility,
            sharpe_ratio: report.sharpe_ratio,
            sortino_ratio: if report.sortino_ratio.is_infinite() { 9999.0 } else { report.sortino_ratio },
            calmar_ratio: report.calmar_ratio,
            win_rate: stats.win_rate,
            profit_factor: if stats.profit_factor.is_infinite() { 9999.0 } else { stats.profit_factor },
            total_trades: stats.total_trades,
            win_trades: stats.win_trades,
            loss_trades: stats.loss_trades,
            avg_win: stats.avg_win,
            avg_loss: stats.avg_loss,
            initial_cash: req.initial_cash,
            final_value,
            start_date,
            end_date,
            trading_days: report.total_bars,
        },
    }))
}

// ── 单元测试 ──────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{body::to_bytes, http::Request};
    use tower::ServiceExt;

    fn make_bars(closes: Vec<f64>) -> Vec<Bar> {
        closes.into_iter().map(|c| Bar { close: c, time: None }).collect()
    }

    #[tokio::test]
    async fn backtest_run_returns_metrics() {
        let router = axum::Router::new()
            .route("/api/backtest/run", axum::routing::post(run_backtest));

        let closes: Vec<f64> = (0..30).map(|i| 100.0 + i as f64).collect();
        let bars = make_bars(closes);
        let body = serde_json::json!({
            "symbol": "TEST",
            "bars": bars.iter().map(|b| serde_json::json!({"close": b.close})).collect::<Vec<_>>(),
            "fast_period": 3,
            "slow_period": 7,
            "initial_cash": 10000.0,
        });

        let response = router
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/backtest/run")
                    .header("Content-Type", "application/json")
                    .body(axum::body::Body::from(serde_json::to_string(&body).unwrap()))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
        let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert!(json["metrics"]["total_return"].is_number());
        assert!(json["metrics"]["sharpe_ratio"].is_number());
        assert!(json["metrics"]["win_rate"].is_number());
        assert!(json["metrics"]["total_trades"].is_number());
        assert!(json["bars_processed"].as_u64().unwrap() == 30);
    }

    #[tokio::test]
    async fn backtest_run_rejects_too_few_bars() {
        let router = axum::Router::new()
            .route("/api/backtest/run", axum::routing::post(run_backtest));

        let body = serde_json::json!({
            "symbol": "X",
            "bars": [{"close": 100.0}],
            "fast_period": 3,
            "slow_period": 7,
            "initial_cash": 1000.0,
        });

        let response = router
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/backtest/run")
                    .header("Content-Type", "application/json")
                    .body(axum::body::Body::from(serde_json::to_string(&body).unwrap()))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    }
}
