import { useEffect, useState } from "react";
import { conclusionLabels, researchChecks, researchRequest, runResearch } from "../api/research";
import type { ResearchCard, ResearchMetrics, ResearchStudy, ResearchSummary } from "../api/research";
import { queueResearch } from "../api/researchOps";

type Job = { id: string; stage: string; spec: { name: string; provider: string } };
const input = "w-full rounded-lg border border-[#303844] bg-[#10141c] px-3 py-2 text-sm";
const button = "rounded-lg border border-[#3b4654] px-3 py-2 text-sm hover:bg-[#29323c] disabled:opacity-40";
const percent = (value: number) => `${(value * 100).toFixed(2)}%`;
const states: Record<string, string> = { registered: "方案已冻结", running: "研究中", completed: "运行完成", failed: "运行未完成" };
const stages: Record<string, string> = { research_registered: "登记方案", research_development: "开发窗口验证",
  research_selection: "冻结候选选择", research_robustness: "稳健性验证", research_finished: "归档结论" };
const scenarios: Record<string, string> = { cost: "成本提高", weight_lower: "仓位上限降低", weight_higher: "仓位上限提高" };
const protocolFields: [string, string, number, number, number, number][] = [
  ["development_bars", "每个开发窗口根数", 30, 3, 200, 1], ["folds", "开发窗口数量", 3, 2, 10, 1],
  ["gap_bars", "留出前间隔根数", 1, 1, 100, 1], ["holdout_bars", "最终留出根数", 40, 4, 200, 1],
  ["min_observations", "最少收益样本数", 30, 3, 500, 1], ["model_request_budget", "模型请求次数上限", 500, 0, 10000, 1],
  ["min_return", "最低收益（小数）", 0, 0, 10, 0.001], ["min_excess_return", "最低超额收益（小数）", 0.001, 0, 10, 0.001],
  ["max_drawdown", "最大回撤（小数）", 0.1, 0.001, 0.99, 0.01], ["min_dsr", "DSR 最低置信度", 0.95, 0.5, 1, 0.01],
  ["max_pbo", "PBO 最高概率", 0.5, 0, 0.5, 0.01], ["cost_multiplier", "成本压力倍数", 2, 1.1, 5, 0.1],
  ["weight_perturbation", "仓位扰动比例", 0.1, 0.01, 0.5, 0.01],
];

