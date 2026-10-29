//! ONNX 推理引擎（基于 tract-onnx）.
//!
//! 实现 `common/data-store/models/<model_id>/` 目录约定：
//!   - `model.onnx`：ONNX 模型文件
//!   - `meta.json`：符合 plan §6 ml_model_meta.schema.json 约定
//!
//! ## MVP 限制（Phase C.4）
//! - 仅支持 single-sample 推理（batch_size = 1）
//! - 模型每次调用时加载，无缓存（C.x 扩展）
//! - 输入/输出均为 f32（内部从 f64 转换）

use std::path::Path;

use serde::Deserialize;
use tract_onnx::prelude::*;

// ── Meta JSON ─────────────────────────────────────────────────────────────────

/// 单个特征规格.
#[derive(Debug, Clone, Deserialize)]
pub struct FeatureSpec {
    pub name: String,
    pub dtype: String,
}

/// 模型元数据（对应 meta.json）.
#[derive(Debug, Clone, Deserialize)]
pub struct MlModelMeta {
    pub model_id: String,
    pub features: Vec<FeatureSpec>,
    pub input_shape: Vec<usize>,
    pub output_shape: Vec<usize>,
}

// ── ML Runner ─────────────────────────────────────────────────────────────────

type OnnxPlan = SimplePlan<TypedFact, Box<dyn TypedOp>, Graph<TypedFact, Box<dyn TypedOp>>>;

/// ONNX 推理器.
///
/// `load(model_dir)` 读取 `meta.json` + 编译 `model.onnx` 为 tract 推理计划。
pub struct MlRunner {
    pub meta: MlModelMeta,
    plan: OnnxPlan,
}

impl MlRunner {
    /// 从模型目录加载 meta.json + model.onnx.
    ///
    /// # Arguments
    /// * `model_dir` — `common/data-store/models/<model_id>/` 路径
    pub fn load<P: AsRef<Path>>(model_dir: P) -> Result<Self, String> {
        let dir = model_dir.as_ref();

        // 读取 meta.json
        let meta_path = dir.join("meta.json");
        let meta_json = std::fs::read_to_string(&meta_path)
            .map_err(|e| format!("Cannot read {meta_path:?}: {e}"))?;
        let meta: MlModelMeta = serde_json::from_str(&meta_json)
            .map_err(|e| format!("meta.json parse error in {meta_path:?}: {e}"))?;

        // 加载 + 编译 ONNX 模型
        let onnx_path = dir.join("model.onnx");
        let plan = tract_onnx::onnx()
            .model_for_path(&onnx_path)
            .map_err(|e| format!("Failed to load {onnx_path:?}: {e}"))?
            .into_optimized()
            .map_err(|e| format!("Failed to optimize {onnx_path:?}: {e}"))?
            .into_runnable()
            .map_err(|e| format!("Failed to compile {onnx_path:?}: {e}"))?;

        Ok(Self { meta, plan })
    }

    /// 对单个样本执行 ONNX 推理.
    ///
    /// # Arguments
    /// * `features` — 输入特征向量（长度必须与 `meta.input_shape` 的最后一维一致）
    ///
    /// # Returns
    /// 输出向量（f64）
    pub fn predict(&self, features: &[f64]) -> Result<Vec<f64>, String> {
        let n_features = features.len();
        let expected = self.meta.input_shape.last().copied().unwrap_or(n_features);
        if n_features != expected {
            return Err(format!(
                "特征数量不匹配：模型期望 {expected}，实际 {n_features}"
            ));
        }

        // 将 f64 转 f32（ONNX 模型通常用 f32）
        let data: Vec<f32> = features.iter().map(|&v| v as f32).collect();

        // 构建输入 Tensor（shape: [1, n_features]）
        let tensor = tract_ndarray::Array2::from_shape_vec(
            (1, n_features),
            data,
        )
        .map_err(|e| format!("Tensor construction error: {e}"))?
        .into_tensor();

        // 执行推理
        let result = self
            .plan
            .run(tvec!(tensor.into()))
            .map_err(|e| format!("Inference error: {e}"))?;

        // 提取输出（f32 → f64）
        let output_tensor = result
            .into_iter()
            .next()
            .ok_or_else(|| "Inference produced no output".to_string())?;

        let output: Vec<f64> = output_tensor
            .as_slice::<f32>()
            .map_err(|e| format!("Output cast error: {e}"))?
            .iter()
            .map(|&v| v as f64)
            .collect();

        Ok(output)
    }
}

// ── 单元测试 ──────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn test_model_dir() -> PathBuf {
        let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        p.push("../shared/fixtures/models/test_linear");
        p
    }

    #[test]
    fn ml_runner_loads_test_model() {
        let runner = MlRunner::load(test_model_dir())
            .expect("test_linear model should load");
        assert_eq!(runner.meta.model_id, "test_linear");
        assert_eq!(runner.meta.features.len(), 2);
        assert_eq!(runner.meta.input_shape, vec![1, 2]);
        assert_eq!(runner.meta.output_shape, vec![1, 1]);
    }

    #[test]
    fn ml_runner_prediction_correct() {
        let runner = MlRunner::load(test_model_dir())
            .expect("test_linear model should load");

        // W=[[0.5, 0.3]], b=[0.1]: 0.5*1.0 + 0.3*2.0 + 0.1 = 1.2
        let output = runner.predict(&[1.0, 2.0]).expect("predict should succeed");
        assert_eq!(output.len(), 1, "output should have 1 element");

        let expected = 0.5_f64 * 1.0 + 0.3 * 2.0 + 0.1; // = 1.2
        let diff = (output[0] - expected).abs();
        assert!(
            diff < 1e-5,
            "prediction {:.6} vs expected {:.6} (diff {:.3e})",
            output[0], expected, diff
        );
    }
}
