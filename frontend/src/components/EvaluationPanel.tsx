import { useState } from "react";

type Usage = { provider_calls: number; reported_tokens: number | null; usage_missing_calls?: number };
type Metrics = Usage & { return_fraction: number | null; max_drawdown_fraction: number | null;
  fills: number; errors: unknown[]; risk_holds?: number };
type Comparison = { job_id: string; name: string; candidate: Metrics; passed: boolean; passed_folds: number };
type Fold = { fold: number; selection_start: string; selection_end: string; test_start: string;
  test_end: string; selected_job_id: string | null; candidate: Metrics; passed: boolean; checks: Record<string, boolean> };
export type Evaluation = { kind?: string; passed: boolean; baseline_name: string; candidate: Metrics;
  baseline: Metrics; usage?: Usage; comparisons?: Comparison[]; folds?: Fold[];
  protocol?: { folds: number; selection_bars: number; test_bars: number; gap_bars: number } };
type Job = { id: string; stage: string; spec: { name: string; provider: string; market: string;
  symbols: string[]; source: string; data_path: string; bar: string };
  config: { llm: { model: string; fallback_models?: string[] }; execution: { initial_cash: number } } };
type RollingRequest = { job_ids: string[]; selection_bars: number; test_bars: number; folds: number;
  gap_bars: number; allow_model_calls: boolean };

const percent = (value: number | null) => value === null ? "无有效窗口" : `${(value * 100).toFixed(2)}%`;
const stamp = (value: string) => new Date(value).toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
const checks: Record<string, string> = { complete_window: "区间不完整", complete_decisions: "决策不完整",
  no_model_errors: "模型错误", no_risk_hold: "触发账户熔断" };
const inputClass = "w-full rounded-lg border border-[#303844] bg-[#10141c] px-3 py-2 text-sm";

