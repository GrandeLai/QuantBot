//! Walk-Forward 窗口切割 + 回测集成测试.
//!
//! 验证 Rust `build_walk_forward_windows` 与 Python golden bit-identical，
//! 并验证 `run_walk_forward_ma_backtest` 基本运行正确。

use std::path::PathBuf;

use quantbot_quant::walk_forward::{
    build_walk_forward_windows, run_walk_forward_ma_backtest, WalkForwardConfig, WalkForwardWindow,
};

// ── helpers ───────────────────────────────────────────────────────────────────

fn golden_path() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("../shared/fixtures/golden/walk_forward_splits_basic.json");
    p
}

#[derive(serde::Deserialize)]
struct GoldenRoot {
    cases: Vec<GoldenCase>,
}

#[derive(serde::Deserialize)]
struct GoldenCase {
    name: String,
    config: GoldenConfig,
    expected_windows: Vec<GoldenWindow>,
    n_windows: usize,
}

#[derive(serde::Deserialize)]
struct GoldenConfig {
    total_rows: usize,
    train_size: usize,
    test_size: usize,
    step_size: usize,
    embargo_size: usize,
}

#[derive(serde::Deserialize, Debug)]
struct GoldenWindow {
    train_start: usize,
    train_end: usize,
    test_start: usize,
    test_end: usize,
}

fn load_golden() -> GoldenRoot {
    let path = golden_path();
    let content = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("Cannot read {path:?}: {e}"));
    serde_json::from_str(&content).expect("walk_forward_splits_basic.json parse error")
}

fn windows_match(rust: &WalkForwardWindow, golden: &GoldenWindow) -> bool {
    rust.train_start == golden.train_start
        && rust.train_end == golden.train_end
        && rust.test_start == golden.test_start
        && rust.test_end == golden.test_end
}

// ── golden equivalence test ───────────────────────────────────────────────────

/// Walk-Forward 窗口索引与 Python golden bit-identical.
#[test]
fn walk_forward_splits_match_golden() {
    let golden = load_golden();

    for case in &golden.cases {
        let cfg = WalkForwardConfig {
            train_size: case.config.train_size,
            test_size: case.config.test_size,
            step_size: case.config.step_size,
            embargo_size: case.config.embargo_size,
        };
        let rust_wins = build_walk_forward_windows(case.config.total_rows, &cfg);

        assert_eq!(
            rust_wins.len(),
            case.n_windows,
            "case '{}': n_windows mismatch (Rust={} vs golden={})",
            case.name,
            rust_wins.len(),
            case.n_windows
        );

        for (i, (rw, gw)) in rust_wins.iter().zip(case.expected_windows.iter()).enumerate() {
            assert!(
                windows_match(rw, gw),
                "case '{}' window {}: Rust={:?} vs golden={gw:?}",
                case.name,
                i,
                rw,
            );
        }

        println!(
            "  ✓ case '{}': {} windows bit-identical",
            case.name,
            rust_wins.len()
        );
    }
}

// ── functional tests ──────────────────────────────────────────────────────────

/// run_walk_forward_ma_backtest 基本运行测试.
#[test]
fn walk_forward_ma_backtest_runs() {
    // 使用趋势上行数据，保证 MA crossover 有信号
    let closes: Vec<f64> = (0..100).map(|i| 100.0 + i as f64).collect();
    let cfg = WalkForwardConfig {
        train_size: 20,
        test_size: 10,
        step_size: 10,
        embargo_size: 2,
    };
    let result = run_walk_forward_ma_backtest(&closes, 3, 7, 10_000.0, &cfg)
        .expect("walk-forward backtest should succeed");

    assert!(result.n_windows > 0, "should have at least one window");
    assert!(
        !result.equity_curve.is_empty(),
        "equity_curve should not be empty"
    );
    assert_eq!(
        result.window_splits.len(),
        build_walk_forward_windows(100, &cfg).len(),
        "window_splits should match build_walk_forward_windows"
    );

    println!("  ✓ walk-forward: {} windows, equity_curve len={}", result.n_windows, result.equity_curve.len());
}

/// 数据不足时 run_walk_forward_ma_backtest 返回 Err.
#[test]
fn walk_forward_backtest_too_few_rows_is_err() {
    let closes = vec![100.0; 10];
    let cfg = WalkForwardConfig {
        train_size: 20,
        test_size: 10,
        step_size: 5,
        embargo_size: 2,
    };
    let result = run_walk_forward_ma_backtest(&closes, 3, 7, 10_000.0, &cfg);
    assert!(result.is_err(), "should fail with too few rows");
}

