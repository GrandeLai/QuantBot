//! 优化器（网格搜索）集成测试.

use std::path::PathBuf;

use quantbot_quant::optimizer::{grid_search_ma_crossover, GridSearchConfig};

// ── helpers ───────────────────────────────────────────────────────────────────

fn golden_path() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("../shared/fixtures/golden/optimizer_grid_basic.json");
    p
}

#[derive(serde::Deserialize)]
struct OptimizerGolden {
    input: OptimizerInput,
    expected: Vec<GoldenResult>,
    tolerance: OptimizerTolerance,
}

#[derive(serde::Deserialize)]
struct OptimizerInput {
    closes: Vec<f64>,
    initial_cash: f64,
    fast_periods: Vec<usize>,
    slow_periods: Vec<usize>,
    risk_free_rate: f64,
    periods_per_year: f64,
}

#[derive(serde::Deserialize, Debug)]
struct GoldenResult {
    fast_period: usize,
    slow_period: usize,
    sharpe_ratio: f64,
    total_return: f64,
    max_drawdown: f64,
}

#[derive(serde::Deserialize)]
struct OptimizerTolerance {
    abs: f64,
}

fn load_golden() -> OptimizerGolden {
    let path = golden_path();
    let content = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("Cannot read {path:?}: {e}"));
    serde_json::from_str(&content).expect("optimizer_grid_basic.json parse error")
}

// ── golden equivalence test ───────────────────────────────────────────────────

#[test]
fn grid_search_results_match_golden() {
    let g = load_golden();
    let tol = g.tolerance.abs;

    let config = GridSearchConfig {
        fast_periods: g.input.fast_periods.clone(),
        slow_periods: g.input.slow_periods.clone(),
    };
    let results = grid_search_ma_crossover(
        &g.input.closes,
        g.input.initial_cash,
        &config,
        g.input.risk_free_rate,
        g.input.periods_per_year,
    );

    assert_eq!(
        results.len(),
        g.expected.len(),
        "result count mismatch: Rust={} golden={}",
        results.len(),
        g.expected.len()
    );

    for (i, (rust_r, golden_r)) in results.iter().zip(g.expected.iter()).enumerate() {
        assert_eq!(rust_r.fast_period, golden_r.fast_period, "row {i} fast_period");
        assert_eq!(rust_r.slow_period, golden_r.slow_period, "row {i} slow_period");

        let sharpe_diff = (rust_r.sharpe_ratio - golden_r.sharpe_ratio).abs();
        assert!(
            sharpe_diff < tol,
            "row {i} sharpe_ratio: Rust={:.8} golden={:.8} diff={:.3e}",
            rust_r.sharpe_ratio, golden_r.sharpe_ratio, sharpe_diff
        );

        let ret_diff = (rust_r.total_return - golden_r.total_return).abs();
        assert!(
            ret_diff < tol,
            "row {i} total_return diff={ret_diff:.3e}"
        );

        let dd_diff = (rust_r.max_drawdown - golden_r.max_drawdown).abs();
        assert!(
            dd_diff < tol,
            "row {i} max_drawdown diff={dd_diff:.3e}"
        );
    }

    println!("  ✓ grid search: {} combos match golden (tol={tol:.0e})", results.len());
}

// ── functional tests ──────────────────────────────────────────────────────────

#[test]
fn grid_search_returns_sorted_by_sharpe() {
    let config = GridSearchConfig {
        fast_periods: vec![2, 3, 5],
        slow_periods: vec![5, 7, 10],
    };
    let closes: Vec<f64> = vec![
        100.0, 101.0, 102.0, 103.0, 99.0, 98.0, 100.0, 105.0, 107.0, 110.0,
        112.0, 108.0, 106.0, 104.0, 102.0, 100.0, 98.0, 99.0, 101.0, 103.0,
        105.0, 107.0, 109.0, 111.0, 113.0,
    ];
    let results = grid_search_ma_crossover(&closes, 10_000.0, &config, 0.0, 252.0);
    assert!(!results.is_empty());
    for w in results.windows(2) {
        assert!(
            w[0].sharpe_ratio >= w[1].sharpe_ratio,
            "not sorted: {:.4} < {:.4}",
            w[0].sharpe_ratio, w[1].sharpe_ratio
        );
    }
    println!("  ✓ results sorted by sharpe ({} combos)", results.len());
}

#[test]
fn grid_search_skips_invalid_combos() {
    let config = GridSearchConfig {
        fast_periods: vec![5, 10],
        slow_periods: vec![3, 7],
    };
    let closes: Vec<f64> = (0..30).map(|i| 100.0 + i as f64).collect();
    let results = grid_search_ma_crossover(&closes, 10_000.0, &config, 0.0, 252.0);
    for r in &results {
        assert!(
            r.fast_period < r.slow_period,
            "invalid combo: fast={} slow={}",
            r.fast_period, r.slow_period
        );
    }
    println!("  ✓ all results have fast < slow");
}
