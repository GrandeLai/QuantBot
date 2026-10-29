import { useEffect, useState } from "react";
import { Brain, FlaskConical, Upload, ShieldCheck } from "lucide-react";
import RuntimePanel from "./RuntimePanel";
import OptionsOverlayPanel from "./OptionsOverlayPanel";
import SECEventStudyPanel from "./SECEventStudyPanel";

type Allocation = { symbol: string; target_weight: number; confidence: number; rationale: string };
type Order = { symbol: string; side: string; quantity: number | string; stop?: number | string };
type SECContext = { data_kind: string; information_cutoff: string; policy: { availability_mode: string; lookback_days: number };
  symbols: Record<string, { mapping_available: boolean; index_window_covered: boolean; omitted_events: number;
    events: { accession: string; form: string; available_at: string; text_excerpt: string; text_truncated: boolean; source_url: string }[] }> };
type Plan = { provider: string; model: string; allocations: Allocation[]; orders: Order[]; adjustments: string[]; sec_events?: SECContext };
type CorporateAction = { kind: string; at: string; action: { id: string; symbol: string; ratio?: string }; amount?: string; quantity_before?: string; quantity_after?: string };
type Result = { candidate?: Result; baseline?: Result; baseline_name?: string; passed?: boolean; checks?: Record<string, boolean>; report_path?: string;
  max_drawdown_fraction?: number; provider_calls?: number; errors?: { error: string }[]; risk_hold?: string; equity_curve?: { equity: number }[];
  status?: string; mode?: string; data_source?: string; plan?: Plan; decisions?: Plan[];
  fills?: number | Order[]; final_equity?: number; return_fraction?: number; model_called?: boolean;
  corporate_action_events?: CorporateAction[];
  account?: { cash: number; equity: number; dividend_income?: number; dividend_receivable?: number; positions: Record<string, string | number> }; as_of?: string };