export default function EvaluationPanel({ job, jobs, report, busy, onEvaluate }: {
  job: Job; jobs: Job[]; report?: Evaluation; busy: boolean; onEvaluate: (body: RollingRequest) => void;
}) {
  const [chosen, setChosen] = useState([job.id]);
  const [selectionBars, setSelectionBars] = useState(30);
  const [testBars, setTestBars] = useState(10);
  const [folds, setFolds] = useState(3);
  const [gap, setGap] = useState(1);
  const [allowModels, setAllowModels] = useState(false);
  const compatible = jobs.filter(j => j.stage !== "paper" && j.spec.market === job.spec.market
    && j.spec.bar === job.spec.bar && j.spec.source === job.spec.source && j.spec.data_path === job.spec.data_path
    && j.spec.symbols.join(",") === job.spec.symbols.join(",")
    && j.config.execution.initial_cash === job.config.execution.initial_cash);
  const selected = compatible.filter(j => chosen.includes(j.id));
  const modelRoutes = selected.reduce((sum, j) => sum + (j.spec.provider === "ai" ? 1 + (j.config.llm.fallback_models?.length || 0) : 0), 0);
  const required = selectionBars + gap + folds * testBars;
  const rolling = report?.kind === "walk_forward";
  const usage = report?.usage || report?.candidate;
  const labels = Object.fromEntries((report?.comparisons || []).map(c => [c.job_id, c.name]));
  return <div className="space-y-4 border-t border-[#303844] pt-4">
    {job.stage !== "paper" && <details open className="rounded-lg bg-[#10141c] p-4">
      <summary className="cursor-pointer font-medium">滚动样本外评估与版本对比</summary>
      <p className="mt-2 text-xs text-[#a4b0bf]">在较早的参考窗口比较固定版本，再用后续窗口检验；测试窗口互不重叠。每折按参考收益选择版本，完成后保留报告供复盘。</p>
      <fieldset disabled={busy} className="mt-3 space-y-2">
        <legend className="mb-2 text-sm">参与对比的候选（最多 6 个）</legend>
        {compatible.map(j => <label className="flex items-center gap-2 text-sm" key={j.id}>
          <input type="checkbox" aria-label={`对比 ${j.spec.name}`} checked={chosen.includes(j.id)}
            disabled={j.id === job.id || (!chosen.includes(j.id) && selected.length >= 6)}
            onChange={e => { setChosen(e.target.checked ? [...chosen, j.id] : chosen.filter(id => id !== j.id)); setAllowModels(false); }}/>
          {j.spec.name} · {j.spec.provider === "ai" ? "AI" : "规则"}
        </label>)}
        <div className="grid grid-cols-2 gap-3 pt-2 sm:grid-cols-4">
          <label className="text-sm">参考根数<input aria-label="滚动参考根数" type="number" min="3" max="200" className={inputClass} value={selectionBars} onChange={e=>setSelectionBars(Number(e.target.value))}/></label>
          <label className="text-sm">测试根数<input aria-label="滚动测试根数" type="number" min="3" max="100" className={inputClass} value={testBars} onChange={e=>setTestBars(Number(e.target.value))}/></label>
          <label className="text-sm">滚动折数<input aria-label="滚动折数" type="number" min="2" max="10" className={inputClass} value={folds} onChange={e=>setFolds(Number(e.target.value))}/></label>
          <label className="text-sm">间隔根数<input aria-label="滚动间隔根数" type="number" min="0" max="100" className={inputClass} value={gap} onChange={e=>setGap(Number(e.target.value))}/></label>
        </div>
        <p className="text-xs text-[#a4b0bf]">需要 {required} 根完成特征预热后的行情（最多 500 根）。同一批次共用冻结行情、初始资金和执行成本。</p>
        {modelRoutes > 0 && <label className="flex items-start gap-2 text-sm text-amber-200">
          <input className="mt-1" type="checkbox" checked={allowModels} onChange={e=>setAllowModels(e.target.checked)}/>
          允许所有所选版本调用模型；最多 {folds * (selectionBars + testBars - 2) * modelRoutes} 次请求（含备用模型），可能产生费用。
        </label>}
        <button className="mt-2 rounded-lg border border-emerald-600 px-3 py-2 text-sm text-emerald-300 disabled:opacity-40"
          disabled={busy || !selected.length || required > 500 || (modelRoutes > 0 && !allowModels)}
          onClick={()=>onEvaluate({ job_ids: selected.map(j=>j.id), selection_bars: selectionBars, test_bars: testBars,
            folds, gap_bars: gap, allow_model_calls: allowModels })}>运行滚动对比</button>
      </fieldset>
    </details>}
    {report && <div className="space-y-3">
      <h3 className="text-sm font-medium">最近{rolling ? "滚动样本外" : "基础回测"}评估 · {report.passed ? "运行完整性检查通过" : "运行完整性检查未通过"} · 研究结论未评定</h3>
      {rolling && <p className="text-xs text-[#a4b0bf]">{report.protocol?.folds} 折，每折参考 {report.protocol?.selection_bars} 根、间隔 {report.protocol?.gap_bars} 根、测试 {report.protocol?.test_bars} 根。以下为独立账户的测试窗口平均收益及最差窗口回撤；期末持仓按市值计价，未拼接为复利曲线。</p>}
      <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead><tr>
        <th>版本 / 基线</th><th>{rolling ? "窗口平均收益" : "收益"}</th><th>{rolling ? "最差窗口回撤" : "最大回撤"}</th><th>成交</th><th>检查</th>
      </tr></thead><tbody>
        {(rolling ? report.comparisons || [] : [{job_id: job.id, name: "候选版本", candidate: report.candidate, passed: report.passed, passed_folds: 0}]).map(c => <tr key={c.job_id} className={c.job_id === job.id ? "text-emerald-200" : ""}>
          <td className="py-2">{c.name}</td><td>{percent(c.candidate.return_fraction)}</td><td>{percent(c.candidate.max_drawdown_fraction)}</td><td>{c.candidate.fills}</td>
          <td>{c.passed ? "运行完整" : "运行不完整"}{rolling ? `（${c.passed_folds}/${report.protocol?.folds}）` : ""}</td>
        </tr>)}
        <tr><td className="py-2">{report.baseline_name === "price_action" ? "价格行为规则" : "动量规则"}</td><td>{percent(report.baseline.return_fraction)}</td><td>{percent(report.baseline.max_drawdown_fraction)}</td><td>{report.baseline.fills}</td><td>对照</td></tr>
        <tr><td className="py-2">现金</td><td>0.00%</td><td>0.00%</td><td>0</td><td>对照</td></tr>
      </tbody></table></div>
      {rolling && <p className="text-xs text-[#8b949e]">规则基线沿用本批次首个版本「{report.comparisons?.[0]?.name}」的参数，所有版本使用同一规则基线。</p>}
      {usage && <p className="text-xs text-[#8b949e]">当前版本{rolling ? "含参考与测试窗口" : ""}：模型/规则调用 {usage.provider_calls} 次，供应方报告用量 {usage.reported_tokens === null ? "未提供" : `${usage.reported_tokens} tokens`}{usage.usage_missing_calls ? `；${usage.usage_missing_calls} 次未报告用量` : ""}。此处不是费用账单。</p>}
      {rolling && <details><summary className="cursor-pointer text-sm">逐折结果与准入原因</summary>
        {report.folds?.map(f => <div className="mt-3 rounded-lg bg-[#10141c] p-3 text-sm" key={f.fold}>
          <p>第 {f.fold} 折 · {f.passed ? "运行完整" : "运行不完整"} · 当前版本测试收益 {percent(f.candidate.return_fraction)}</p>
          <p className="mt-1 text-xs text-[#a4b0bf]">参考 {stamp(f.selection_start)} — {stamp(f.selection_end)}；测试 {stamp(f.test_start)} — {stamp(f.test_end)}</p>
          <p className="mt-1 text-xs text-[#a4b0bf]">依据参考窗口选择：{f.selected_job_id ? labels[f.selected_job_id] : "无合格版本"}</p>
          {Object.entries(f.checks).filter(([, passed])=>!passed).map(([key])=><p className="mt-1 text-xs text-amber-200" key={key}>{key.startsWith("selection_") ? "参考窗口" : "测试窗口"}：{checks[key.replace(/^(selection|test)_/, "")] || key}</p>)}
        </div>)}
      </details>}
      <p className="text-xs text-[#8b949e]">准入检查数据完整性、决策有效性与账户熔断，不以排名批准交易。历史时间隔离无法排除模型已有知识或人工事后选参的影响，不代表前瞻收益。</p>
    </div>}
  </div>;
}
