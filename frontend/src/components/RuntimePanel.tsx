import { useCallback, useEffect, useState } from "react";
import EvaluationPanel, { type Evaluation } from "./EvaluationPanel";
import ResearchPanel from "./ResearchPanel";
import ResearchOperationsPanel from "./ResearchOperationsPanel";

type Spec = { name: string; market: string; provider: string; source: string; symbols: string[]; bar: string;
  data_path: string; interval_seconds: number; max_daily_loss: number; max_drawdown: number };
type Account = { cash: number; equity: number; realized_pnl: number; unrealized_pnl: number;
  estimated_execution_cost: number; positions: Record<string, string>;
  pending_orders: { client_order_id: string; symbol: string; side: string; quantity: string | number }[] };
type Job = { id: string; parent: string | null; status: string; stage: string; last_error: string | null;
  spec: Spec; account_view: Account; config: { llm: { model: string }; execution: { initial_cash: number } } };
type Detail = { job: Job; cycles: { id: string; as_of: string; status: string; result: {
  account: Account;
  plan?: { allocations: { symbol: string; rationale: string; target_weight: number }[] };
  orders: { symbol: string; side: string; quantity: string; status: string; reason?: string; price?: string }[] } }[];
  events: { id: number; kind: string; at: number; payload: { error?: string } }[];
  evaluations: { id: string; report: Evaluation }[] };

const riskMessages: Record<string, string> = { daily_loss_limit: "达到日内亏损上限", drawdown_limit: "达到账户回撤上限", nonpositive_equity: "账户权益不足" };

const states: Record<string, string> = { candidate: "待评估", evaluated: "已评估", paper: "持续模拟",
  active: "运行中", paused: "已暂停", held: "异常暂停", closing: "等待模拟平仓", completed: "已完成",
  monitored: "持仓检查", discarded: "计划已撤回", model_error: "模型失败", interrupted: "执行中断", started: "决策中" };

async function request(path = "", body?: unknown) {
  const response = await fetch("/api/quantbot/runtime" + path, body === undefined ? undefined : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "请求未通过验证");
  return data;
}

