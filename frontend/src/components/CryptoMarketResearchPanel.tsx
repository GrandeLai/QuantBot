import { useState } from "react";
type Summary = {market_regime:string; funding_percentile:number; recommended_timeframes:string[]; generated_at:string};
export default function CryptoMarketResearchPanel() {
  const [summary,setSummary]=useState<Summary|null>(null);
  const [error,setError]=useState("");
  const [busy,setBusy]=useState(false);
  async function load(){setBusy(true);setError("");try{const r=await fetch("/api/crypto/research/latest?symbol=BTC-USDT");if(!r.ok)throw new Error("市场研究数据暂不可用");setSummary(await r.json());}catch(e){setError(e instanceof Error?e.message:"请求失败");}finally{setBusy(false);}}
  return <div className="p-6 max-w-4xl mx-auto text-[#e6edf3] space-y-5"><h1 className="text-2xl font-semibold">加密市场机制研究</h1><p className="text-sm text-[#8b949e]">基于公开资金费率历史的规则分析。历史不足时使用保守区间标签，结果需要与实际行情交叉核对。</p><button onClick={load} disabled={busy} className="bg-emerald-500 text-[#071a13] rounded-lg px-4 py-2 disabled:opacity-40">{busy?"正在读取…":"读取 BTC 市场摘要"}</button>{error&&<p role="alert" className="text-red-300 text-sm">{error}</p>}{summary&&<div className="border border-[#303844] bg-[#161b22] rounded-xl p-5 space-y-3 text-sm"><p>机制标签：{summary.market_regime}</p><p>资金费率分位：{summary.funding_percentile}%</p><p>观察周期：{summary.recommended_timeframes.join("、")}</p><p className="text-[#8b949e]">此摘要属于规则研究，不构成训练模型、交易信号或成交收益。</p></div>}</div>;
}
