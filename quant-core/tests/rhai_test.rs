//! Rhai 策略引擎集成测试.
//!
//! 验证 Rhai 驱动的 MA crossover 与 Rust 硬编码版**数值等价**——
//! Rhai 引擎不应引入浮点误差（决策路径相同，bookkeeping 也相同）.

use std::path::PathBuf;

fn strategy_path() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("strategies/ma_crossover.rhai");
    p
}

/// 与 mvp-backtest 一致的 25 根 K 线固定输入（与 golden case 同款）.
fn fixture_closes() -> Vec<f64> {
    vec![
        100.0, 101.0, 102.0, 103.0, 99.0, 98.0, 100.0, 105.0, 107.0, 110.0,
        112.0, 108.0, 106.0, 104.0, 102.0, 100.0, 98.0, 99.0, 101.0, 103.0,
        105.0, 107.0, 109.0, 111.0, 113.0,
    ]
}

#[test]
fn rhai_ma_crossover_matches_rust_hardcoded() {
    let closes = fixture_closes();
    let fast_period = 3;
    let slow_period = 7;
    let initial_cash = 10_000.0;

    let hardcoded = quantbot_quant::run_ma_crossover_backtest(
        &closes,
        fast_period,
        slow_period,
        initial_cash,
    )
    .expect("hardcoded backtest should succeed");

    let rhai_driven = quantbot_quant::run_rhai_ma_crossover_backtest(
        &closes,
        strategy_path(),
        fast_period,
        slow_period,
        initial_cash,
    )
    .expect("rhai-driven backtest should succeed");

    assert_eq!(
        hardcoded.len(),
        rhai_driven.len(),
        "equity_curve length mismatch"
    );

    let max_abs_diff = hardcoded
        .iter()
        .zip(rhai_driven.iter())
        .map(|(a, b)| (a - b).abs())
        .fold(0.0_f64, f64::max);

    assert!(
        max_abs_diff < 1e-12,
        "Rhai-driven vs hardcoded MA crossover drift: max |Δ| = {max_abs_diff:.3e} > 1e-12\n  hardcoded: {hardcoded:?}\n  rhai:      {rhai_driven:?}"
    );

    println!(
        "  ✓ Rhai-driven MA crossover bit-equivalent to hardcoded (max |Δ| = {max_abs_diff:.3e})"
    );
}

#[test]
fn rhai_strategy_path_loads_from_disk() {
    use quantbot_quant::runtime::RhaiEngine;

    let engine = RhaiEngine::new();
    let strategy = engine
        .compile_file(strategy_path())
        .expect("ma_crossover.rhai should compile");
    assert_eq!(strategy.name(), "ma_crossover");

    // Sanity: signal(fast=10, slow=5) → "long"
    let signal = engine
        .call_signal(&strategy, 10.0, 5.0)
        .expect("call_signal should succeed");
    assert_eq!(signal, quantbot_quant::runtime::Signal::Long);
}
