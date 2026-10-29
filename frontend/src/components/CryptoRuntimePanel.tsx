import { useEffect, useState } from "react";

type Strategy = { name?: string; id?: string; display_name?: string; description?: string; live_supported?: boolean; demo_supported?: boolean };
export default function CryptoRuntimePanel() {
  const [strategies,setStrategies]=useState<Strategy[]>([]);
  const [selected,setSelected]=useState("");
  const [message,setMessage]=useState("");
  const [busy,setBusy]=useState(false);
  useEffect(()=>{fetch("/api/quantbot/crypto/strategies").then(r=>r.json()).then(r=>{
    const entries=r.strategy_catalog.strategies as Strategy[];
    setStrategies(entries);setSelected(entries[0]?.name||entries[0]?.id||"");
  }).catch(()=>setMessage("策略目录暂不可用"));},[]);
  async function run(){setBusy(true);setMessage("");try{
    const r=await fetch("/api/quantbot/crypto/strategy-run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({strategy:selected,symbol:"BTC/USDT",cycles:1})});
    const data=await r.json();if(!r.ok)throw new Error(data.detail||"策略执行失败");
    const result=data.strategy_run;
    setMessage(`模拟完成：${result.strategy_name||selected}。执行结果已保存到统一运行目录。此策略的报价机会收益属于估算，实际结果请查看运行记录。`);
  }catch(e){setMessage(e instanceof Error?e.message:"请求失败");}finally{setBusy(false);}}
  return <div className="p-6 max-w-5xl mx-auto space-y-5 text-[#e6edf3]"><h1 className="text-2xl font-semibold">加密策略</h1><p className="text-sm text-[#8b949e]">CoinBot 的规则策略与验证能力。此处使用固定模拟行情，运行一次本地 paper；AI 分配在「AI 量化交易」。</p><div className="flex flex-wrap gap-3"><select aria-label="加密策略" value={selected} onChange={e=>setSelected(e.target.value)} className="bg-[#161b22] border border-[#303844] rounded-lg px-3 py-2">{strategies.map(s=><option key={s.name||s.id} value={s.name||s.id}>{s.display_name||s.name||s.id}</option>)}</select><button onClick={run} disabled={busy||!selected} className="bg-emerald-500 text-[#071a13] px-4 py-2 rounded-lg disabled:opacity-40">{busy?"正在运行…":"运行一轮模拟"}</button></div>{message&&<p role="status" className="text-sm text-amber-200">{message}</p>}<div className="grid md:grid-cols-2 gap-3">{strategies.map(s=><div key={s.name||s.id} className="border border-[#303844] rounded-xl p-4 bg-[#161b22]"><h2 className="font-medium">{s.display_name||s.name||s.id}</h2><p className="text-sm text-[#8b949e] mt-2">{s.description}</p><p className="text-xs text-[#8b949e] mt-3">{s.demo_supported?"原策略支持受门禁控制的 OKX Demo 验证":"本地策略研究"} · 实盘未准入</p></div>)}</div></div>;
}
