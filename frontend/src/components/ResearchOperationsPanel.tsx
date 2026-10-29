import { useEffect, useState } from "react";
import { operationsRequest, type ResearchOperations } from "../api/researchOps";
import { conclusionLabels } from "../api/research";

const labels: Record<string, string> = { queued: "等待计划执行", running: "运行记录未结束", completed: "运行完成", failed: "运行失败",
  interrupted: "中断，未重试", canceled: "已取消", skipped: "已由其他入口处理", registered: "已冻结方案" };
const when = (seconds: number | null) => seconds === null ? "—" : new Date(seconds * 1000).toLocaleString();
const duration = (seconds: number | null) => seconds === null ? "—" : seconds < 60 ? `${Math.round(seconds)} 秒` : `${(seconds / 60).toFixed(1)} 分钟`;
const button = "rounded-lg border border-[#3b4654] px-3 py-2 text-sm hover:bg-[#29323c] disabled:opacity-40";

export default function ResearchOperationsPanel() {
  const [data, setData] = useState<ResearchOperations | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let alive = true;
    async function poll() {
      try { const current = await operationsRequest<ResearchOperations>(); if (alive) setData(current); }
      catch (e) { if (alive) setError(String(e)); }
    }
    void poll(); const timer = setInterval(() => void poll(), 5000);
    return () => { alive = false; clearInterval(timer); };
  }, []);
  async function act(path: string) {
    setBusy(true); setError("");
    try { await operationsRequest(path, {}); setData(await operationsRequest<ResearchOperations>()); }
    catch (e) { setError(e instanceof Error ? e.message : "请求失败"); }
    finally { setBusy(false); }
  }
  return <section className="space-y-4 rounded-xl border border-[#303844] bg-[#161b22] p-4" aria-label="研究运行看板">
    <div className="flex flex-wrap items-center justify-between gap-3"><h3 className="font-medium">研究运行看板</h3>
      <span className="text-xs text-[#a4b0bf]">{data?.worker_online ? "研究队列服务在线" : "研究队列服务未启动或心跳过期"} · 并发上限 {data?.concurrency_limit ?? 1}</span></div>
    {data && <>
      <div className="grid gap-3 text-sm sm:grid-cols-4">
        <div>已完成研究 <strong>{data.study_counts.completed || 0}</strong><p className="text-xs text-[#a4b0bf]">最近完成：{when(data.latest_success)}</p></div>
        <div>失败研究 <strong>{data.study_counts.failed || 0}</strong><p className="text-xs text-[#a4b0bf]">最近失败：{when(data.latest_failure)}</p></div>
        <div>等待计划 <strong>{data.queue_counts.queued || 0}</strong><p className="text-xs text-[#a4b0bf]">等待及运行计划最多 {data.reserved_model_requests} 次模型请求</p></div>
        <div>AI 决策尝试 <strong>{data.model_usage.decision_calls}</strong><p className="text-xs text-[#a4b0bf]">已报告 {data.model_usage.reported_tokens ?? "未知"} tokens；{data.model_usage.missing_usage_calls} 次缺用量，{data.model_usage.unresolved_calls} 次结果未确认，{data.model_usage.unknown_trials || 0} 个试验次数未知</p></div>
      </div>
      <p className="text-xs text-[#a4b0bf]">运行完成只表示流程完成；研究是否达标需查看独立结论。当前磁盘可用 {(data.disk.free_bytes / 1024 ** 3).toFixed(1)} GiB。</p>
      <div className="overflow-auto"><table className="w-full min-w-[850px] text-left text-xs"><thead><tr><th>研究计划</th><th>状态</th><th>计划时间</th><th>实际开始</th><th>延迟 / 用时</th><th>操作</th></tr></thead>
        <tbody>{data.queue.map(q => <tr key={q.id} className="border-t border-[#303844]"><td className="py-3">{q.title}{q.error && <p className="max-w-72 text-amber-200">{q.error}</p>}</td><td>{labels[q.status] || q.status}</td>
          <td>{when(q.planned)}</td><td>{when(q.started)}</td><td>{duration(q.delay_seconds)} / {duration(q.duration_seconds)}</td>
          <td>{q.status === "queued" && <button className={button} disabled={busy} onClick={() => void act(`/research-queue/${q.id}/cancel`)}>取消计划</button>}</td></tr>)}</tbody></table></div>
      {!data.queue.length && <p className="text-sm text-[#a4b0bf]">尚无排队计划。冻结研究后可选择计划时间。</p>}
      <button className={button} disabled={busy || !data.queue.some(q => q.status === "queued" && q.planned <= data.as_of)} onClick={() => void act("/research-queue/run")}>执行一个到期研究</button>
      <p className="text-xs text-[#a4b0bf]">此操作使用排队时记录的模型授权。持续执行计划需单独启动研究队列服务；网页轮询仅查看状态。</p>
      <details><summary className="cursor-pointer text-sm">最近研究的数据与结果（最多 50 项）</summary>
        <div className="overflow-auto"><table className="mt-2 w-full min-w-[650px] text-left text-xs"><thead><tr><th>研究</th><th>结论</th><th>试验 / 失败</th><th>历史数据截止</th><th>数据版本</th></tr></thead>
          <tbody>{data.studies.map(s => <tr key={s.id} className="border-t border-[#303844]"><td className="py-3">{s.title}</td><td>{conclusionLabels[s.conclusion || "not_assessed"]}</td><td>{s.trials} / {s.failed_trials}</td>
            <td>{new Date(s.data_quality.end).toLocaleString()}</td><td>{s.data_quality.data_kind === "synthetic" ? "合成 · " : ""}{s.data_quality.rows} 行 · {s.data_quality.availability_enforced ? `${s.data_quality.revision_count || 0} 次修订，按发布时间过滤` : "发布时间未验证"}</td></tr>)}</tbody></table></div>
      </details>
      <p className="text-xs text-[#a4b0bf]">{data.basis}</p>
    </>}
    {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
  </section>;
}
