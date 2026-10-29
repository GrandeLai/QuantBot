//! ONNX 推理集成测试.
//!
//! 验证 MlRunner::load + predict 对 test_linear 模型产生正确结果，
//! 并验证 Rhai 中 ml_predict_from_dir() 调用通过。

use std::path::PathBuf;

use quantbot_quant::ml_runner::MlRunner;
use quantbot_quant::runtime::RhaiEngine;

fn model_dir() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("../shared/fixtures/models/test_linear");
    p
}

// ── load + predict tests ──────────────────────────────────────────────────────

#[test]
fn onnx_model_loads() {
    let runner = MlRunner::load(model_dir()).expect("test_linear model should load");
    assert_eq!(runner.meta.model_id, "test_linear");
    assert_eq!(runner.meta.features.len(), 2);
    println!("  ✓ MlRunner loaded: model_id={}", runner.meta.model_id);
}

#[test]
fn onnx_prediction_matches_expected() {
    let runner = MlRunner::load(model_dir()).expect("test_linear model should load");

    // W=[[0.5, 0.3]], b=[0.1]
    // y = 0.5 * 1.0 + 0.3 * 2.0 + 0.1 = 1.2
    let output = runner.predict(&[1.0, 2.0]).expect("predict should succeed");

    assert_eq!(output.len(), 1, "output should have length 1");

    let expected = 0.5_f64 * 1.0 + 0.3 * 2.0 + 0.1;
    let diff = (output[0] - expected).abs();
    assert!(
        diff < 1e-5,
        "output[0]={:.8} expected={:.8} diff={diff:.3e}",
        output[0], expected
    );

    println!(
        "  ✓ ONNX prediction: output={:.6} expected={:.6} (diff={diff:.3e})",
        output[0], expected
    );
}

#[test]
fn onnx_wrong_feature_count_is_err() {
    let runner = MlRunner::load(model_dir()).expect("model loads");
    let result = runner.predict(&[1.0]);  // expects 2 features
    assert!(
        result.is_err(),
        "should fail with wrong feature count"
    );
}

// ── Rhai integration ──────────────────────────────────────────────────────────

#[test]
fn rhai_ml_predict_works() {
    let engine = RhaiEngine::new();
    let model_dir_str = model_dir()
        .canonicalize()
        .expect("model dir should exist")
        .to_str()
        .expect("path is valid utf-8")
        .to_string();

    let script = format!(
        r#"
        let features = [1.0, 2.0];
        let result = ml_predict_from_dir("{model_dir}", features);
        result[0]
        "#,
        model_dir = model_dir_str.replace('\\', "/"),
    );
    let strategy = engine
        .compile_str(&script, "ml_test")
        .expect("script compiles");
    let output: f64 = engine
        .engine()
        .eval_ast(strategy.ast())
        .expect("Rhai ml_predict_from_dir should succeed");

    let expected = 0.5_f64 * 1.0 + 0.3 * 2.0 + 0.1;
    let diff = (output - expected).abs();
    assert!(
        diff < 1e-5,
        "Rhai output={output:.8} expected={expected:.8} diff={diff:.3e}"
    );

    println!("  ✓ Rhai ml_predict_from_dir: output={output:.6} (diff={diff:.3e})");
}
