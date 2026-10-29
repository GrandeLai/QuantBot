//! Golden dataset cross-language equivalence tests.
//!
//! 读 `common/data-store/golden/expected/<case>.json`（由
//! tools/golden-generator 生成的 Python 期望输出），跑 Rust 实现，
//! 断言每一项在容差内对齐。

use std::fs;
use std::path::PathBuf;

use serde::Deserialize;

#[derive(Deserialize)]
struct GoldenInput {
    closes: Vec<f64>,
    fast_period: usize,
    slow_period: usize,
    initial_cash: f64,
    risk_free_rate: f64,
    periods_per_year: f64,
}

#[derive(Deserialize)]
struct GoldenExpected {
    equity_curve: Vec<f64>,
    total_return: f64,
    sharpe_ratio: f64,
    max_drawdown: f64,
    max_drawdown_start: usize,
    max_drawdown_end: usize,
}

#[derive(Deserialize)]
struct GoldenTolerance {
    abs: f64,
    #[allow(dead_code)]
    rel: f64,
}

#[derive(Deserialize)]
struct GoldenCase {
    #[allow(dead_code)]
    case_id: String,
    input: GoldenInput,
    expected: GoldenExpected,
    tolerance: GoldenTolerance,
}

fn load_case(case_id: &str) -> GoldenCase {
    // Cargo runs tests with cwd = package dir (apps/quant-assistant/backend/)
    let mut path = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    path.push("../shared/fixtures/golden");
    path.push(format!("{case_id}.json"));

    let content = fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("Failed to read golden case at {path:?}: {e}"));
    serde_json::from_str(&content).expect("Failed to parse golden JSON")
}

#[test]
fn ma_crossover_basic_matches_python_expected() {
    let case = load_case("ma_crossover_basic");

    let actual_equity = quantbot_quant::run_ma_crossover_backtest(
        &case.input.closes,
        case.input.fast_period,
        case.input.slow_period,
        case.input.initial_cash,
    )
    .expect("Rust backtest should succeed on golden input");

    assert_eq!(
        actual_equity.len(),
        case.expected.equity_curve.len(),
        "equity_curve length mismatch"
    );

    let max_abs_diff = actual_equity
        .iter()
        .zip(case.expected.equity_curve.iter())
        .map(|(a, b)| (a - b).abs())
        .fold(0.0_f64, f64::max);

    assert!(
        max_abs_diff <= case.tolerance.abs,
        "equity_curve drift: max |Δ| = {:.3e} > tolerance abs = {:.3e}\n  actual:   {:?}\n  expected: {:?}",
        max_abs_diff,
        case.tolerance.abs,
        actual_equity,
        case.expected.equity_curve
    );

    // Compute Sharpe / drawdown / total_return same way as the HTTP endpoint
    let returns: Vec<f64> = actual_equity
        .windows(2)
        .map(|w| (w[1] - w[0]) / w[0])
        .collect();
    let actual_sharpe = quantbot_quant::sharpe_ratio(
        &returns,
        case.input.risk_free_rate,
        case.input.periods_per_year,
    );
    let (actual_dd, actual_dd_start, actual_dd_end) =
        quantbot_quant::max_drawdown(&actual_equity);
    let actual_total_return = (actual_equity.last().unwrap() - case.input.initial_cash)
        / case.input.initial_cash;

    assert!(
        (actual_sharpe - case.expected.sharpe_ratio).abs() <= case.tolerance.abs,
        "sharpe drift: actual={actual_sharpe} expected={} abs={:.3e}",
        case.expected.sharpe_ratio,
        case.tolerance.abs
    );
    assert!(
        (actual_dd - case.expected.max_drawdown).abs() <= case.tolerance.abs,
        "max_drawdown drift: actual={actual_dd} expected={}",
        case.expected.max_drawdown
    );
    assert_eq!(
        actual_dd_start, case.expected.max_drawdown_start,
        "max_drawdown_start mismatch"
    );
    assert_eq!(
        actual_dd_end, case.expected.max_drawdown_end,
        "max_drawdown_end mismatch"
    );
    assert!(
        (actual_total_return - case.expected.total_return).abs() <= case.tolerance.abs,
        "total_return drift: actual={actual_total_return} expected={}",
        case.expected.total_return
    );

    println!(
        "  ✓ ma_crossover_basic equivalence: max |Δ equity| = {max_abs_diff:.3e}, sharpe diff = {:.3e}, max_dd diff = {:.3e}",
        (actual_sharpe - case.expected.sharpe_ratio).abs(),
        (actual_dd - case.expected.max_drawdown).abs(),
    );
}
