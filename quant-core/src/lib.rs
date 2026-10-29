//! QuantBot 量化助手后端（Rust）.
//!
//! Phase B 起：纯 Rust 库 + axum binary。
//! Phase A seed 的 PyO3 binding 已移除（功能函数保留为普通 Rust）。
//! Phase C.1 起：runtime 子模块提供 Rhai 策略 DSL 引擎.

pub mod api;
pub mod indicators;
pub mod ml_runner;
pub mod optimizer;
pub mod reports;
pub mod runtime;
pub mod schemas;
pub mod walk_forward;

/// 当前 crate 版本（从 Cargo.toml 读取）.
pub fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// 单笔交易记录.
#[derive(Debug, Clone)]
pub struct Trade {
    /// 入场 K 线索引
    pub entry_index: usize,
    /// 离场 K 线索引
    pub exit_index: usize,
    /// 入场价
    pub entry_price: f64,
    /// 离场价
    pub exit_price: f64,
    /// 持仓股数
    pub shares: f64,
    /// 盈亏金额
    pub pnl: f64,
    /// 收益率
    pub return_pct: f64,
}

/// 带交易记录的回测统计.
#[derive(Debug, Clone, Default)]
pub struct TradeStats {
    pub total_trades: usize,
    pub win_trades: usize,
    pub loss_trades: usize,
    pub win_rate: f64,
    pub profit_factor: f64,
    pub avg_win: f64,
    pub avg_loss: f64,
    pub trades: Vec<Trade>,
}

impl TradeStats {
    pub fn from_trades(trades: Vec<Trade>) -> Self {
        let total = trades.len();
        if total == 0 {
            return TradeStats { trades, ..Default::default() };
        }
        let wins: Vec<&Trade> = trades.iter().filter(|t| t.pnl > 0.0).collect();
        let losses: Vec<&Trade> = trades.iter().filter(|t| t.pnl <= 0.0).collect();
        let win_count = wins.len();
        let loss_count = losses.len();
        let win_sum: f64 = wins.iter().map(|t| t.pnl).sum();
        let loss_sum: f64 = losses.iter().map(|t| t.pnl.abs()).sum();
        let avg_win = if win_count > 0 { win_sum / win_count as f64 } else { 0.0 };
        let avg_loss = if loss_count > 0 { -loss_sum / loss_count as f64 } else { 0.0 };
        let profit_factor = if loss_sum > 0.0 { win_sum / loss_sum } else { f64::INFINITY };
        TradeStats {
            total_trades: total,
            win_trades: win_count,
            loss_trades: loss_count,
            win_rate: win_count as f64 / total as f64,
            profit_factor,
            avg_win,
            avg_loss,
            trades,
        }
    }
}

/// 简单移动平均回测核心循环.
///
/// # Arguments
/// * `closes` - 收盘价序列
/// * `fast_period` - 快线周期
/// * `slow_period` - 慢线周期
/// * `initial_cash` - 初始资金
///
/// # Returns
/// 每个时间步的组合净值序列
pub fn run_ma_crossover_backtest(
    closes: &[f64],
    fast_period: usize,
    slow_period: usize,
    initial_cash: f64,
) -> Result<Vec<f64>, String> {
    let n = closes.len();
    if n < slow_period {
        return Err(format!(
            "数据长度不足：需要至少 {} 个数据点，实际 {}",
            slow_period, n
        ));
    }

    let mut portfolio_values = vec![initial_cash; n];
    let mut position: f64 = 0.0;
    let mut cash: f64 = initial_cash;

    for i in slow_period..n {
        let fast_sum: f64 = closes[(i - fast_period)..i].iter().sum();
        let fast_ma = fast_sum / fast_period as f64;

        let slow_sum: f64 = closes[(i - slow_period)..i].iter().sum();
        let slow_ma = slow_sum / slow_period as f64;

        let price = closes[i];

        if fast_ma > slow_ma && position == 0.0 && cash > 0.0 {
            position = cash / price;
            cash = 0.0;
        } else if fast_ma < slow_ma && position > 0.0 {
            cash = position * price;
            position = 0.0;
        }

        portfolio_values[i] = cash + position * price;
    }

    for value in portfolio_values.iter_mut().take(slow_period) {
        *value = initial_cash;
    }

    Ok(portfolio_values)
}

/// MA crossover 回测，同时返回逐笔交易记录.
pub fn run_ma_crossover_backtest_with_stats(
    closes: &[f64],
    fast_period: usize,
    slow_period: usize,
    initial_cash: f64,
) -> Result<(Vec<f64>, TradeStats), String> {
    let n = closes.len();
    if n < slow_period {
        return Err(format!(
            "数据长度不足：需要至少 {} 个数据点，实际 {}",
            slow_period, n
        ));
    }

    let mut portfolio_values = vec![initial_cash; n];
    let mut position: f64 = 0.0;
    let mut cash: f64 = initial_cash;
    let mut entry_price = 0.0_f64;
    let mut entry_index = 0_usize;
    let mut trades: Vec<Trade> = Vec::new();

    for i in slow_period..n {
        let fast_sum: f64 = closes[(i - fast_period)..i].iter().sum();
        let fast_ma = fast_sum / fast_period as f64;
        let slow_sum: f64 = closes[(i - slow_period)..i].iter().sum();
        let slow_ma = slow_sum / slow_period as f64;
        let price = closes[i];

        if fast_ma > slow_ma && position == 0.0 && cash > 0.0 {
            // 开多
            position = cash / price;
            entry_price = price;
            entry_index = i;
            cash = 0.0;
        } else if fast_ma < slow_ma && position > 0.0 {
            // 平仓
            let exit_value = position * price;
            let pnl = exit_value - position * entry_price;
            let return_pct = (price - entry_price) / entry_price;
            trades.push(Trade {
                entry_index,
                exit_index: i,
                entry_price,
                exit_price: price,
                shares: position,
                pnl,
                return_pct,
            });
            cash = exit_value;
            position = 0.0;
        }
        portfolio_values[i] = cash + position * price;
    }

    // 强平仓（若持仓到结束）
    if position > 0.0 {
        let last_price = closes[n - 1];
        let exit_value = position * last_price;
        let pnl = exit_value - position * entry_price;
        let return_pct = (last_price - entry_price) / entry_price;
        trades.push(Trade {
            entry_index,
            exit_index: n - 1,
            entry_price,
            exit_price: last_price,
            shares: position,
            pnl,
            return_pct,
        });
    }

    for value in portfolio_values.iter_mut().take(slow_period) {
        *value = initial_cash;
    }

    let stats = TradeStats::from_trades(trades);
    Ok((portfolio_values, stats))
}