export default function ResearchPanel({ jobs, selectedJobId }: { jobs: Job[]; selectedJobId: string }) {
  const [studies, setStudies] = useState<ResearchSummary[]>([]);
  const [study, setStudy] = useState<ResearchStudy | null>(null);
  const [chosen, setChosen] = useState<string[]>([]);
  const [card, setCard] = useState<ResearchCard>({ title: "", hypothesis: "", rationale: "", failure_condition: "" });
  const [protocol, setProtocol] = useState<Record<string, number>>(() => Object.fromEntries(protocolFields.map(([k, , v]) => [k, v])));
  const [busy, setBusy] = useState(false);
  const [allowModels, setAllowModels] = useState(false);
  const [synthetic, setSynthetic] = useState(false);
  const [plannedAt, setPlannedAt] = useState("");
  const [queueNotice, setQueueNotice] = useState("");
  const [error, setError] = useState("");
  useEffect(() => { setChosen(selectedJobId ? [selectedJobId] : []); }, [selectedJobId]);
  async function refresh() { setStudies(await researchRequest<ResearchSummary[]>()); }
  useEffect(() => { void refresh().catch(e => setError(String(e))); }, []);
  useEffect(() => {
    if (!study || !["registered", "running"].includes(study.status)) return;
    let alive = true;
    const timer = setInterval(() => {
      void researchRequest<ResearchStudy>(`/${study.id}`).then(s => { if (alive) { setStudy(s); if (s.status !== study.status) void refresh().catch(e => setError(String(e))); } })
        .catch(e => { if (alive) setError(String(e)); });
    }, 3000);
    return () => { alive = false; clearInterval(timer); };
  }, [study?.id, study?.status]);
  async function act(operation: () => Promise<ResearchStudy>) {
    setBusy(true); setError("");
    try { setStudy(await operation()); await refresh(); }
    catch (e) {
      setError(e instanceof Error ? e.message : "研究失败");
      if (study) {
        try { setStudy(await researchRequest(`/${study.id}`)); await refresh(); }
        catch (refreshError) { setError(previous => `${previous}；读取研究记录失败：${String(refreshError)}`); }
      }
    }
    finally { setBusy(false); }
  }
  async function download() {
    if (!study) return;
    try {
      const full = await researchRequest(`/${study.id}/export`);
      const url = URL.createObjectURL(new Blob([JSON.stringify(full, null, 2)], { type: "application/json" }));
      const link = document.createElement("a"); link.href = url; link.download = `quantbot-research-${study.id}.json`;
      link.click(); URL.revokeObjectURL(url);
    } catch (e) { setError(String(e)); }
  }
  const report = study?.report;
  const rows: { name: string; metrics: ResearchMetrics }[] = report?.holdout && report.baseline ? [
    { name: "最终留出：选定候选", metrics: report.holdout }, { name: "规则基准", metrics: report.baseline },
    ...(report.stress || []).map(r => ({ name: scenarios[r.scenario], metrics: r })),
    ...(report.placebos || []).map(r => ({ name: `随机对照 ${r.seed}`, metrics: r })),
  ] : [];
  const curve = report?.holdout?.equity_curve || [];
  const low = Math.min(...curve.map(p => p.equity)), high = Math.max(...curve.map(p => p.equity));
  const required = protocol.development_bars * protocol.folds + protocol.gap_bars + protocol.holdout_bars;
  return <div className="space-y-4 rounded-xl border border-[#36584b] bg-[#141d1a] p-5">
    <div><h3 className="text-lg font-semibold">策略研究闭环</h3>
      <p className="mt-1 text-sm text-[#a4b0bf]">登记假设与淘汰标准 → 冻结数据 → 开发验证 → 一次留出 → 稳健性报告。</p></div>
    <details open={studies.length === 0}>
      <summary className="cursor-pointer text-sm font-medium">登记新研究</summary>
      <fieldset disabled={busy} className="mt-3 space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">{([
          ["title", "研究名称"], ["hypothesis", "想验证的假设"], ["rationale", "为什么可能有效"], ["failure_condition", "何时放弃这个假设"],
        ] as const).map(([key, label]) => <label key={key} className="text-sm">{label}<textarea aria-label={label}
          className={input} rows={key === "title" ? 1 : 2} maxLength={key === "title" ? 120 : 2000}
          value={card[key]} onChange={e => setCard({ ...card, [key]: e.target.value })}/></label>)}</div>
        <p className="text-xs text-[#a4b0bf]">文字记录研究意图；自动结论按下面冻结的数值标准判断。候选可来自不同模型或策略参数，但必须使用相同行情、资金、成本和账户熔断阈值。</p>
        <div className="flex flex-wrap gap-3">{jobs.filter(j => j.stage !== "paper").map(j => <label key={j.id} className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={chosen.includes(j.id)} disabled={!chosen.includes(j.id) && chosen.length >= 6}
            onChange={e => setChosen(e.target.checked ? [...chosen, j.id] : chosen.filter(id => id !== j.id))}/>{j.spec.name}
        </label>)}</div>
        {!jobs.length && <p className="text-sm text-amber-200">先创建候选任务，再登记研究。</p>}
        <details><summary className="cursor-pointer text-sm">窗口、预算与研究标准</summary>
          <div className="mt-3 grid gap-3 sm:grid-cols-3">{protocolFields.map(([key, label, , min, max, step]) => <label key={key} className="text-sm">{label}
            <input className={input} aria-label={label} type="number" min={min} max={max} step={step} value={protocol[key]}
              onChange={e => setProtocol({ ...protocol, [key]: Number(e.target.value) })}/></label>)}</div>
          <p className="mt-2 text-xs text-[#a4b0bf]">小数 0.10 表示 10%。收益和超额收益须严格超过门槛。随机对照使用固定种子 7、42、99。开发窗口各自重置资金，不拼接复利曲线。</p>
        </details>
        <p className="text-xs text-[#a4b0bf]">需要 {required} 根预热后的行情，另需特征预热历史。默认标准是可修改的研究起点，不构成盈利保证。</p>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={synthetic} onChange={e => setSynthetic(e.target.checked)}/>这是合成数据，仅验证流程</label>
        <button className={button} disabled={busy || !chosen.length || required > 500 || Object.values(card).some(v => !v.trim())}
          onClick={() => { setAllowModels(false); void act(() => researchRequest("", { job_ids: chosen, card, protocol, data_kind: synthetic ? "synthetic" : "historical_unverified" })); }}>冻结研究方案</button>
      </fieldset>
    </details>
    <label className="block text-sm">已有研究（包括失败与淘汰）<select className={input} disabled={busy} value={study?.id || ""}
      onChange={e => { if (e.target.value) { setQueueNotice(""); setAllowModels(false); void act(() => researchRequest(`/${e.target.value}`)); } }}>
      <option value="">选择研究</option>{studies.map(s => <option key={s.id} value={s.id}>{s.title} · {states[s.status]} · {conclusionLabels[s.conclusion]}</option>)}
    </select></label>
    {study && <div className="space-y-3 border-t border-[#36584b] pt-4">
      <p className="font-medium">{study.plan.card.title} · {states[study.status]}{report ? ` · ${conclusionLabels[report.conclusion]}` : ""}</p>
      <p className="text-sm">{study.plan.card.hypothesis}</p>
      <p className="text-sm text-amber-200">{study.plan.data_quality.data_kind === "synthetic" ? "合成数据：结果仅供流程验证" : "历史行情：来源声明尚未完成完整历史可得性验证"}</p>
      <p className="text-xs text-[#a4b0bf]">最终留出：{study.plan.windows.holdout.start} — {study.plan.windows.holdout.end}。登记后不能修改方案或重复运行。</p>
      <details><summary className="cursor-pointer text-sm">数据检查与冻结方案</summary>
        <p className="mt-2 text-sm">已通过行情结构、连续性与预热检查，共 {study.plan.data_quality.rows} 行。</p>
        {study.plan.data_quality.limitations.map(t => <p key={t} className="text-xs text-amber-200">{t}</p>)}
        <pre className="mt-2 max-h-64 overflow-auto text-xs">{JSON.stringify({ card: study.plan.card, protocol: study.plan.protocol, candidates: study.plan.candidates.map(c => c.spec.name) }, null, 2)}</pre>
      </details>
      {study.status === "registered" && <>
        {study.plan.max_model_requests > 0 && <label className="flex items-start gap-2 text-sm text-amber-200"><input type="checkbox" checked={allowModels} onChange={e => setAllowModels(e.target.checked)}/>
          允许此研究调用模型，最多 {study.plan.max_model_requests} 次请求（含备用模型）；压力测试重用决策，不再次调用模型。</label>}
        <button className={button} disabled={busy || (study.plan.max_model_requests > 0 && !allowModels)} onClick={() => {
          setStudy({ ...study, status: "running" }); void act(() => runResearch(study.id, allowModels));
        }}>执行已冻结研究</button>
        <div className="flex flex-wrap items-end gap-2"><label className="text-sm">计划时间（本机时区）<input type="datetime-local" aria-label="研究计划时间" className={input} value={plannedAt} onChange={e => setPlannedAt(e.target.value)}/></label>
          <button className={button} disabled={busy || !plannedAt || (study.plan.max_model_requests > 0 && !allowModels)} onClick={() => void act(async () => {
            await queueResearch(study.id, plannedAt, allowModels); setQueueNotice("已加入研究计划；运行情况见下方看板。"); return researchRequest(`/${study.id}`);
          })}>加入研究计划</button></div>
        {queueNotice && <p role="status" className="text-sm text-emerald-300">{queueNotice}</p>}
      </>}
      <p className="text-xs text-[#a4b0bf]">历史可得性：{study.plan.data_quality.availability_enforced ? `已按声明的发布时间过滤 ${study.plan.data_quality.revision_count ?? 0} 条修订；来源真实性尚未独立认证` : "普通行情快照，历史发布时间未验证"}。</p>
      {report && <>
        <p className="text-sm">运行结果：{report.execution_completed ? "全部试验完成" : "有未完成或失败试验"}。研究结论：{conclusionLabels[report.conclusion]}。</p>
        {report.error && <p role="alert" className="text-sm text-red-300">{report.error}</p>}
        {!!report.reasons.length && <ul className="list-disc pl-5 text-sm text-amber-200">{report.reasons.map(r => <li key={r}>{researchChecks[r] || r}：未满足</li>)}</ul>}
        {!!rows.length && <div className="overflow-auto"><table className="w-full text-left text-sm"><thead><tr><th>试验</th><th>收益</th><th>最大回撤</th><th>成交次数</th></tr></thead>
          <tbody>{rows.map(r => <tr key={r.name}><td className="py-2">{r.name}</td><td>{percent(r.metrics.return_fraction)}</td><td>{percent(r.metrics.max_drawdown_fraction)}</td><td>{r.metrics.fills}</td></tr>)}
            <tr><td>现金基准</td><td>0.00%</td><td>0.00%</td><td>0</td></tr></tbody></table></div>}
        {curve.length > 1 && <svg viewBox="0 0 600 130" role="img" aria-label="最终留出权益曲线" className="w-full max-h-40 rounded-lg bg-[#10141c]">
          <polyline points={curve.map((p, i) => `${10 + i * 580 / (curve.length - 1)},${110 - (p.equity - low) * 90 / Math.max(0.01, high - low)}`).join(" ")} fill="none" stroke="#34d399" strokeWidth="2"/>
          <text x="10" y="15" fill="#a4b0bf" fontSize="11">{high.toFixed(2)}</text><text x="10" y="125" fill="#a4b0bf" fontSize="11">{low.toFixed(2)}</text>
        </svg>}
        {report.diagnostics && <p className="text-xs text-[#a4b0bf]">候选 {report.diagnostics.trial_count} 个，留出收益样本 {report.diagnostics.observations} 个；DSR：{report.diagnostics.dsr.confidence === undefined ? report.diagnostics.dsr.reason : percent(report.diagnostics.dsr.confidence)}；PBO：{report.diagnostics.pbo.probability === undefined ? report.diagnostics.pbo.reason : percent(report.diagnostics.pbo.probability)}。</p>}
        {report.model_usage && <p className="text-xs text-[#a4b0bf]">AI 决策调用 {report.model_usage.provider_calls} 次；供应方报告用量 {report.model_usage.reported_tokens === null ? "未提供" : `${report.model_usage.reported_tokens} tokens`}。</p>}
      </>}
      <details><summary className="cursor-pointer text-sm">阶段与全部试验记录（{study.trials.length}）</summary>
        {study.events.map(e => <p key={e.id} className="mt-1 text-xs">{new Date(e.at * 1000).toLocaleString()} · {stages[e.kind] || e.kind}</p>)}
        {study.trials.map(t => <p key={t.key} className="mt-1 break-all text-xs">{t.key} · {t.status === "completed" ? "运行完成" : t.status === "failed" ? "运行失败" : "已开始"}{t.result?.error ? ` · ${t.result.error}` : ""}</p>)}
      </details>
      <p className="text-xs text-[#a4b0bf]">{study.plan.evidence_boundary}</p>
      <button className={button} onClick={() => void download()}>下载完整研究证据</button>
    </div>}
    {busy && <p role="status" className="text-sm text-[#a4b0bf]">正在处理研究任务；已完成的窗口会逐个保存。</p>}
    {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
  </div>;
}
