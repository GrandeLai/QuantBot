/**
 * ML 策略训练与预测面板.
 */
import { useCallback, useEffect, useState } from "react";
import { cn } from "../lib/utils";

interface Prediction {
  predictions: number[];
  count: number;
}

export default function MLStrategyPanel() {
  const [modelName, setModelName] = useState("lgbm_v1");
  const [barsJson, setBarsJson] = useState("[]");
  const [models, setModels] = useState<string[]>([]);
  const [prediction, setPrediction] = useState<Prediction | null>(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadModels = useCallback(async () => {
    try {
      const res = await fetch("/api/ml/models");
      if (res.ok) {
        const d = (await res.json()) as { models: string[] };
        setModels(d.models);
      }
    } catch (_) {}
  }, []);

  useEffect(() => {
    void loadModels();
  }, [loadModels]);

  const trainModel = async () => {
    setLoading(true);
    setError(null);
    try {
      let bars: unknown[];
      try {
        bars = JSON.parse(barsJson) as unknown[];
      } catch {
        setError("Bars JSON 格式错误");
        return;
      }
      const res = await fetch("/api/ml/train", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_name: modelName, bars }),
      });
      if (!res.ok) {
        const e = (await res.json()) as { detail: string };
        setError(e.detail);
        return;
      }
      const d = (await res.json()) as { message: string };
      setMessage(d.message);
      void loadModels();
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  const predict = async () => {
    setLoading(true);
    setError(null);
    try {
      let bars: unknown[];
      try {
        bars = JSON.parse(barsJson) as unknown[];
      } catch {
        setError("Bars JSON 格式错误");
        return;
      }
      const res = await fetch("/api/ml/predict", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_name: modelName, bars }),
      });
      if (!res.ok) {
        const e = (await res.json()) as { detail: string };
        setError(e.detail);
        return;
      }
      setPrediction((await res.json()) as Prediction);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  const signalColor = (s: number) =>
    s === 1 ? "text-[#00C087]" : s === -1 ? "text-[#FF4D4D]" : "text-[#8E9299]";

  const signalLabel = (s: number) =>
    s === 1 ? "买入 ↑" : s === -1 ? "卖出 ↓" : "持平 —";

  return (
    <div className="flex gap-4">
      {/* ── 左侧：配置 ────────────────────────────── */}
      <div className="w-72 shrink-0 bg-[#151619] border border-[#2A2D35] rounded-xl p-5 flex flex-col gap-3">
        <p className="text-xs font-mono uppercase tracking-wider text-[#8E9299]">
          ML 策略配置
        </p>

        <div className="flex flex-col gap-1">
          <label className="text-[10px] text-[#8E9299]">模型名称</label>
          <input
            value={modelName}
            onChange={(e) => setModelName(e.target.value)}
            className="w-full bg-[#1C1E22] border border-[#2A2D35] rounded-lg px-3 py-1.5 text-xs text-white outline-none focus:border-[#4A4D55] transition-colors"
          />
        </div>

        <div className="flex flex-col gap-1">
          <label className="text-[10px] text-[#8E9299]">K 线数据 (JSON)</label>
          <textarea
            value={barsJson}
            onChange={(e) => setBarsJson(e.target.value)}
            rows={6}
            className="w-full bg-[#1C1E22] border border-[#2A2D35] rounded-lg px-3 py-2 text-xs text-white outline-none focus:border-[#4A4D55] resize-y transition-colors"
            style={{ fontFamily: "'JetBrains Mono', monospace" }}
          />
        </div>

        <div className="flex gap-2">
          <button
            onClick={() => void trainModel()}
            disabled={loading}
            className="flex-1 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-xs font-semibold transition-colors disabled:opacity-50"
          >
            训练
          </button>
          <button
            onClick={() => void predict()}
            disabled={loading}
            className="flex-1 py-2 bg-amber-500 hover:bg-amber-400 text-black rounded-lg text-xs font-semibold transition-colors disabled:opacity-50"
          >
            预测
          </button>
        </div>

        {message && <p className="text-xs text-[#00C087]">{message}</p>}
        {error && <p className="text-xs text-[#FF4D4D]">{error}</p>}

        {/* 已保存模型 */}
        <div className="pt-3 border-t border-[#2A2D35]">
          <p className="text-[10px] font-mono uppercase tracking-wider text-[#8E9299] mb-2">
            已保存模型
          </p>
          {models.length === 0 ? (
            <p className="text-xs text-[#4A4D55]">暂无模型</p>
          ) : (
            <div className="flex flex-col gap-1">
              {models.map((m) => (
                <p key={m} className="text-xs text-[#8E9299] font-mono">
                  {m}
                </p>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ── 右侧：预测结果 ────────────────────────── */}
      <div className="flex-1 bg-[#151619] border border-[#2A2D35] rounded-xl p-6">
        {prediction ? (
          <div>
            <p className="text-sm font-semibold text-white mb-5">
              预测结果
              <span className="ml-2 text-xs text-[#8E9299] font-normal">
                ({prediction.count} 个信号)
              </span>
            </p>
            <div className="flex flex-wrap gap-2">
              {prediction.predictions.map((s, i) => (
                <div
                  key={i}
                  className={cn(
                    "px-3 py-1.5 rounded-lg bg-[#1C1E22] text-xs font-mono font-bold",
                    signalColor(s),
                  )}
                >
                  [{i}] {signalLabel(s)}
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="flex items-center justify-center h-full text-[#4A4D55] text-sm">
            输入 K 线数据后点击「训练」或「预测」
          </div>
        )}
      </div>
    </div>
  );
}