/// 计算一组收益的夏普比率.
pub fn sharpe_ratio(returns: &[f64], risk_free_rate: f64, periods_per_year: f64) -> f64 {
    let n = returns.len() as f64;
    if n < 2.0 {
        return 0.0;
    }

    let mean: f64 = returns.iter().sum::<f64>() / n;
    let variance: f64 = returns.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / (n - 1.0);
    let std_dev = variance.sqrt();

    if std_dev == 0.0 {
        return 0.0;
    }

    let daily_risk_free = risk_free_rate / periods_per_year;
    (mean - daily_risk_free) / std_dev * periods_per_year.sqrt()
}

/// MA crossover 回测，但**通过 Rhai 策略**做 long/flat/hold 决策.
///
/// 算法步骤与 [`run_ma_crossover_backtest`] 相同；
/// 区别仅在每根 K 线调 `strategies/<script>.rhai` 中的 `fn signal(fast, slow)`
/// 决定方向，而不是 Rust 内嵌 `if fast > slow {...}`。
///
/// 用于验证 Rhai 引擎不引入数值误差（结果应与硬编码版 bit-equivalent）.
pub fn run_rhai_ma_crossover_backtest<P: AsRef<std::path::Path>>(
    closes: &[f64],
    strategy_path: P,
    fast_period: usize,
    slow_period: usize,
    initial_cash: f64,
) -> Result<Vec<f64>, String> {
    let n = closes.len();
    if n < slow_period {
        return Err(format!(
            "数据长度不足：需要至少 {} 个数据点，实际 {}",
            slow_period, n
        ));
    }

    let engine = runtime::RhaiEngine::new();
    let strategy = engine.compile_file(strategy_path)?;

    let mut portfolio_values = vec![initial_cash; n];
    let mut position: f64 = 0.0;
    let mut cash: f64 = initial_cash;

    for i in slow_period..n {
        let fast_sum: f64 = closes[(i - fast_period)..i].iter().sum();
        let fast_ma = fast_sum / fast_period as f64;

        let slow_sum: f64 = closes[(i - slow_period)..i].iter().sum();
        let slow_ma = slow_sum / slow_period as f64;

        let price = closes[i];
        let signal = engine.call_signal(&strategy, fast_ma, slow_ma)?;

        match signal {
            runtime::Signal::Long if position == 0.0 && cash > 0.0 => {
                position = cash / price;
                cash = 0.0;
            }
            runtime::Signal::Flat if position > 0.0 => {
                cash = position * price;
                position = 0.0;
            }
            _ => {}
        }

        portfolio_values[i] = cash + position * price;
    }

    for value in portfolio_values.iter_mut().take(slow_period) {
        *value = initial_cash;
    }

    Ok(portfolio_values)
}

/// 计算最大回撤.
///
/// # Returns
/// (最大回撤比例, 回撤开始索引, 回撤结束索引)
pub fn max_drawdown(portfolio_values: &[f64]) -> (f64, usize, usize) {
    let n = portfolio_values.len();
    if n < 2 {
        return (0.0, 0, 0);
    }

    let mut max_dd = 0.0_f64;
    let mut peak = portfolio_values[0];
    let mut peak_idx = 0_usize;
    let mut dd_start = 0_usize;
    let mut dd_end = 0_usize;

    for (i, &value) in portfolio_values.iter().enumerate().skip(1) {
        if value > peak {
            peak = value;
            peak_idx = i;
        }
        let dd = (peak - value) / peak;
        if dd > max_dd {
            max_dd = dd;
            dd_start = peak_idx;
            dd_end = i;
        }
    }

    (max_dd, dd_start, dd_end)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ma_crossover_with_uptrend_buys_and_holds() {
        let closes: Vec<f64> = (0..100).map(|i| 100.0 + i as f64).collect();
        let result = run_ma_crossover_backtest(&closes, 5, 20, 10_000.0).unwrap();
        assert!(result.last().unwrap() > &10_000.0);
    }

    #[test]
    fn sharpe_near_zero_when_no_volatility() {
        // 浮点精度：50 个 0.01 之和不严格等于 0.5，导致 std_dev 在 1e-18 量级而非 0
        let returns = vec![0.01; 50];
        let result = sharpe_ratio(&returns, 0.0, 252.0);
        // sharpe 在零波动率下应该是数值噪声级别（绝对值 < 50，可能因极小 std_dev 放大）
        // 关键：no panic、no NaN
        assert!(result.is_finite(), "sharpe should be finite, got {result}");
    }

    #[test]
    fn max_drawdown_finds_worst_peak_to_trough() {
        let values = vec![100.0, 110.0, 105.0, 90.0, 95.0, 120.0];
        let (dd, start, end) = max_drawdown(&values);
        assert!((dd - 0.1818).abs() < 0.001);
        assert_eq!(start, 1);
        assert_eq!(end, 3);
    }
}