/// 每个测试窗口从测试前最后一个估值点承接收益。
#[test]
fn walk_forward_equity_compounds_only_test_bar_returns() {
    let closes: Vec<f64> = (0..80).map(|i| 100.0 + i as f64).collect();
    let cfg = WalkForwardConfig {
        train_size: 20,
        test_size: 10,
        step_size: 10,
        embargo_size: 2,
    };
    let result =
        run_walk_forward_ma_backtest(&closes, 3, 7, 10_000.0, &cfg).unwrap();

    let mut base = 10_000.0;
    assert_eq!(result.equity_curve.len(), result.n_windows * cfg.test_size);
    for (window, curve) in result.window_splits.iter().zip(result.equity_curve.chunks(cfg.test_size)) {
        let preceding_close = closes[window.test_start - 1];
        for (offset, equity) in curve.iter().enumerate() {
            let expected = base * closes[window.test_start + offset] / preceding_close;
            assert!((equity - expected).abs() < 1e-6);
        }
        base *= closes[window.test_end] / preceding_close;
    }
}

/// 隔离带仅供时间推进，不属于测试期收益。
#[test]
fn walk_forward_equity_excludes_embargo_bars() {
    let closes: Vec<f64> = (0..15).map(|i| 100.0 + i as f64).collect();
    let cfg = WalkForwardConfig {
        train_size: 7,
        test_size: 5,
        step_size: 5,
        embargo_size: 3,
    };
    let result = run_walk_forward_ma_backtest(&closes, 2, 3, 10_000.0, &cfg).unwrap();

    assert_eq!(result.n_windows, 1);
    assert_eq!(result.window_splits[0].test_start, 10);
    assert_eq!(result.equity_curve.len(), 5);
    // 已持仓的单调上涨样本，从测试前索引 9（价格 109）承接首根测试收益。
    for (offset, equity) in result.equity_curve.iter().enumerate() {
        let expected = 10_000.0 * (110.0 + offset as f64) / 109.0;
        assert!((equity - expected).abs() < 1e-6);
    }
}

#[test]
fn walk_forward_keeps_first_test_bar_loss() {
    let closes = vec![100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0, 90.0, 89.0];
    let cfg = WalkForwardConfig { train_size: 7, test_size: 2, step_size: 2, embargo_size: 3 };
    let result = run_walk_forward_ma_backtest(&closes, 2, 3, 10_000.0, &cfg).unwrap();
    assert_eq!(result.equity_curve.len(), 2);
    // 索引 10 的下跌不会回头改变由索引 9 及以前价格决定的仓位。
    assert!((result.equity_curve[0] - 10_000.0 * 90.0 / 109.0).abs() < 1e-6);
    assert!((result.equity_curve[1] - 10_000.0 * 89.0 / 109.0).abs() < 1e-6);
}

#[test]
fn walk_forward_rejects_zero_window_dimensions() {
    let closes = vec![100.0; 30];
    for (train_size, test_size, step_size) in [(0, 5, 5), (7, 0, 5), (7, 5, 0)] {
        let cfg = WalkForwardConfig { train_size, test_size, step_size, embargo_size: 2 };
        assert!(run_walk_forward_ma_backtest(&closes, 2, 3, 10_000.0, &cfg).is_err());
        assert!(build_walk_forward_windows(closes.len(), &cfg).is_empty());
    }
}

#[test]
fn walk_forward_rejects_unrepresentable_windows_and_invalid_periods() {
    let closes = vec![100.0; 30];
    let mut cfg = WalkForwardConfig { train_size: usize::MAX, test_size: 5, step_size: 5, embargo_size: 2 };
    assert!(run_walk_forward_ma_backtest(&closes, 2, 3, 10_000.0, &cfg).is_err());
    cfg.train_size = 7;
    for (fast, slow) in [(0, 3), (2, 0), (4, 3)] {
        assert!(run_walk_forward_ma_backtest(&closes, fast, slow, 10_000.0, &cfg).is_err());
    }
    // 极大步长只应产生首个有效窗口，结束时不能整数溢出。
    cfg.step_size = usize::MAX;
    let result = run_walk_forward_ma_backtest(&closes, 2, 3, 10_000.0, &cfg).unwrap();
    assert_eq!(result.n_windows, 1);
    assert_eq!(result.equity_curve.len(), 5);
}

#[test]
fn walk_forward_rejects_overlapping_test_returns() {
    let closes = vec![100.0; 30];
    let cfg = WalkForwardConfig { train_size: 7, test_size: 5, step_size: 1, embargo_size: 2 };
    // 窗口切分仍可用于重叠验证，但不能将同一天收益重复拼接为一条净值。
    let windows = build_walk_forward_windows(closes.len(), &cfg);
    assert!(windows[1].test_start <= windows[0].test_end);
    assert!(run_walk_forward_ma_backtest(&closes, 2, 3, 10_000.0, &cfg).is_err());
}

#[test]
fn walk_forward_rejects_invalid_cash_and_prices() {
    let closes = vec![100.0; 30];
    let cfg = WalkForwardConfig { train_size: 7, test_size: 5, step_size: 5, embargo_size: 2 };
    for cash in [0.0, -1.0, f64::NAN, f64::INFINITY] {
        assert!(run_walk_forward_ma_backtest(&closes, 2, 3, cash, &cfg).is_err());
    }
    for price in [0.0, -1.0, f64::NAN, f64::INFINITY] {
        let mut invalid = closes.clone();
        invalid[10] = price;
        assert!(run_walk_forward_ma_backtest(&invalid, 2, 3, 10_000.0, &cfg).is_err());
    }
}
