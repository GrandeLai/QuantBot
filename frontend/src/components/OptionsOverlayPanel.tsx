import { useEffect, useState } from "react";
import { optionsRequest, overlayPortfolio } from "../api/optionsOverlay";
import type { OverlayPosition, OverlayReport, OverlayResult } from "../api/optionsOverlay";

const names: Record<string, string> = { hold: "直接持股", covered_call: "备兑看涨", protective_put: "保护性看跌", collar: "领口" };
const colors = ["#94a3b8", "#34d399", "#60a5fa", "#c084fc", "#fbbf24", "#fb7185"];
const initialPolicy = { min_dte: 14, target_dte: 30, max_dte: 45, roll_dte: 3, call_moneyness: 1.05,
  put_moneyness: 0.95, coverage_fraction: 1, max_premium_fraction: 0.05, max_spread_fraction: 0.3,
  commission_per_contract: 0.65, liquidate_at_end: false };
const money = (n: number | null | undefined) => n == null ? "—" : n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const percent = (n: number | null | undefined) => n == null ? "—" : `${(n * 100).toFixed(2)}%`;
function label(result: OverlayResult) {
  return names[result.strategy] + (result.assignment_scenario === "ex_dividend_stress" ? " · 提前指派压力" : "");
}

export default function OptionsOverlayPanel() {
  const [data, setData] = useState<Record<string, unknown> | null>(null);
  const [file, setFile] = useState("");
  const [cash, setCash] = useState("5000");
  const [positions, setPositions] = useState<OverlayPosition[]>([{ symbol: "", quantity: "100" }]);
  const [policy, setPolicy] = useState(initialPolicy);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [report, setReport] = useState<OverlayReport | null>(null);
  const [selected, setSelected] = useState(0);
  useEffect(() => { setReport(null); }, [data, cash, positions, policy]);
  const field = "w-full rounded border border-[#303844] bg-[#10141c] px-3 py-2 text-sm";
  async function loadExample() {
    setBusy(true); setError("");
    try {
      const sample = await optionsRequest<{ data: Record<string, unknown>; portfolio: { cash: number; positions: Record<string, number> }; policy: Partial<typeof initialPolicy> }>("/example");
      setData(sample.data); setFile("合成样例 · 45 个交易日"); setCash(String(sample.portfolio.cash));
      setPositions(Object.entries(sample.portfolio.positions).map(([symbol, quantity]) => ({ symbol, quantity: String(quantity) })));
      setPolicy({ ...initialPolicy, ...sample.policy }); setReport(null);
    } catch (e) { setError(e instanceof Error ? e.message : "样例读取失败"); }
    finally { setBusy(false); }
  }
  async function readFile(chosen?: File) {
    if (!chosen) return;
    setError(""); setData(null); setFile(""); setReport(null);
    try {
      if (chosen.size > 32000000) throw new Error("文件超过 32 MB");
      const parsed = JSON.parse(await chosen.text());
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("需要完整的历史期权数据包");
      setData(parsed); setFile(chosen.name);
    } catch (e) { setError(e instanceof Error ? e.message : "文件无法读取"); }
  }
  async function run() {
    if (!data) return;
    setBusy(true); setError(""); setReport(null);
    try {
      const result = await optionsRequest<OverlayReport>("/backtest", { data, portfolio: overlayPortfolio(cash, positions), policy });
      setReport(result); setSelected(0);
    } catch (e) { setError(e instanceof Error ? e.message : "回测失败"); }
    finally { setBusy(false); }
  }
  const chosen = report?.results[selected];
  const last = chosen?.equity_curve.at(-1);
  const allEquities = report?.results.flatMap(r => r.equity_curve.map(x => x.equity)) ?? [];
  const low = allEquities.length ? Math.min(...allEquities) : 0, high = allEquities.length ? Math.max(...allEquities) : 0;
  const days = Math.max(1, ...(report?.results.map(r => r.equity_curve.length - 1) ?? []));
  const numberField = (key: Exclude<keyof typeof initialPolicy, "liquidate_at_end">, title: string, scale = 1, step = 1) =>
    <label className="block text-xs space-y-2" key={key}><span>{title}</span><input className={field} aria-label={title} type="number" step={step}
      value={Number((policy[key] * scale).toFixed(5))} onChange={e => setPolicy({ ...policy, [key]: Number(e.target.value) / scale })}/></label>;
  return <section className="rounded-xl border border-[#303844] bg-[#161b22] p-5 space-y-5" aria-label="持仓期权研究">
    <div><h2 className="text-lg font-semibold">持仓期权研究</h2><p className="mt-1 text-sm text-[#a4b0bf]">用相同持仓对比直接持股、备兑看涨、保护性看跌和领口，并单列除息前提前指派压力情景。</p></div>
    <div className="flex flex-wrap items-center gap-3">
      <label className="cursor-pointer rounded-lg border border-dashed border-[#465265] px-4 py-3 text-sm">{file || "导入历史期权数据"}
        <input type="file" accept=".json" aria-label="历史期权数据" className="hidden" disabled={busy} onChange={e => { void readFile(e.target.files?.[0]); }}/></label>
      <button className="text-sm text-emerald-300 underline disabled:opacity-40" onClick={loadExample} disabled={busy}>加载合成样例</button>
    </div>
    <p className="text-xs text-[#a4b0bf]">数据包需包含原始股价、合约规格、历史买卖报价、可得时间及分红。实时期权链不能替代历史报价；股票拆分和调整合约暂未支持。</p>
    {data && <p className="text-sm text-amber-200">{data.data_kind === "synthetic" ? "当前使用合成数据，仅验证流程和账本。" : "当前使用导入历史数据，来源和发布时间尚未独立认证。"} 来源：{String(data.source ?? "待校验")}</p>}
    <fieldset disabled={busy} className="space-y-4">
      <legend className="mb-3 text-sm font-medium">已有持仓 · 按首日收盘价计入初始权益</legend>
      <label className="block max-w-xs text-xs space-y-2">初始现金（美元）<input className={field} type="number" min="0" aria-label="期权初始现金" value={cash} onChange={e => setCash(e.target.value)}/></label>
      {positions.map((row, i) => <div key={i} className="flex items-end gap-3">
        <label className="flex-1 text-xs space-y-2">股票标的<input className={field} aria-label={`期权持仓标的 ${i + 1}`} value={row.symbol} onChange={e => setPositions(positions.map((r, j) => j === i ? { ...r, symbol: e.target.value } : r))}/></label>
        <label className="flex-1 text-xs space-y-2">股数<input className={field} type="number" min="1" step="1" aria-label={`期权持仓股数 ${i + 1}`} value={row.quantity} onChange={e => setPositions(positions.map((r, j) => j === i ? { ...r, quantity: e.target.value } : r))}/></label>
        <button className="px-2 py-2 text-sm text-[#a4b0bf] disabled:opacity-30" disabled={positions.length === 1} onClick={() => setPositions(positions.filter((_, j) => i !== j))}>移除</button>
      </div>)}
      <button className="text-xs text-emerald-300" disabled={positions.length >= 20} onClick={() => setPositions([...positions, { symbol: "", quantity: "100" }])}>＋ 添加持仓</button>
      <div className="grid sm:grid-cols-3 gap-4">
        {numberField("target_dte", "目标剩余天数")}{numberField("call_moneyness", "看涨行权价 / 股价（%）", 100, 1)}{numberField("put_moneyness", "看跌行权价 / 股价（%）", 100, 1)}
        {numberField("coverage_fraction", "持仓覆盖比例（%）", 100, 1)}{numberField("max_premium_fraction", "单日新付权利金 / 权益（%）", 100, 0.5)}{numberField("roll_dte", "剩余几天开始换月")}
      </div>
      <details><summary className="cursor-pointer text-sm text-[#a4b0bf]">成交与到期设置</summary><div className="grid sm:grid-cols-2 gap-4 mt-4">
        {numberField("min_dte", "最短剩余天数")}{numberField("max_dte", "最长剩余天数")}
        {numberField("max_spread_fraction", "买卖价差 / 中间价上限（%）", 100, 1)}{numberField("commission_per_contract", "每份单边佣金（美元）", 1, 0.01)}
      </div><label className="mt-4 flex gap-2 text-sm"><input type="checkbox" checked={policy.liquidate_at_end} onChange={e => setPolicy({ ...policy, liquidate_at_end: e.target.checked })}/>预定在最后一天收盘平掉剩余期权；无法成交时报告失败</label></details>
    </fieldset>
    <button className="rounded-lg bg-emerald-500 px-5 py-2.5 text-sm font-medium text-[#071a13] disabled:opacity-40" disabled={busy || !data} onClick={run}>{busy ? "正在验证与回放…" : "对比四种持仓方案"}</button>
    {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
    {report && <div className="space-y-4 border-t border-[#303844] pt-5">
      <div className="flex flex-wrap items-center justify-between gap-3"><h3 className="font-medium">{report.execution_completed ? "六组回放完成" : "部分回放未完成"} · {report.data_quality.sessions} 个交易日</h3><a href={report.download_url} download className="text-sm text-emerald-300 underline">下载完整报告与输入</a></div>
      <p className="text-xs text-amber-200">{report.data_quality.data_kind === "synthetic" ? "合成样例不能证明策略有效。" : "尚未进行独立留出检验，策略有效性未评定。"} 模型调用 {report.model_calls} 次 · 券商调用 {report.broker_calls} 次</p>
      {allEquities.length > 1 && <svg viewBox="0 0 640 180" role="img" aria-label="持仓期权方案权益对比" className="w-full max-h-52 rounded-lg bg-[#10141c]">
        <text x="8" y="16" fill="#94a3b8" fontSize="11">{money(high)}</text><text x="8" y="174" fill="#94a3b8" fontSize="11">{money(low)}</text>
        {report.results.map((r, i) => <polyline key={i} fill="none" stroke={colors[i]} strokeWidth={i === selected ? 3 : 1.5} strokeDasharray={i >= 4 ? "5 4" : undefined}
          points={r.equity_curve.map((p, j) => `${10 + j * 620 / days},${155 - (p.equity - low) * 125 / Math.max(high - low, 1)}`).join(" ")}/>)}
      </svg>}
      <div className="overflow-x-auto"><table className="w-full whitespace-nowrap text-left text-xs"><thead className="text-[#8b949e]"><tr><th className="py-3">方案</th><th>期末权益</th><th>收益</th><th>超额（百分点）</th><th>最大回撤</th><th>状态</th></tr></thead><tbody>
        {report.results.map((r, i) => <tr className={i === selected ? "bg-[#202a37]" : ""} key={i}><td className="py-3 pr-4"><button onClick={() => setSelected(i)} style={{ color: colors[i] }}>{label(r)}</button></td><td>{money(r.final_equity)}</td><td>{percent(r.return_fraction)}</td><td>{r.excess_return_vs_hold == null ? "—" : (r.excess_return_vs_hold * 100).toFixed(2)}</td><td>{percent(r.max_drawdown_fraction)}</td><td>{r.execution_completed ? "完成" : "失败"}</td></tr>)}
      </tbody></table></div>
      {chosen && <div className="rounded-lg bg-[#10141c] p-4 space-y-3 text-sm">
        <h4 className="font-medium">{label(chosen)} · 账本与持仓</h4>
        <p>期权成交 {chosen.option_trades} 笔 · 指派 {chosen.assignments} 次 · 总佣金 {money(chosen.fees)} · 收取权利金 {money(chosen.opening_premium_received)} · 支付权利金 {money(chosen.opening_premium_paid)}</p>
        {last && <><p>截至 {last.date}：股票损益 {money(last.stock_pnl)} ＋ 期权损益 {money(last.option_pnl)} ＋ 分红 {money(last.dividend_income)} ＋ 费用 {money(last.fees)} ＝ 总损益 {money(last.equity - chosen.initial_equity)}</p>
          <p>现金 {money(last.cash)} · 已结算可用 {money(last.buying_power)} · 应收分红 {money(last.dividend_receivable)} · Delta 美元敞口 {money(last.greeks.dollar_delta)}</p>
          <p className="text-xs text-[#8b949e]">{last.greeks.basis}</p></>}
        <p>期末股票：{Object.entries(chosen.ending_positions.stocks).map(([s, q]) => `${s} ${q} 股`).join("；")}。剩余期权 {Object.keys(chosen.ending_positions.options).length} 个合约。</p>
        {chosen.errors.map((e, i) => <p role="alert" className="text-red-300" key={i}>{e.date}：{e.error}</p>)}
        <details><summary className="cursor-pointer text-[#a4b0bf]">交易与拒绝记录（{chosen.events.length} 条）</summary><div className="max-h-64 overflow-auto mt-3 space-y-2 text-xs">{chosen.events.map((e, i) => <p key={i}>{e.at ?? e.date} · {e.type} {e.contract_id ?? ""} {e.action ?? ""} {e.reason ?? ""}</p>)}</div></details>
      </div>}
      <p className="text-xs leading-relaxed text-[#8b949e]">{report.evidence_boundary}</p>
    </div>}
  </section>;
}