export default function UnifiedTradingPanel() {
  const [market, setMarket] = useState("stocks");
  const [provider, setProvider] = useState("ai");
  const [mode, setMode] = useState("simulate");
  const [bars, setBars] = useState(30);
  const [allowModels, setAllowModels] = useState(false);
  const [routes, setRoutes] = useState(1);
  const [cash, setCash] = useState(10000);
  const [csv, setCsv] = useState("");
  const [fileName, setFileName] = useState("");
  const [vintageData, setVintageData] = useState<Record<string, unknown> | null>(null);
  const [vintageFileName, setVintageFileName] = useState("");
  const [secContext, setSecContext] = useState<Record<string, unknown> | null>(null);
  const [secFileName, setSecFileName] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [failureReport, setFailureReport] = useState("");
  const [result, setResult] = useState<Result | null>(null);
  useEffect(() => { fetch("/api/quantbot/status").then(r=>r.json()).then(r=>{setModel(r.model ? `${r.model_backend === "codex_cli" ? "本地 Codex · " : ""}${r.model}` : "尚未配置模型");setRoutes(1 + (r.fallback_models?.length || 0));}).catch(()=>setError("服务连接失败")); }, []);
  useEffect(() => { setResult(null); setFailureReport(""); }, [market, provider, mode, bars, cash, csv, secContext, vintageData]);
  const metrics = result?.candidate ?? result;
  const plan = metrics?.plan ?? metrics?.decisions?.at(-1);
  const equity = metrics?.equity_curve?.map(row=>row.equity) || [];
  const low = Math.min(...equity), high = Math.max(...equity);
  const curve = equity.map((v,i)=>`${10+i*580/Math.max(1,equity.length-1)},${110-(v-low)*90/Math.max(1,high-low)}`).join(" ");
  function downloadReport() {
    const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = "quantbot-backtest.json"; link.click(); URL.revokeObjectURL(url);
  }
  async function readEvents(file?: File) {
    if (!file) return;
    setError(""); setResult(null); setAllowModels(false);
    try {
      if (file.size > 64000000) throw new Error("公告输入文件超过 64 MB");
      const value = JSON.parse(await file.text());
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("公告输入需为 JSON 对象");
      setSecContext(value); setSecFileName(file.name);
    } catch (e) { setSecContext(null); setSecFileName(""); setError(e instanceof Error ? e.message : "公告文件读取失败"); }
  }
  async function readVintages(file?: File) {
    if (!file) return;
    setError(""); setResult(null); setAllowModels(false);
    try {
      if (file.size > 32000000) throw new Error("历史版本输入超过 32 MB");
      const value = JSON.parse(await file.text());
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("历史版本输入需为 JSON 对象");
      setVintageData(value); setVintageFileName(file.name); setCsv(""); setFileName("");
    } catch (e) { setError(e instanceof Error ? e.message : "历史版本文件读取失败"); }
  }
  async function readCsv(file?: File) {
    if (!file) return;
    setError(""); setResult(null); setAllowModels(false);
    try {
      if (file.size > 4000000) throw new Error("网页行情 CSV 超过 4 MB");
      const value = await file.text();
      if (!value.trim()) throw new Error("行情 CSV 不能为空");
      setCsv(value); setFileName(file.name); setVintageData(null); setVintageFileName("");
    } catch (e) { setError(e instanceof Error ? e.message : "行情文件读取失败"); }
  }
  async function run() {
    setBusy(true); setError(""); setResult(null); setFailureReport("");
    try {
      const response = await fetch("/api/quantbot/allocate", { method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({ market,provider,mode,initial_cash:cash,csv_data:csv,confirm_paper:confirmed,last_bars:bars,allow_model_calls:allowModels,
          sec_context: market === "stocks" && mode === "simulate" ? secContext : null,
          vintage_data: mode === "simulate" ? vintageData : null }) });
      const data = await response.json();
      if (!response.ok) {
        if (typeof data.detail?.download_url === "string") setFailureReport(data.detail.download_url);
        throw new Error(typeof data.detail === "string" ? data.detail : data.detail?.message || "输入未通过验证");
      }
      setResult(data);
    } catch(e) { setError(e instanceof Error ? e.message : "请求失败"); }
    finally { setBusy(false); }
  }
  function changeMarket(value: string) { setMarket(value);setMode("simulate");setAllowModels(false);setCsv("");setFileName("");setVintageData(null);setVintageFileName("");setSecContext(null);setSecFileName("");setResult(null);setConfirmed(false);if(provider==="price_action")setProvider("ai"); }
  const field = "w-full bg-[#10141c] border border-[#303844] rounded-lg px-3 py-2.5 text-sm";
  return <div className="max-w-5xl mx-auto p-6 space-y-6 text-[#e6edf3]">
    <div className="flex items-center gap-3"><Brain className="text-emerald-400" size={30}/><div><h1 className="text-2xl font-semibold">AI 量化交易</h1><p className="text-sm text-[#8b949e] mt-1">历史行情 → 策略决策 → 独立风控 → 回测成交 → 绩效复盘。当前以回测完成流程，模拟盘接入暂缓。</p></div></div>
    <div className="flex flex-wrap gap-3 text-xs text-[#a4b0bf]"><span className="rounded-full border border-[#303844] px-3 py-1.5">模型：{model}</span><span className="flex items-center gap-1"><ShieldCheck size={14}/> 历史回测 · 确定性风控</span></div>
    <fieldset disabled={busy} aria-label="主回测配置" className="rounded-xl border border-[#303844] bg-[#161b22] p-5 space-y-5">
      <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <label className="text-sm space-y-2 block">资产市场<select aria-label="资产市场" className={field} value={market} onChange={e=>changeMarket(e.target.value)}><option value="stocks">美股</option><option value="crypto">加密现货</option></select></label>
        <label className="text-sm space-y-2 block">决策方式<select aria-label="决策方式" className={field} value={provider} onChange={e=>{setProvider(e.target.value);setResult(null);setAllowModels(false);if(market==="stocks"&&e.target.value==="demo"&&mode==="paper")setMode("simulate");}}><option value="ai">大模型 AI</option>{market==="stocks"&&<option value="price_action">价格行为对照</option>}<option value="demo">离线演示（规则）</option></select></label>
        <label className="text-sm space-y-2 block">操作<select aria-label="操作" className={field} value={mode} onChange={e=>{setMode(e.target.value);setConfirmed(false);setAllowModels(false);setResult(null);setVintageData(null);setVintageFileName("");}}><option value="simulate">历史回测（当前流程）</option><option value="decide">生成交易计划</option>{(market==="crypto"||provider!=="demo")&&<option value="paper">{market==="stocks"?"Alpaca 模拟盘（后续步骤）":"本地模拟成交（后续步骤）"}</option>}</select></label>
        <label className="text-sm space-y-2 block">初始模拟资金<input aria-label="初始模拟资金" type="number" min="1" disabled={market==="stocks"&&mode==="paper"} className={field} value={cash} onChange={e=>setCash(Number(e.target.value))}/></label>
      </div>
      <label className="flex items-center gap-3 rounded-lg border border-dashed border-[#3b4654] p-4 cursor-pointer"><Upload size={20}/><div className="text-sm"><span>{fileName||"选择行情 CSV"}</span><p className="text-xs text-[#8b949e] mt-1">{market==="stocks"?"date,symbol,open,high,low,close,volume；默认 127 根特征预热，再增加回测根数 − 1":"timestamp,symbol,open,high,low,close,volume,bar；至少 35 根预热，再增加回测根数 − 1；带时区、连续"}</p></div><input aria-label="行情 CSV" type="file" accept=".csv" className="hidden" onChange={e=>{const f=e.target.files?.[0];e.target.value="";void readCsv(f);}}/></label>
      {mode === "simulate" && <div className="rounded-lg border border-[#303844] p-4 space-y-2">
        <div className="flex flex-wrap items-center gap-4"><label className="cursor-pointer text-sm text-emerald-300 underline">{vintageFileName || "或：导入含发布时间的行情 JSON"}<input className="hidden" type="file" accept=".json" aria-label="历史行情版本输入" onChange={e => { const file=e.target.files?.[0];e.target.value="";void readVintages(file); }}/></label>
          {vintageData && <button className="text-xs text-[#a4b0bf] underline" onClick={() => { setVintageData(null); setVintageFileName(""); setAllowModels(false); }}>移除历史版本输入</button>}</div>
        <p className="text-xs text-[#a4b0bf]">与 CSV 二选一。保留原始价格与修订时间；美股可附整股拆并股、普通美元现金分红。分红先计入待到账权益，支付后才成为可用现金。</p>
      </div>}
      {provider==="demo"&&<p className="text-sm text-amber-300 flex items-center gap-2"><FlaskConical size={16}/> 离线演示不调用大模型。{!csv && !vintageData ? "未导入行情，将使用合成行情。" : "本次使用导入的行情。"}</p>}
      {market === "stocks" && mode === "simulate" && <div className="rounded-lg border border-[#303844] p-4 space-y-2">
        <div className="flex flex-wrap items-center gap-4"><label className="cursor-pointer text-sm text-emerald-300 underline">{secFileName || "可选：导入 AI 回测公告输入"}<input className="hidden" type="file" accept=".json" aria-label="AI 回测公告输入" onChange={e => { void readEvents(e.target.files?.[0]); }}/></label>
          {secContext && <button className="text-xs text-[#a4b0bf] underline" onClick={() => { setSecContext(null); setSecFileName(""); setAllowModels(false); }}>移除公告输入</button>}</div>
        <p className="text-xs text-[#a4b0bf]">可从下方 SEC 事件研究导出。按每次决策的可得时间选取历史版本，报告保留所见正文与覆盖缺口；规则对照仅使用价格特征。</p>
      </div>}
      {mode==="simulate"&&<div className="space-y-3">
        <label className="block text-sm">回测根数 <input aria-label="回测根数" className="ml-2 w-24 rounded border border-[#303844] bg-[#10141c] px-3 py-2" type="number" min="3" max="500" value={bars} onChange={e=>{setBars(Number(e.target.value));setAllowModels(false);}}/></label>
        <p className="text-xs text-[#a4b0bf]">使用最近 {bars} 根预热完成后的行情；收盘生成决策，下一根开盘计入成本撮合，最后一根只估值。每次从独立初始资金开始。</p>
        {provider==="ai"&&<label className="flex items-start gap-2 text-sm text-amber-200"><input className="mt-1" type="checkbox" checked={allowModels} onChange={e=>setAllowModels(e.target.checked)}/>允许本次回测调用模型，最多 {(bars-1)*routes} 次请求（含备用模型），可能产生费用</label>}
      </div>}
      {mode==="paper"&&<label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>确认本次记录模拟成交{market==="stocks"?"并向 Alpaca Paper 提交订单":"（独立账户，收盘价加成本）"}</label>}
      <button onClick={run} disabled={busy||(mode==="paper"&&!confirmed)||(mode==="simulate"&&provider==="ai"&&!allowModels)} className="rounded-lg bg-emerald-500 text-[#071a13] px-5 py-2.5 font-medium disabled:opacity-40">{busy?"正在验证与计算…":mode==="simulate"?"运行回测":mode==="decide"?"生成计划":"执行模拟"}</button>
      {error&&<p role="alert" className="text-sm text-red-300">{error}</p>}
      {failureReport && <p className="text-xs text-amber-200">已保留失败前的调用输入、决策与模拟成交。<a href={failureReport} download className="ml-2 text-emerald-300 underline">下载失败报告</a> · 未确认的调用不会自动重试。</p>}
    </fieldset>
    {result&&<div className="rounded-xl border border-[#303844] bg-[#161b22] p-5 space-y-4">
      <div className="flex flex-wrap gap-5 text-sm"><span>数据：{result.data_source==="mock_fixture"?"合成演示行情":result.data_source==="vintage"?"历史行情版本（来源声明未认证）":"离线 CSV"}</span><span>模型：{plan?.model||(result.model_called?"未返回有效决策":"规则对照")}</span>{result.as_of&&<span>截止：{new Date(result.as_of).toLocaleString()}</span>}</div>
      {result.candidate&&<div className="flex flex-wrap items-center justify-between gap-3"><h2 className="font-medium">回测报告 · {result.passed ? "检查通过" : "检查未通过"}</h2><button className="text-sm text-emerald-300 underline" onClick={downloadReport}>下载完整报告</button></div>}
      {metrics?.account&&<div className="grid grid-cols-2 gap-4"><div>账户权益 <strong>{metrics.account.equity.toFixed(2)}</strong></div><div>可用现金 <strong>{metrics.account.cash.toFixed(2)}</strong></div></div>}
      {!!metrics?.corporate_action_events?.length && <details className="rounded-lg border border-[#303844] p-3"><summary className="cursor-pointer text-sm text-emerald-300">拆股与分红记录（{metrics.corporate_action_events.length}）</summary>
        <p className="text-sm mt-3">累计分红权益：{metrics.account?.dividend_income?.toFixed(2) ?? "0.00"} · 待到账分红：{metrics.account?.dividend_receivable?.toFixed(2) ?? "0.00"}</p>
        {metrics.corporate_action_events.map(event => <p className="text-xs text-[#a4b0bf] mt-2" key={`${event.action.id}-${event.kind}`}>{event.at.slice(0, 10)} · {event.action.symbol} · {event.kind === "split" ? `拆并股 ${event.quantity_before} → ${event.quantity_after} 股` : event.kind === "dividend_entitlement" ? `登记分红权益 ${event.amount} USD` : `分红到账 ${event.amount} USD`}</p>)}
      </details>}
      {metrics?.final_equity!==undefined&&<p>期末权益：{metrics.final_equity.toFixed(2)} · 回测收益：{((metrics.return_fraction||0)*100).toFixed(2)}% · 最大回撤：{((metrics.max_drawdown_fraction||0)*100).toFixed(2)}% · 成交数：{typeof metrics.fills==="number"?metrics.fills:metrics.fills?.length||0}</p>}
      {result.baseline&&<p className="text-sm text-[#a4b0bf]">同区间{result.baseline_name==="price_action"?"价格行为":"动量"}规则收益：{((result.baseline.return_fraction||0)*100).toFixed(2)}% · 现金基线：0.00% · 当前策略决策调用：{metrics?.provider_calls} 次</p>}
      {equity.length>1&&<svg viewBox="0 0 600 130" role="img" aria-label="回测权益曲线" className="w-full max-h-40 rounded-lg bg-[#10141c]"><polyline points={curve} fill="none" stroke="#34d399" strokeWidth="2"/><text x="10" y="15" fill="#a4b0bf" fontSize="11">{high.toFixed(2)}</text><text x="10" y="125" fill="#a4b0bf" fontSize="11">{low.toFixed(2)}</text></svg>}
      {result.checks&&Object.entries(result.checks).filter(([,passed])=>!passed).map(([key])=><p className="text-sm text-amber-300" key={key}>检查未通过：{({complete_window:"回测区间不完整",complete_decisions:"部分时点缺少有效决策",no_model_errors:"模型或决策校验失败",no_risk_hold:"触发账户风控暂停"} as Record<string,string>)[key] || key}</p>)}
      {!!metrics?.errors?.length&&<details><summary className="cursor-pointer text-sm text-amber-300">失败记录（{metrics.errors.length}）</summary>{metrics.errors.map((e,i)=><p className="mt-1 text-xs text-amber-200" key={i}>{e.error}</p>)}</details>}
      {plan?.sec_events && <details className="rounded-lg border border-[#303844] p-3"><summary className="cursor-pointer text-sm text-emerald-300">最近一次决策可见的 SEC 公告</summary>
        <p className="text-xs text-amber-200 mt-3">{plan.sec_events.data_kind === "synthetic" ? "合成公告验收数据。" : "公告的历史可得性尚未独立认证。"} 信息截止 {plan.sec_events.information_cutoff} · 最近 {plan.sec_events.policy.lookback_days} 天 · {({ observed: "首次取得时间", declared: "声明公开时间", acceptance_proxy: "受理时间代理假设" } as Record<string, string>)[plan.sec_events.policy.availability_mode]}</p>
        {Object.entries(plan.sec_events.symbols).map(([symbol, state]) => <div key={symbol} className="mt-3 text-xs space-y-2"><p>{symbol} · {state.events.length} 份正文 · 超出数量限制 {state.omitted_events} 份{!state.mapping_available ? " · 无该时点公司映射" : !state.index_window_covered ? " · 索引或映射未覆盖整个回看窗口" : ""}</p>
          {state.events.map(event => <div key={event.accession} className="rounded bg-[#10141c] p-3 space-y-2"><p>{event.form} · 可得 {event.available_at}{event.text_truncated ? " · 正文已截断" : ""}</p><p className="whitespace-pre-wrap text-[#a4b0bf]">{event.text_excerpt}</p>{plan.sec_events?.data_kind !== "synthetic" && <a className="text-emerald-300 underline" href={event.source_url} target="_blank" rel="noreferrer">SEC 原始文件</a>}</div>)}</div>)}
        <p className="text-xs text-[#8b949e] mt-3">空列表只表示导入归档中没有当时可见的匹配记录，不能据此认定公司没有公告。所有决策的输入见完整报告。</p>
      </details>}
      {plan&&<><h2 className="font-medium">目标持仓与理由</h2><div className="space-y-3">{plan.allocations.map(a=><div key={a.symbol} className="border-b border-[#29323c] pb-3"><div className="flex gap-5 text-sm"><strong>{a.symbol}</strong><span>目标 {(a.target_weight*100).toFixed(1)}%</span><span>置信度 {(a.confidence*100).toFixed(0)}%</span></div><p className="text-sm text-[#a4b0bf] mt-1">{a.rationale}</p></div>)}</div><h2 className="font-medium">通过风控的订单</h2>{plan.orders.length?<div className="overflow-x-auto"><table className="w-full text-sm text-left"><thead><tr className="text-[#8b949e]"><th className="py-2">标的</th><th>方向</th><th>数量</th><th>保护止损</th></tr></thead><tbody>{plan.orders.map((o,i)=><tr key={i}><td className="py-2">{o.symbol}</td><td>{o.side==="buy"?"买入":"卖出"}</td><td>{o.quantity}</td><td>{o.stop??"—"}</td></tr>)}</tbody></table></div>:<p className="text-sm text-[#8b949e]">本次无需新订单，或风控未准入新增持仓。</p>}{!!plan.adjustments.length&&<p className="text-xs text-amber-300">风控调整：{plan.adjustments.join("；")}</p>}</>}
      <p className="text-xs text-[#8b949e]">回测报告保留决策、订单、权益、成本和错误记录。结果用于验证历史假设，不代表实盘成交或前瞻收益；完成后不会自动启用模拟盘。</p>
    </div>}
    <OptionsOverlayPanel/>
    <SECEventStudyPanel/>
    <RuntimePanel/>
  </div>;
}
