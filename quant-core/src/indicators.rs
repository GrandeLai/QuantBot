//! 技术指标计算库.
//!
//! 所有函数均为纯函数（无副作用），可直接在 Rhai 引擎中注册。
//! NaN 约定：Rhai 不支持 Option，因此 Rhai 版函数用 `f64::NAN` 表示 warmup 期的无效值；
//! Rust 原生版函数返回 `Vec<Option<f64>>` 保持类型安全。

/// 简单移动平均（SMA）.
///
/// 对每个索引 `i`：
/// - `i < period - 1`：`None`（warmup 期不足）
/// - `i >= period - 1`：`Some(mean(closes[i-period+1..=i]))`
///
/// # Arguments
/// * `closes` - 收盘价序列
/// * `period`  - 窗口长度（>= 1）
///
/// # Panics
/// period == 0 时 panic
///
/// # Examples
/// ```
/// use quantbot_quant::indicators::sma;
/// let v = sma(&[1.0, 2.0, 3.0, 4.0, 5.0], 3);
/// assert_eq!(v[0], None);
/// assert_eq!(v[1], None);
/// assert!((v[2].unwrap() - 2.0).abs() < 1e-12);
/// assert!((v[3].unwrap() - 3.0).abs() < 1e-12);
/// assert!((v[4].unwrap() - 4.0).abs() < 1e-12);
/// ```
pub fn sma(closes: &[f64], period: usize) -> Vec<Option<f64>> {
    assert!(period >= 1, "period must be >= 1");
    let n = closes.len();
    let mut out = vec![None; n];
    for i in (period - 1)..n {
        let sum: f64 = closes[(i + 1 - period)..=i].iter().sum();
        out[i] = Some(sum / period as f64);
    }
    out
}

/// 指数移动平均（EMA）.
///
/// 第一个有效值（索引 `period - 1`）= `sma(closes[0..period])`；
/// 后续：`ema[i] = closes[i] * k + ema[i-1] * (1 - k)`，`k = 2 / (period + 1)`。
///
/// 这是 pandas-ta / TradingView 标准 EMA 实现（Wilder smoothing 另有实现）。
///
/// # Examples
/// ```
/// use quantbot_quant::indicators::ema;
/// let v = ema(&[1.0, 2.0, 3.0, 4.0, 5.0], 3);
/// assert_eq!(v[0], None);
/// assert_eq!(v[1], None);
/// let seed = (1.0 + 2.0 + 3.0) / 3.0; // = 2.0
/// assert!((v[2].unwrap() - seed).abs() < 1e-12);
/// ```
pub fn ema(closes: &[f64], period: usize) -> Vec<Option<f64>> {
    assert!(period >= 1, "period must be >= 1");
    let n = closes.len();
    let mut out: Vec<Option<f64>> = vec![None; n];
    if n < period {
        return out;
    }

    // seed = SMA of first `period` values
    let seed: f64 = closes[..period].iter().sum::<f64>() / period as f64;
    out[period - 1] = Some(seed);

    let k = 2.0 / (period as f64 + 1.0);
    let mut prev = seed;
    for i in period..n {
        let v = closes[i] * k + prev * (1.0 - k);
        out[i] = Some(v);
        prev = v;
    }
    out
}

// ── Rhai-compatible wrappers (NaN instead of None) ───────────────────────────

/// SMA，Rhai 版：warmup 期返回 `f64::NAN`，其余与 [`sma`] 相同.
pub fn sma_rhai(closes: Vec<f64>, period: i64) -> Vec<f64> {
    let p = period.max(1) as usize;
    sma(&closes, p)
        .into_iter()
        .map(|v| v.unwrap_or(f64::NAN))
        .collect()
}

/// EMA，Rhai 版：warmup 期返回 `f64::NAN`，其余与 [`ema`] 相同.
pub fn ema_rhai(closes: Vec<f64>, period: i64) -> Vec<f64> {
    let p = period.max(1) as usize;
    ema(&closes, p)
        .into_iter()
        .map(|v| v.unwrap_or(f64::NAN))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sma_warmup_is_none() {
        let v = sma(&[1.0, 2.0, 3.0, 4.0], 3);
        assert_eq!(v[0], None);
        assert_eq!(v[1], None);
        assert!(v[2].is_some());
        assert!(v[3].is_some());
    }

    #[test]
    fn sma_values_correct() {
        let v = sma(&[1.0, 2.0, 3.0, 4.0, 5.0], 3);
        assert!((v[2].unwrap() - 2.0).abs() < 1e-15);
        assert!((v[3].unwrap() - 3.0).abs() < 1e-15);
        assert!((v[4].unwrap() - 4.0).abs() < 1e-15);
    }

    #[test]
    fn sma_period_1_equals_closes() {
        let closes = vec![5.0, 3.0, 7.0];
        let v = sma(&closes, 1);
        for (i, &c) in closes.iter().enumerate() {
            assert!((v[i].unwrap() - c).abs() < 1e-15);
        }
    }

    #[test]
    fn ema_seed_equals_sma_of_first_period() {
        let closes = vec![1.0, 2.0, 3.0, 4.0, 5.0];
        let v = ema(&closes, 3);
        // seed = (1+2+3)/3 = 2.0
        assert!((v[2].unwrap() - 2.0).abs() < 1e-15);
    }

    #[test]
    fn ema_subsequent_values_correct() {
        let closes = vec![1.0, 2.0, 3.0, 4.0, 5.0];
        let v = ema(&closes, 3);
        let k = 2.0 / 4.0; // 2/(3+1)
        let seed = 2.0;
        let expected_3 = 4.0 * k + seed * (1.0 - k); // i=3
        let expected_4 = 5.0 * k + expected_3 * (1.0 - k); // i=4
        assert!((v[3].unwrap() - expected_3).abs() < 1e-15);
        assert!((v[4].unwrap() - expected_4).abs() < 1e-15);
    }

    #[test]
    fn ema_warmup_is_none() {
        let v = ema(&[1.0, 2.0, 3.0], 3);
        assert_eq!(v[0], None);
        assert_eq!(v[1], None);
        assert!(v[2].is_some());
    }

    #[test]
    fn sma_rhai_nan_for_warmup() {
        let v = sma_rhai(vec![1.0, 2.0, 3.0, 4.0], 3);
        assert!(v[0].is_nan());
        assert!(v[1].is_nan());
        assert!((v[2] - 2.0).abs() < 1e-15);
        assert!((v[3] - 3.0).abs() < 1e-15);
    }

    #[test]
    fn ema_rhai_nan_for_warmup() {
        let v = ema_rhai(vec![1.0, 2.0, 3.0, 4.0], 3);
        assert!(v[0].is_nan());
        assert!(v[1].is_nan());
        assert!((v[2] - 2.0).abs() < 1e-15);
    }
}
