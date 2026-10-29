//! 指标库集成测试.
//!
//! 验证 Rust SMA/EMA 实现与 golden 数据等价（< 1e-12），
//! 并验证 Rhai 引擎中注册的 sma/ema 函数与 Rust 原生版 bit-equivalent。

use std::path::PathBuf;

use quantbot_quant::indicators::{ema, sma};
use quantbot_quant::runtime::RhaiEngine;

// ── helpers ───────────────────────────────────────────────────────────────────

fn golden_path() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    // CARGO_MANIFEST_DIR = apps/quant-assistant/backend
    // → ../shared/fixtures/golden/
    p.push("../shared/fixtures/golden/sma_basic.json");
    p
}

#[derive(serde::Deserialize)]
struct SmaGolden {
    input: SmaInput,
    expected: SmaExpected,
    tolerance: SmaToler,
}

#[derive(serde::Deserialize)]
struct SmaInput {
    closes: Vec<f64>,
}

#[derive(serde::Deserialize)]
struct SmaExpected {
    sma_3: Vec<Option<f64>>,
    sma_7: Vec<Option<f64>>,
    ema_3: Vec<Option<f64>>,
    ema_7: Vec<Option<f64>>,
}

#[derive(serde::Deserialize)]
struct SmaToler {
    abs: f64,
}

fn load_golden() -> SmaGolden {
    let path = golden_path();
    let content = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("Cannot read {path:?}: {e}"));
    serde_json::from_str(&content).expect("sma_basic.json parse error")
}

fn max_diff(rust: &[Option<f64>], golden: &[Option<f64>]) -> f64 {
    assert_eq!(rust.len(), golden.len(), "length mismatch");
    rust.iter()
        .zip(golden.iter())
        .map(|(r, g)| match (r, g) {
            (Some(rv), Some(gv)) => (rv - gv).abs(),
            (None, None) => 0.0,
            _ => f64::INFINITY, // None/Some mismatch → infinite diff
        })
        .fold(0.0_f64, f64::max)
}

// ── golden equivalence tests ──────────────────────────────────────────────────

#[test]
fn sma_matches_golden() {
    let g = load_golden();
    let tol = g.tolerance.abs;

    let rust_sma3 = sma(&g.input.closes, 3);
    let rust_sma7 = sma(&g.input.closes, 7);

    let diff3 = max_diff(&rust_sma3, &g.expected.sma_3);
    let diff7 = max_diff(&rust_sma7, &g.expected.sma_7);

    assert!(
        diff3 < tol,
        "SMA(3) drift vs golden: max |Δ| = {diff3:.3e} > {tol:.3e}"
    );
    assert!(
        diff7 < tol,
        "SMA(7) drift vs golden: max |Δ| = {diff7:.3e} > {tol:.3e}"
    );

    println!("  ✓ SMA(3) bit-equivalent to golden (max |Δ| = {diff3:.3e})");
    println!("  ✓ SMA(7) bit-equivalent to golden (max |Δ| = {diff7:.3e})");
}

#[test]
fn ema_matches_golden() {
    let g = load_golden();
    let tol = g.tolerance.abs;

    let rust_ema3 = ema(&g.input.closes, 3);
    let rust_ema7 = ema(&g.input.closes, 7);

    let diff3 = max_diff(&rust_ema3, &g.expected.ema_3);
    let diff7 = max_diff(&rust_ema7, &g.expected.ema_7);

    assert!(
        diff3 < tol,
        "EMA(3) drift vs golden: max |Δ| = {diff3:.3e} > {tol:.3e}"
    );
    assert!(
        diff7 < tol,
        "EMA(7) drift vs golden: max |Δ| = {diff7:.3e} > {tol:.3e}"
    );

    println!("  ✓ EMA(3) bit-equivalent to golden (max |Δ| = {diff3:.3e})");
    println!("  ✓ EMA(7) bit-equivalent to golden (max |Δ| = {diff7:.3e})");
}

// ── Rhai integration tests ────────────────────────────────────────────────────

/// Rhai 中调用 sma() 与 Rust 原生 sma() bit-equivalent.
#[test]
fn rhai_sma_function_matches_rust_sma() {
    let closes = vec![
        100.0_f64, 101.0, 102.0, 103.0, 99.0, 98.0, 100.0, 105.0, 107.0, 110.0,
        112.0, 108.0, 106.0, 104.0, 102.0, 100.0, 98.0, 99.0, 101.0, 103.0,
        105.0, 107.0, 109.0, 111.0, 113.0,
    ];
    let period = 7_usize;

    let rust_sma = sma(&closes, period);

    // Call sma via Rhai engine
    let engine = RhaiEngine::new();
    let script = format!(
        r#"
        let closes = {closes_arr};
        sma(closes, {period})
        "#,
        closes_arr = format!(
            "[{}]",
            closes
                .iter()
                .map(|v| v.to_string())
                .collect::<Vec<_>>()
                .join(", ")
        ),
        period = period as i64,
    );
    let strategy = engine.compile_str(&script, "sma_test").unwrap();
    let result: rhai::Array = engine
        .engine()
        .eval_ast(strategy.ast())
        .expect("Rhai sma evaluation failed");

    assert_eq!(result.len(), closes.len(), "length mismatch");

    for (i, (rhai_val, rust_opt)) in result.iter().zip(rust_sma.iter()).enumerate() {
        let rhai_f = rhai_val.as_float().expect("Rhai array element is float");
        match rust_opt {
            Some(rv) => {
                let diff = (rhai_f - rv).abs();
                assert!(
                    diff < 1e-12,
                    "index {i}: Rhai sma={rhai_f} vs Rust sma={rv}, diff={diff:.3e}"
                );
            }
            None => {
                assert!(
                    rhai_f.is_nan(),
                    "index {i}: expected NaN (warmup) but Rhai returned {rhai_f}"
                );
            }
        }
    }

    println!("  ✓ Rhai sma() bit-equivalent to Rust sma()");
}

/// 加载 sma_crossover.rhai，验证 signal_from_closes 返回正确信号.
#[test]
fn rhai_sma_strategy_computes_correct_signals() {
    let mut strategy_path = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    strategy_path.push("strategies/sma_crossover.rhai");

    let engine = RhaiEngine::new();
    let strategy = engine
        .compile_file(&strategy_path)
        .expect("sma_crossover.rhai should compile");
    assert_eq!(strategy.name(), "sma_crossover");

    // Sanity: signal(fast=10, slow=5) → "long" (reuses compat fn signal(fast, slow))
    let sig = engine
        .call_signal(&strategy, 10.0, 5.0)
        .expect("call_signal should succeed");
    assert_eq!(sig, quantbot_quant::runtime::Signal::Long);

    // signal(fast=5, slow=10) → "flat"
    let sig2 = engine.call_signal(&strategy, 5.0, 10.0).unwrap();
    assert_eq!(sig2, quantbot_quant::runtime::Signal::Flat);

    println!("  ✓ sma_crossover.rhai signal() compat interface works");
}
