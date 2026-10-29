import { useEffect, useState } from "react";
import { parseEventHorizons, secStudyRequest } from "../api/secStudy";
import type { HistoricalIdentity, SECStudyReport } from "../api/secStudy";

type ImportedData = Record<string, unknown>;
const reasons: Record<string, string> = { amendment_excluded: "修订申报未纳入", public_availability_unknown: "缺少公开时间证据",
  item_filter: "条目不匹配", no_historical_symbol_mapping: "该时点无历史标的映射", outside_price_window: "行情未覆盖可得时点",
  overlapping_event_window: "与先前事件窗口重叠", incomplete_forward_window: "后续行情不足" };
const percent = (value: number | null) => value == null ? "—" : `${(value * 100).toFixed(2)}%`;
const points = (value: number | null) => value == null ? "—" : (value * 100).toFixed(2);

export default function SECEventStudyPanel() {
  const [bundle, setBundle] = useState<ImportedData | null>(null);
  const [prices, setPrices] = useState<ImportedData | null>(null);
  const [files, setFiles] = useState({ bundle: "", prices: "" });
  const [universe, setUniverse] = useState<HistoricalIdentity[]>([{ cik: "", symbol: "", start: "", end: "" }]);
  const [benchmark, setBenchmark] = useState("SPY");
  const [horizons, setHorizons] = useState("1,5,20");
  const [mode, setMode] = useState("observed");
  const [delay, setDelay] = useState(15);
  const [latency, setLatency] = useState(60);
  const [cost, setCost] = useState(10);
  const [items, setItems] = useState("");
  const [amendments, setAmendments] = useState(false);
  const [overlap, setOverlap] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [report, setReport] = useState<SECStudyReport | null>(null);
  useEffect(() => { setReport(null); }, [bundle, prices, universe, benchmark, horizons, mode, delay, latency, cost, items, amendments, overlap]);
  const field = "w-full rounded border border-[#303844] bg-[#10141c] px-3 py-2 text-sm";
  async function sample() {
    setBusy(true); setError("");
    try {
      const result = await secStudyRequest<{ bundle: ImportedData; prices: ImportedData; universe: HistoricalIdentity[]; policy: { horizons: number[]; benchmark: string } }>("/example");
      setBundle(result.bundle); setPrices(result.prices); setUniverse(result.universe);
      setFiles({ bundle: "跨季度合成 SEC 样例", prices: "合成股票与基准行情" });
      setHorizons(result.policy.horizons.join(",")); setBenchmark(result.policy.benchmark); setMode("observed");
      setDelay(15); setLatency(60); setCost(10); setItems(""); setAmendments(false); setOverlap(false);
    } catch (e) { setError(e instanceof Error ? e.message : "样例加载失败"); }
    finally { setBusy(false); }
  }
  async function readFile(kind: "bundle" | "prices", file?: File) {
    if (!file) return;
    setError(""); setReport(null);
    try {
      if (file.size > 64000000) throw new Error("输入文件超过 64 MB");
      const value = JSON.parse(await file.text());
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("数据包格式无效");
      if (kind === "bundle") setBundle(value); else setPrices(value);
      setFiles(previous => ({ ...previous, [kind]: file.name }));
    } catch (e) {
      if (kind === "bundle") setBundle(null); else setPrices(null);
      setError(e instanceof Error ? e.message : "文件读取失败");
    }
  }
  async function run() {
    setBusy(true); setError(""); setReport(null);
    try {
      const result = await secStudyRequest<SECStudyReport>("", { bundle, prices, universe,
        policy: { benchmark, horizons: parseEventHorizons(horizons), availability_mode: mode,
          assumed_public_delay_seconds: Math.round(delay * 60), order_latency_seconds: latency, cost_bps_per_side: cost,
          include_amendments: amendments, allow_overlapping: overlap, items: items ? items.split(",").map(s => s.trim()) : [] } });
      setReport(result);
    } catch (e) { setError(e instanceof Error ? e.message : "事件研究失败"); }
    finally { setBusy(false); }
  }
  function exportContext() {
    const input = { schema_version: 1, bundle, universe, policy: { availability_mode: mode,
      assumed_public_delay_seconds: Math.round(delay * 60), processing_delay_seconds: latency,
      lookback_days: 7, max_events_per_symbol: 3, max_text_chars: 2000, include_amendments: true } };
    const url = URL.createObjectURL(new Blob([JSON.stringify(input, null, 2)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = "quantbot-sec-context.json"; link.click(); URL.revokeObjectURL(url);
  }
  const exclusions = report?.skipped.reduce((counts, row) => ({ ...counts, [row.reason]: (counts[row.reason] ?? 0) + 1 }), {} as Record<string, number>);
  return <section className="rounded-xl border border-[#303844] bg-[#161b22] p-5 space-y-4" aria-label="SEC 历史事件研究">
    <h2 className="text-lg font-semibold">SEC 历史事件研究</h2>
    <p className="text-sm text-[#a4b0bf]">检查 8-K 可得之后的固定交易日窗口，对比股票与基准收益。保留正文修订，区分受理时间、首次取得时间和公开时间假设。</p>
    <details><summary className="cursor-pointer text-sm text-emerald-300">配置与运行事件研究</summary><div className="space-y-4 mt-4">
      <div className="flex flex-wrap gap-3">{(["bundle", "prices"] as const).map(kind => <label key={kind} className="cursor-pointer rounded border border-dashed border-[#465265] p-3 text-sm">{files[kind] || (kind === "bundle" ? "导入 SEC 归档数据" : "导入历史股价与基准")}
        <input aria-label={kind === "bundle" ? "SEC 归档数据" : "事件历史行情"} className="hidden" type="file" accept=".json" disabled={busy} onChange={e => { void readFile(kind, e.target.files?.[0]); }}/></label>)}
        <button className="text-sm underline text-emerald-300 disabled:opacity-40" onClick={sample} disabled={busy}>加载跨季度合成样例</button></div>
      {bundle && <p className="text-xs text-amber-200">{bundle.data_kind === "synthetic" || prices?.data_kind === "synthetic" ? "合成数据仅用于验证流程。" : "导入历史数据尚未通过独立可得性认证。"} 文件须包含完整原文和覆盖清单，缺失季度或采集失败不能当作无事件。</p>}
      <fieldset className="space-y-4" disabled={busy}><legend className="text-sm mb-3">历史公司与股票映射</legend>
        {universe.map((row, i) => <div className="grid grid-cols-2 sm:grid-cols-4 gap-3" key={i}>{(["cik", "symbol", "start", "end"] as const).map(key => <label className="text-xs space-y-2" key={key}>{({ cik: "公司 CIK", symbol: "历史股票代码", start: "映射开始日", end: "映射结束日" })[key]}
          <input className={field} aria-label={`事件映射 ${i + 1} ${key}`} type={key === "start" || key === "end" ? "date" : "text"} value={row[key]}
            onChange={e => setUniverse(universe.map((old, j) => i === j ? { ...old, [key]: e.target.value } : old))}/></label>)}</div>)}
        <div className="flex gap-4 text-xs text-emerald-300"><button onClick={() => setUniverse([...universe, { cik: "", symbol: "", start: "", end: "" }])} disabled={universe.length >= 100}>＋ 添加历史映射</button>{universe.length > 1 && <button onClick={() => setUniverse(universe.slice(0, -1))}>移除最后一项</button>}</div>
        <div className="grid sm:grid-cols-3 gap-4">
          <label className="text-xs space-y-2">基准标的<input className={field} aria-label="事件基准标的" value={benchmark} onChange={e => setBenchmark(e.target.value)}/></label>
          <label className="text-xs space-y-2">窗口交易日数<input className={field} aria-label="事件窗口交易日数" value={horizons} onChange={e => setHorizons(e.target.value)}/></label>
          <label className="text-xs space-y-2">单边成本（bps）<input className={field} type="number" min="0" max="500" aria-label="事件单边成本" value={cost} onChange={e => setCost(Number(e.target.value))}/></label>
          <label className="text-xs space-y-2">可得时间依据<select className={field} aria-label="事件可得时间依据" value={mode} onChange={e => setMode(e.target.value)}><option value="observed">本地首次取得时间</option><option value="declared">有证据声明的公开时间</option><option value="acceptance_proxy">受理时间＋假定公开延迟</option></select></label>
          <label className="text-xs space-y-2">假定公开延迟（分钟）<input className={field} type="number" min="1" disabled={mode !== "acceptance_proxy"} aria-label="假定公开延迟" value={delay} onChange={e => setDelay(Number(e.target.value))}/></label>
          <label className="text-xs space-y-2">获知后下单延迟（秒）<input className={field} type="number" min="1" aria-label="事件下单延迟" value={latency} onChange={e => setLatency(Number(e.target.value))}/></label>
        </div>
        <p className="text-xs text-amber-200">{mode === "observed" ? "现在下载的历史文件按现在首次取得计算；如行情只覆盖过去，可能没有合格事件窗口。" : mode === "declared" ? "没有公开时间或证据来源的事件会被排除。" : "受理时间代理是研究假设。SEC 不保证公开延迟，报告会保留此假设且不会标记为已验证的历史可得数据。"}</p>
        <label className="block text-xs space-y-2">条目筛选（可留空，例如 2.02,5.02）<input className={field} aria-label="SEC 条目筛选" value={items} onChange={e => setItems(e.target.value)}/></label>
        <div className="flex flex-wrap gap-5 text-sm"><label className="flex gap-2"><input type="checkbox" checked={amendments} onChange={e => setAmendments(e.target.checked)}/>将 8-K/A 作为单独事件</label><label className="flex gap-2"><input type="checkbox" checked={overlap} onChange={e => setOverlap(e.target.checked)}/>允许同标的窗口重叠</label></div>
      </fieldset>
      <button className="rounded-lg bg-emerald-500 px-5 py-2.5 text-sm font-medium text-[#071a13] disabled:opacity-40" onClick={run} disabled={busy || !bundle || !prices}>{busy ? "正在核对事件与行情…" : "运行历史事件研究"}</button>
      {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
    </div></details>
    {report && <div className="space-y-4 border-t border-[#303844] pt-4">
      <div className="flex flex-wrap items-center justify-between gap-3"><h3 className="font-medium">{report.execution_completed ? "事件回放完成" : "事件回放未完成"} · {report.data_quality.event_groups} 个事件组</h3><a href={report.download_url} download className="text-sm underline text-emerald-300">下载事件与行情完整报告</a></div>
      <div className="space-y-2"><button className="text-sm underline text-emerald-300" onClick={exportContext}>下载 AI 回测公告输入</button><p className="text-xs text-[#a4b0bf]">导出全部归档与历史映射，供上方主回测使用；不带入事件收益。默认回看 7 天、每个标的最多 3 份正文，纳入修订申报；沿用当前可得时间依据。</p></div>
      <p className="text-xs text-[#a4b0bf]">覆盖 {report.data_quality.quarter_count} 个季度 · {report.data_quality.accessions} 份申报 · 去重 {report.data_quality.duplicate_records} 条 · 保留 {report.data_quality.revision_versions} 个正文修订 · 模型 / 券商调用 {report.model_calls} / {report.broker_calls}</p>
      <p className="text-xs text-amber-200">{report.data_quality.data_kind === "synthetic" ? "合成样例不能用于证明事件策略有效。" : "这些是事件窗口描述统计，策略有效性尚未评定。"}</p>
      <div className="overflow-x-auto"><table className="w-full whitespace-nowrap text-xs text-left"><thead className="text-[#8b949e]"><tr><th className="py-3">交易日窗口</th><th>事件 / 日期组</th><th>平均净收益</th><th>基准收益</th><th>超额（百分点）</th><th>成本加倍净收益</th><th>按日期聚类 95% 区间</th></tr></thead><tbody>
        {report.summary.map(row => <tr key={row.sessions}><td className="py-3">{row.sessions} 日</td><td>{row.events} / {row.entry_date_clusters}</td><td>{percent(row.mean_return)}</td><td>{percent(row.mean_benchmark_return)}</td><td>{points(row.mean_excess_return)}</td><td>{percent(row.double_cost_mean_return)}</td><td>{row.date_cluster_bootstrap_95 ? row.date_cluster_bootstrap_95.map(percent).join(" ～ ") : "日期组不足 5"}</td></tr>)}
      </tbody></table></div>
      <p className="text-xs text-[#a4b0bf]">区间围绕等权日期组的平均超额计算；窗口存在跨日相关，不能当作独立试验或组合年化收益。</p>
      {!!Object.keys(exclusions ?? {}).length && <p className="text-xs text-amber-200">排除记录：{Object.entries(exclusions ?? {}).map(([reason, count]) => `${reasons[reason] ?? reason} ${count} 条`).join("；")}</p>}
      <details><summary className="cursor-pointer text-sm text-[#a4b0bf]">逐事件时间与窗口</summary><div className="mt-3 max-h-80 overflow-auto space-y-3">
        {report.events.map((event, i) => <div key={i} className="rounded bg-[#10141c] p-3 text-xs space-y-2"><p>{event.symbol} · 入场 {event.entry_date} · 条目 {event.items.join(", ")} · 合并 {event.accessions.length} 份申报</p><p>受理 {event.accepted_at}；首次取得 {event.first_seen_at}；本次可得 {event.available_at}</p><p>{event.windows.map(w => `${w.sessions} 日：净收益 ${percent(w.net_return)} / 超额 ${points(w.excess_return)} 个百分点`).join("；")}</p>{report.data_quality.data_kind === "synthetic" ? <span className="text-[#8b949e]">合成申报，完整输入见下载报告</span> : <a className="text-emerald-300 underline" href={event.source_url} target="_blank" rel="noreferrer">SEC 原始文件</a>}</div>)}
      </div></details>
      <p className="text-xs leading-relaxed text-[#8b949e]">{report.evidence_boundary}</p>
    </div>}
  </section>;
}
