//! 策略参数优化：网格搜索.
//!
//! 对每个 (fast_period, slow_period) 组合跑 MA crossover 回测，
//! 按 Sharpe 降序返回所有有效结果。
//!
//! 等价于 Python `quantbot_quant.optimize.engine.OptimizationEngine.grid_search`。

use serde::Serialize;

use crate::{max_drawdown, run_ma_crossover_backtest, sharpe_ratio};

/// 网格搜索参数配置.
#[derive(Debug, Clone)]
pub struct GridSearchConfig {
    /// 快线周期列表.
    pub fast_periods: Vec<usize>,
    /// 慢线周期列表.
    pub slow_periods: Vec<usize>,
}

/// 单次参数组合的回测优化结果.
#[derive(Debug, Clone, Serialize)]
pub struct OptimizeResult {
    pub fast_period: usize,
    pub slow_period: usize,
    pub sharpe_ratio: f64,
    pub total_return: f64,
    pub max_drawdown: f64,
}

/// MA crossover 网格搜索.
///
/// 枚举所有 (fast, slow) 组合（仅 fast < slow），对每组运行回测，
/// 返回按 Sharpe 降序的结果列表。
///
/// **失败的组合**（数据不足、fast >= slow）被跳过，不出现在结果中。
///
/// # Arguments
/// * `closes` - 收盘价序列
/// * `initial_cash` - 初始资金
/// * `config` - 参数网格
/// * `risk_free_rate` - 年化无风险利率（用于 Sharpe 计算）
/// * `periods_per_year` - 年化交易周期数（日频=252，小时频=252*6.5）
pub fn grid_search_ma_crossover(
    closes: &[f64],
    initial_cash: f64,
    config: &GridSearchConfig,
    risk_free_rate: f64,
    periods_per_year: f64,
) -> Vec<OptimizeResult> {
    let mut results: Vec<OptimizeResult> = Vec::new();

    for &fast in &config.fast_periods {
        for &slow in &config.slow_periods {
            // fast >= slow 没有意义（快线必须快于慢线）
            if fast >= slow {
                continue;
            }

            let equity = match run_ma_crossover_backtest(closes, fast, slow, initial_cash) {
                Ok(v) => v,
                Err(_) => continue,
            };

            let n = equity.len();
            if n < 2 {
                continue;
            }

            // 计算日收益率（与 Python 对齐：returns = diff(equity) / equity[:-1]）
            let returns: Vec<f64> = (1..n)
                .map(|i| (equity[i] - equity[i - 1]) / equity[i - 1])
                .collect();

            let sharpe = sharpe_ratio(&returns, risk_free_rate, periods_per_year);
            let (dd, _, _) = max_drawdown(&equity);

            let total_return = if initial_cash > 0.0 {
                (equity[n - 1] - initial_cash) / initial_cash
            } else {
                0.0
            };

            results.push(OptimizeResult {
                fast_period: fast,
                slow_period: slow,
                sharpe_ratio: sharpe,
                total_return,
                max_drawdown: dd,
            });
        }
    }

    // 按 Sharpe 降序排列（与 Python OptimizationEngine.grid_search 一致）
    results.sort_by(|a, b| {
        b.sharpe_ratio
            .partial_cmp(&a.sharpe_ratio)
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    results
}

#[cfg(test)]
mod tests {
    use super::*;

    const FIXTURE_CLOSES: &[f64] = &[
        100.0, 101.0, 102.0, 103.0, 99.0, 98.0, 100.0, 105.0, 107.0, 110.0,
        112.0, 108.0, 106.0, 104.0, 102.0, 100.0, 98.0, 99.0, 101.0, 103.0,
        105.0, 107.0, 109.0, 111.0, 113.0,
    ];

    #[test]
    fn grid_search_returns_sorted_by_sharpe() {
        let config = GridSearchConfig {
            fast_periods: vec![2, 3, 5],
            slow_periods: vec![5, 7, 10],
        };
        let results = grid_search_ma_crossover(FIXTURE_CLOSES, 10_000.0, &config, 0.0, 252.0);
        assert!(!results.is_empty(), "should have some results");

        // Assert Sharpe is non-increasing
        for i in 1..results.len() {
            assert!(
                results[i - 1].sharpe_ratio >= results[i].sharpe_ratio,
                "results[{}].sharpe={} > results[{}].sharpe={}",
                i - 1, results[i - 1].sharpe_ratio,
                i, results[i].sharpe_ratio
            );
        }
    }

    #[test]
    fn grid_search_skips_invalid_combos() {
        let config = GridSearchConfig {
            fast_periods: vec![5, 10],
            slow_periods: vec![3, 7],  // 5>3, 10>3, 10>7 → invalid: (5,3), (10,3)
        };
        let results = grid_search_ma_crossover(FIXTURE_CLOSES, 10_000.0, &config, 0.0, 252.0);
        for r in &results {
            assert!(r.fast_period < r.slow_period,
                "fast={} >= slow={}", r.fast_period, r.slow_period);
        }
    }

    #[test]
    fn grid_search_all_valid_combos_present() {
        let config = GridSearchConfig {
            fast_periods: vec![2, 3],
            slow_periods: vec![5, 7],
        };
        let results = grid_search_ma_crossover(FIXTURE_CLOSES, 10_000.0, &config, 0.0, 252.0);
        // 2×2 combinations, all fast < slow → expect 4 results
        assert_eq!(results.len(), 4, "expected 4 valid combos");
    }
}
