//! 回测绩效报告生成.
//!
//! 从 equity curve 计算一套完整的绩效指标，等价于 Python
//! `quantbot_quant.backtest.metrics.calculate_metrics`。
//!
//! ## 指标对照
//! | Rust 字段        | Python 字段           | 算法                                    |
//! |------------------|-----------------------|----------------------------------------|
//! | total_return     | total_return          | (final - initial) / initial            |
//! | annual_return    | annual_return (CAGR)  | (1+total_return)^(252/n) - 1           |
//! | volatility       | volatility            | std_dev(daily_returns) * sqrt(252)     |
//! | sharpe_ratio     | sharpe_ratio          | (mean - rf/252) / std * sqrt(252)      |
//! | sortino_ratio    | sortino_ratio         | (mean - rf/252) / downside_std * sqrt  |
//! | max_drawdown     | max_drawdown          | max peak-to-trough / peak              |
//! | calmar_ratio     | calmar_ratio          | annual_return / max_drawdown           |

use crate::{max_drawdown, sharpe_ratio};

// ── 数据结构 ──────────────────────────────────────────────────────────────────

/// 回测绩效报告（全套指标）.
#[derive(Debug, Clone)]
pub struct BacktestReport {
    // 收益类
    pub total_return: f64,
    pub annual_return: f64,  // CAGR
    // 风险类
    pub max_drawdown: f64,
    pub max_drawdown_start: usize,
    pub max_drawdown_end: usize,
    pub volatility: f64,     // 年化波动率
    // 风险调整收益
    pub sharpe_ratio: f64,
    pub sortino_ratio: f64,
    pub calmar_ratio: f64,
    // 统计
    pub total_bars: usize,
}

// ── 计算函数 ──────────────────────────────────────────────────────────────────

/// 从 equity curve 计算完整绩效报告.
///
/// 与 Python `calculate_metrics` 逐项对齐：
/// - `annual_return`：CAGR = (1 + total_return)^(periods_per_year / n) - 1
/// - `volatility`：std_dev(returns) × sqrt(periods_per_year)
/// - `sortino_ratio`：使用**下行标准差**（仅负收益的 std），而非总 std
/// - `calmar_ratio`：annual_return / max_drawdown（max_drawdown 为正值）
pub fn calculate_report(
    equity_curve: &[f64],
    initial_cash: f64,
    risk_free_rate: f64,
    periods_per_year: f64,
) -> BacktestReport {
    let n = equity_curve.len();
    let total_bars = n;

    if n < 2 || initial_cash <= 0.0 {
        return BacktestReport {
            total_return: 0.0,
            annual_return: 0.0,
            max_drawdown: 0.0,
            max_drawdown_start: 0,
            max_drawdown_end: 0,
            volatility: 0.0,
            sharpe_ratio: 0.0,
            sortino_ratio: 0.0,
            calmar_ratio: 0.0,
            total_bars,
        };
    }

    // ── 日收益率序列 ────────────────────────────────────────────────────────
    let returns: Vec<f64> = (1..n)
        .map(|i| (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])
        .collect();

    // ── 总收益 & CAGR ───────────────────────────────────────────────────────
    let total_return = (equity_curve[n - 1] - initial_cash) / initial_cash;
    let years = (n - 1) as f64 / periods_per_year;  // n-1: return count = bar count - 1
    let annual_return = if years > 0.0 {
        (1.0 + total_return).powf(1.0 / years) - 1.0
    } else {
        0.0
    };

    // ── 波动率（年化） ───────────────────────────────────────────────────────
    let m = returns.len() as f64;
    let mean_ret: f64 = returns.iter().sum::<f64>() / m;
    let variance: f64 = returns.iter().map(|r| (r - mean_ret).powi(2)).sum::<f64>() / (m - 1.0);
    let std_dev = variance.sqrt();
    let volatility = std_dev * periods_per_year.sqrt();

    // ── Sharpe ──────────────────────────────────────────────────────────────
    let sharpe = sharpe_ratio(&returns, risk_free_rate, periods_per_year);

    // ── Sortino（仅负收益参与下行 std）───────────────────────────────────────
    let daily_rf = risk_free_rate / periods_per_year;
    let downside_sq_sum: f64 = returns
        .iter()
        .filter(|&&r| r < daily_rf)
        .map(|&r| (r - daily_rf).powi(2))
        .sum();
    let n_down = returns.iter().filter(|&&r| r < daily_rf).count() as f64;
    let sortino = if n_down > 0.0 {
        let downside_std = (downside_sq_sum / n_down).sqrt();
        if downside_std == 0.0 {
            0.0
        } else {
            (mean_ret - daily_rf) / downside_std * periods_per_year.sqrt()
        }
    } else {
        // No downside returns → infinite sortino (cap at 0 for missing data)
        f64::INFINITY
    };

    // ── Max Drawdown ────────────────────────────────────────────────────────
    let (max_dd, dd_start, dd_end) = max_drawdown(equity_curve);

    // ── Calmar Ratio ────────────────────────────────────────────────────────
    let calmar = if max_dd > 0.0 {
        annual_return / max_dd
    } else {
        0.0
    };

    BacktestReport {
        total_return,
        annual_return,
        max_drawdown: max_dd,
        max_drawdown_start: dd_start,
        max_drawdown_end: dd_end,
        volatility,
        sharpe_ratio: sharpe,
        sortino_ratio: sortino,
        calmar_ratio: calmar,
        total_bars,
    }
}

/// 将报告格式化为可读文本摘要.
pub fn format_report_text(report: &BacktestReport) -> String {
    format!(
        "=== Backtest Report ===\n\
         Total Return:   {:.2}%\n\
         Annual Return:  {:.2}%\n\
         Max Drawdown:   {:.2}%\n\
         Volatility:     {:.2}%\n\
         Sharpe Ratio:   {:.3}\n\
         Sortino Ratio:  {:.3}\n\
         Calmar Ratio:   {:.3}\n\
         Total Bars:     {}\n",
        report.total_return * 100.0,
        report.annual_return * 100.0,
        report.max_drawdown * 100.0,
        report.volatility * 100.0,
        report.sharpe_ratio,
        report.sortino_ratio,
        report.calmar_ratio,
        report.total_bars,
    )
}

// ── 单元测试 ──────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn report_total_return_correct() {
        let equity = vec![10_000.0, 11_000.0];
        let report = calculate_report(&equity, 10_000.0, 0.0, 252.0);
        assert!((report.total_return - 0.1).abs() < 1e-12);
    }

    #[test]
    fn report_flat_equity_zero_sharpe() {
        let equity = vec![10_000.0; 50];
        let report = calculate_report(&equity, 10_000.0, 0.0, 252.0);
        assert_eq!(report.sharpe_ratio, 0.0);
        assert_eq!(report.max_drawdown, 0.0);
    }

    #[test]
    fn format_report_text_contains_sharpe() {
        let equity: Vec<f64> = (0..30).map(|i| 10_000.0 + i as f64 * 100.0).collect();
        let report = calculate_report(&equity, 10_000.0, 0.0, 252.0);
        let text = format_report_text(&report);
        assert!(text.contains("Sharpe"));
        assert!(text.contains("Total Return"));
    }
}