export default function RuntimePanel() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [online, setOnline] = useState(false);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("我的回测候选");
  const [market, setMarket] = useState("stocks");
  const [source, setSource] = useState("csv");
  const [provider, setProvider] = useState("ai");
  const [symbols, setSymbols] = useState("SPY,AAPL,MSFT");
  const [bar, setBar] = useState("1d");
  const [path, setPath] = useState("");
  const [cash, setCash] = useState(10000);
  const [model, setModel] = useState("");
  const [loss, setLoss] = useState(3);
  const [drawdown, setDrawdown] = useState(10);
  const [authorized, setAuthorized] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [exitConfirmed, setExitConfirmed] = useState(false);
  const [bars, setBars] = useState(30);
  const job = jobs.find(j => j.id === selected);
  const refresh = useCallback(async () => {
    const result = await request(); setJobs(result.jobs); setOnline(result.worker_online);
    if (selected) setDetail(await request(`/jobs/${selected}`));
  }, [selected]);
  useEffect(() => {
    let alive = true;
    async function poll() { try { if (alive) await refresh(); } catch (e) { if (alive) setError(String(e)); } }
    void poll(); const timer = setInterval(() => void poll(), 5000);
    return () => { alive = false; clearInterval(timer); };
  }, [refresh]);
  async function act(operation: () => Promise<unknown>) {
    setBusy(true); setError("");
    try { await operation(); await refresh(); } catch(e) { setError(e instanceof Error ? e.message : "请求失败"); }
    finally { setBusy(false); }
  }
  async function create(parent?: Job) {
    const spec = { name, market, source, provider, symbols: symbols.split(/[,，\s]+/).filter(Boolean),
      bar, data_path: path, interval_seconds: 60, max_daily_loss: loss / 100, max_drawdown: drawdown / 100 };
    const result = await request("/jobs", { spec: parent ? { ...parent.spec, name: parent.spec.name + " 新版本" } : spec,
      initial_cash: parent ? parent.config.execution.initial_cash : cash, parent: parent?.id,
      model: model.trim() || undefined });
    setSelected(result.id); setDetail(null); setAuthorized(false);
  }
  function control(action: string) {
    return act(() => request(`/jobs/${selected}/control`, { action, allow_model_calls: authorized, acknowledge_risk: acknowledged }));
  }
  const field = "w-full rounded-lg border border-[#303844] bg-[#10141c] px-3 py-2 text-sm";
  const button = "rounded-lg border border-[#3b4654] px-3 py-2 text-sm hover:bg-[#29323c] disabled:opacity-40";
  const selectedDetail = detail?.job.id === selected ? detail : null;
  const report = selectedDetail?.evaluations[0]?.report;
  const equity = [...(selectedDetail?.cycles || [])].reverse().map(c=>c.result.account.equity);
  const low = Math.min(...equity), high = Math.max(...equity);
  const curve = equity.map((v,i)=>`${10+i*580/Math.max(1,equity.length-1)},${110-(v-low)*90/Math.max(1,high-low)}`).join(" ");
  return <section className="space-y-4 border-t border-[#303844] pt-6">
    <div className="flex items-center justify-between gap-3"><div><h2 className="text-xl font-semibold">回测研究与版本对比</h2>
      <p className="mt-1 text-sm text-[#a4b0bf]">创建候选 → 历史回测 → 滚动评估 → 报告复盘；回测无需启动运行服务。</p></div>
      <span className={online ? "text-emerald-400 text-sm" : "text-amber-300 text-sm"}>{online ? "运行服务在线" : "持续模拟未启动（不影响回测）"}</span></div>
    
    <details className="rounded-xl border border-[#303844] bg-[#161b22] p-4" open={jobs.length === 0}>
      <summary className="cursor-pointer font-medium">新建候选任务</summary>
      <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <label className="text-sm">任务名称<input aria-label="任务名称" className={field} value={name} onChange={e=>setName(e.target.value)}/></label>
        <label className="text-sm">市场<select aria-label="任务市场" className={field} value={market} onChange={e=>{setMarket(e.target.value);setSource("csv");setSymbols(e.target.value === "stocks" ? "SPY,AAPL,MSFT" : "BTC/USDT,ETH/USDT");setBar(e.target.value === "stocks" ? "1d" : "15m");if(provider === "price_action")setProvider("ai");}}><option value="stocks">美股</option><option value="crypto">加密现货</option></select></label>
        <label className="text-sm">决策方式<select aria-label="任务决策方式" className={field} value={provider} onChange={e=>setProvider(e.target.value)}><option value="ai">大模型 AI</option>{market === "stocks" && <option value="price_action">价格行为规则</option>}<option value="demo">动量演示规则</option></select></label>
        <label className="text-sm">行情来源<select aria-label="任务行情来源" className={field} value={source} onChange={e=>setSource(e.target.value)}><option value="csv">本机历史 CSV</option><option value="vintage">带发布时间的历史版本 JSON（仅回测）</option><option value={market === "stocks" ? "yahoo" : "okx_public"}>{market === "stocks" ? "Yahoo 日线" : "OKX 公开行情（只读）"}</option></select></label>
        <label className="text-sm">标的，以逗号分隔<input aria-label="任务标的" className={field} value={symbols} onChange={e=>setSymbols(e.target.value)}/></label>
        <label className="text-sm">K 线周期<select aria-label="任务周期" className={field} value={bar} onChange={e=>setBar(e.target.value)}>{market === "crypto" && <><option value="15m">15 分钟</option><option value="1h">1 小时</option></>}<option value="1d">日线</option></select></label>
        {["csv", "vintage"].includes(source) && <label className="text-sm sm:col-span-2">本机行情文件完整路径<input aria-label="任务行情路径" className={field} value={path} placeholder="/Users/…/daily.csv" onChange={e=>setPath(e.target.value)}/></label>}
        <label className="text-sm">模拟资金<input aria-label="任务模拟资金" type="number" min="1" className={field} value={cash} onChange={e=>setCash(Number(e.target.value))}/></label>
        <label className="text-sm">日内亏损暂停（%）<input aria-label="日内亏损上限" type="number" min="0.1" max="99" step="0.1" className={field} value={loss} onChange={e=>setLoss(Number(e.target.value))}/></label>
        <label className="text-sm">账户回撤暂停（%）<input aria-label="回撤上限" type="number" min="0.1" max="99" step="0.1" className={field} value={drawdown} onChange={e=>setDrawdown(Number(e.target.value))}/></label>
      </div>
      <button className={button + " mt-4"} disabled={busy} onClick={()=>void act(()=>create())}>创建候选任务</button>
    </details>
    <div className="flex flex-wrap gap-2">{jobs.map(j=><button key={j.id} className={button + (selected === j.id ? " border-emerald-400 text-emerald-300" : "")} onClick={()=>{setSelected(j.id);setDetail(null);setAcknowledged(false);setAuthorized(false);setExitConfirmed(false);}}>{j.spec.name} · {states[j.status]}</button>)}</div>
    <ResearchPanel jobs={jobs} selectedJobId={selected}/>
    <ResearchOperationsPanel/>
    {job && <div className="rounded-xl border border-[#303844] bg-[#161b22] p-5 space-y-4">
      <p className="text-sm">{states[job.stage]} · {job.spec.symbols.join(" / ")} · {job.spec.bar} · {job.spec.provider === "ai" ? job.config.llm.model || "模型未配置" : "确定性规则"}</p>
      <p className="text-xs text-[#8b949e]">任务 {job.id}{job.parent ? " · 独立账户的新版本" : ""}。回测独立计算，报告保留在本候选中，不改动持续模拟账户。</p>
      {job.last_error && <p role="alert" className="text-sm text-amber-300">暂停或异常原因：{riskMessages[job.last_error] || job.last_error}</p>}
      {job.spec.provider === "ai" && <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={authorized} onChange={e=>setAuthorized(e.target.checked)}/>允许本任务的模型调用（历史评估会逐根调用，持续运行可能产生费用）</label>}
      {job.last_error && <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={acknowledged} onChange={e=>setAcknowledged(e.target.checked)}/>已检查并处理上述原因，允许恢复检查</label>}
      <div className="flex flex-wrap items-center gap-2">
        {job.stage !== "paper" && <><label className="text-sm">评估根数 <input aria-label="评估根数" className="w-20 rounded border border-[#303844] bg-[#10141c] px-2 py-2" type="number" min="3" max="500" value={bars} onChange={e=>setBars(Number(e.target.value))}/></label><button className={button} disabled={busy || (job.spec.provider === "ai" && !authorized)} onClick={()=>void act(()=>request(`/jobs/${selected}/evaluate`, { last_bars: bars, allow_model_calls: authorized }))}>运行基础回测</button></>}
      </div>
      <EvaluationPanel key={job.id} job={job} jobs={jobs} report={report} busy={busy}
        onEvaluate={body=>void act(()=>request("/evaluations/walk-forward", body))}/>
      <details open={job.stage === "paper"} className="space-y-4 border-t border-[#303844] pt-4">
        <summary className="cursor-pointer text-sm">后续步骤：持续本地模拟与账户监控（当前暂缓）</summary>
        <p className="text-xs text-[#a4b0bf]">回测到报告即完成。若以后启用持续模拟，需要另行启动运行服务；已有模拟账户的控制仍保留。</p>
        <div className="flex flex-wrap items-center gap-2">
        <button className={button} disabled={busy || job.stage === "candidate" || (job.spec.provider === "ai" && !authorized) || (!!job.last_error && !acknowledged)} onClick={()=>void control("resume")}>启用 / 恢复</button>
        <button className={button} disabled={busy} onClick={()=>void control("pause")}>暂停新决策</button>
        <label className="flex items-center gap-1 text-sm"><input type="checkbox" checked={exitConfirmed} onChange={e=>setExitConfirmed(e.target.checked)}/>确认模拟清仓</label>
        <button className={button} disabled={busy || !exitConfirmed} onClick={()=>void control("exit")}>退出持仓</button>
      </div>
      <div className="grid grid-cols-2 gap-4 text-sm lg:grid-cols-4"><div>权益 <strong>{job.account_view.equity.toFixed(2)}</strong></div><div>现金 <strong>{job.account_view.cash.toFixed(2)}</strong></div><div>已实现盈亏 <strong>{job.account_view.realized_pnl.toFixed(2)}</strong></div><div>未实现盈亏 <strong>{job.account_view.unrealized_pnl.toFixed(2)}</strong></div></div>
      <p className="text-xs text-[#8b949e]">本地模拟，估计交易成本 {job.account_view.estimated_execution_cost.toFixed(2)}；已计入盈亏。</p>
      {equity.length > 1 && <div><h3 className="text-sm font-medium">最近账户权益</h3><svg viewBox="0 0 600 130" role="img" aria-label="本地模拟账户权益曲线" className="mt-2 w-full max-h-40 rounded-lg bg-[#10141c]"><polyline points={curve} fill="none" stroke="#34d399" strokeWidth="2"/><text x="10" y="15" fill="#a4b0bf" fontSize="11">{high.toFixed(2)}</text><text x="10" y="125" fill="#a4b0bf" fontSize="11">{low.toFixed(2)}</text></svg></div>}
      <div className="grid gap-4 sm:grid-cols-2"><div><h3 className="text-sm font-medium">当前持仓</h3>{Object.entries(job.account_view.positions).map(([s,q])=><p key={s} className="mt-1 text-sm">{s} · {q}</p>)}{!Object.keys(job.account_view.positions).length && <p className="text-sm text-[#8b949e]">空仓</p>}</div><div><h3 className="text-sm font-medium">待模拟成交订单</h3>{job.account_view.pending_orders.map((o,i)=><p className="mt-1 text-sm" key={i}>{o.symbol} · {o.side === "buy" ? "买入" : "卖出"} {o.quantity} <button disabled={busy} className="ml-2 text-amber-300 underline disabled:opacity-40" onClick={()=>void act(()=>request(`/jobs/${selected}/orders/${o.client_order_id}/cancel`, {}))}>取消</button></p>)}{!job.account_view.pending_orders.length && <p className="text-sm text-[#8b949e]">无待执行订单</p>}</div></div>
      <details><summary className="cursor-pointer text-sm">最近运行与成交记录</summary>{selectedDetail?.cycles.map(c=><div key={c.id} className="mt-3 border-t border-[#303844] pt-2 text-sm"><p>{new Date(c.as_of).toLocaleString()} · {states[c.status] || c.status}</p>{c.result.orders.map((o,i)=><p className="text-xs text-[#a4b0bf]" key={i}>{o.symbol} · {o.side} {o.quantity} · {o.status}{o.price ? ` @ ${o.price}` : ""}{o.reason ? ` · ${o.reason}` : ""}</p>)}{c.result.plan?.allocations.map(a=><p key={a.symbol} className="text-xs text-[#a4b0bf]">{a.symbol} {(a.target_weight*100).toFixed(1)}% · {a.rationale}</p>)}</div>)}</details>
      <details><summary className="cursor-pointer text-sm">运行告警</summary>{selectedDetail?.events.filter(e=>e.kind === "alert").map(e=><p className="mt-2 text-sm text-amber-300" key={e.id}>{new Date(e.at*1000).toLocaleString()} · {riskMessages[e.payload.error || ""] || e.payload.error}</p>)}</details>
      </details>
      <div className="flex flex-wrap gap-2"><input aria-label="新版本模型" className={field + " sm:max-w-xs"} placeholder="新版本模型名称（可选）" value={model} onChange={e=>setModel(e.target.value)}/><button className={button} disabled={busy} onClick={()=>void act(()=>create(job))}>复制为候选版本</button></div>
      <p className="text-xs text-[#8b949e]">模型、策略和风控随版本冻结。启用同系列的另一个已评估版本会暂停当前版本；各版本使用独立模拟账户。</p>
    </div>}
    {busy && <p className="text-sm text-[#a4b0bf]">正在处理，历史评估可能需要较长时间…</p>}
    {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
  </section>;
}
