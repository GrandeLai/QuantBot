//! 回测报告绩效指标集成测试.

use std::path::PathBuf;

use quantbot_quant::reports::{calculate_report, format_report_text};
use quantbot_quant::run_ma_crossover_backtest;

// ── helpers ───────────────────────────────────────────────────────────────────

fn golden_path() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("../shared/fixtures/golden/reports_basic.json");
    p
}

#[derive(serde::Deserialize)]
struct ReportsGolden {
    input: ReportsInput,
    expected: ReportsExpected,
    tolerance: ReportsTolerance,
}

#[derive(serde::Deserialize)]
struct ReportsInput {
    closes: Vec<f64>,
    fast_period: usize,
    slow_period: usize,
    initial_cash: f64,
    risk_free_rate: f64,
    periods_per_year: f64,
}

#[derive(serde::Deserialize)]
struct ReportsExpected {
    total_return: f64,
    annual_return: f64,
    max_drawdown: f64,
    max_drawdown_start: usize,
    max_drawdown_end: usize,
    volatility: f64,
    sharpe_ratio: f64,
    // sortino can be None (infinity)
    sortino_ratio: Option<f64>,
    calmar_ratio: f64,
    total_bars: usize,
}

#[derive(serde::Deserialize)]
struct ReportsTolerance {
    abs: f64,
}

fn load_golden() -> ReportsGolden {
    let path = golden_path();
    let content = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("Cannot read {path:?}: {e}"));
    serde_json::from_str(&content).expect("reports_basic.json parse error")
}

// ── golden equivalence test ───────────────────────────────────────────────────

#[test]
fn report_metrics_match_golden() {
    let g = load_golden();
    let tol = g.tolerance.abs;

    let equity = run_ma_crossover_backtest(
        &g.input.closes,
        g.input.fast_period,
        g.input.slow_period,
        g.input.initial_cash,
    )
    .expect("MA crossover should succeed");

    let report = calculate_report(&equity, g.input.initial_cash, g.input.risk_free_rate, g.input.periods_per_year);

    macro_rules! check {
        ($field:ident) => {
            let diff = (report.$field - g.expected.$field).abs();
            assert!(
                diff < tol,
                "{}: Rust={:.8} golden={:.8} diff={:.3e}",
                stringify!($field), report.$field, g.expected.$field, diff
            );
        };
    }

    check!(total_return);
    check!(annual_return);
    check!(max_drawdown);
    check!(volatility);
    check!(sharpe_ratio);
    check!(calmar_ratio);

    assert_eq!(report.max_drawdown_start, g.expected.max_drawdown_start, "dd_start");
    assert_eq!(report.max_drawdown_end, g.expected.max_drawdown_end, "dd_end");
    assert_eq!(report.total_bars, g.expected.total_bars, "total_bars");

    // sortino: golden may be None (infinity); Rust returns f64::INFINITY
    if let Some(golden_sortino) = g.expected.sortino_ratio {
        let diff = (report.sortino_ratio - golden_sortino).abs();
        assert!(diff < tol, "sortino diff={diff:.3e}");
    } else {
        assert!(
            report.sortino_ratio.is_infinite(),
            "sortino should be infinite, got {}",
            report.sortino_ratio
        );
    }

    println!("  ✓ report metrics match golden (tol={tol:.0e})");
}

// ── functional tests ──────────────────────────────────────────────────────────

#[test]
fn sortino_ratio_positive_for_uptrend() {
    // Monotonically increasing equity → positive returns → positive sortino
    let equity: Vec<f64> = (0..50).map(|i| 10_000.0 + i as f64 * 50.0).collect();
    let report = calculate_report(&equity, 10_000.0, 0.0, 252.0);
    assert!(
        report.sortino_ratio.is_infinite() || report.sortino_ratio > 0.0,
        "sortino should be positive or inf for uptrend, got {}",
        report.sortino_ratio
    );
}

#[test]
fn calmar_ratio_definition() {
    // calmar = annual_return / max_drawdown
    let equity = vec![
        10_000.0, 11_000.0, 10_500.0, 10_200.0, 11_500.0, 12_000.0,
    ];
    let report = calculate_report(&equity, 10_000.0, 0.0, 252.0);
    if report.max_drawdown > 0.0 {
        let expected_calmar = report.annual_return / report.max_drawdown;
        let diff = (report.calmar_ratio - expected_calmar).abs();
        assert!(diff < 1e-12, "calmar definition violated: diff={diff:.3e}");
    }
}

#[test]
fn format_report_text_roundtrip() {
    let equity: Vec<f64> = (0..25)
        .map(|i| 10_000.0 + i as f64 * 100.0)
        .collect();
    let report = calculate_report(&equity, 10_000.0, 0.0, 252.0);
    let text = format_report_text(&report);
    assert!(text.contains("Total Return"));
    assert!(text.contains("Sharpe"));
    assert!(text.contains("Calmar"));
    println!("{text}");
}
