const items = [
  ["价格行为", "趋势、突破、回调、反转与 ATR。作为 AI 的行情证据，也可在运行中心选择独立规则对照。"],
  ["多因子研究", "主决策使用动量与波动率评分。左侧因子筛选提供股票、期权、基本面与情绪研究。"],
  ["市场与风险", "行情总览和 K 线图表读取实际数据；风险与复盘提供持仓、订单查询和历史验证。"],
  ["历史验证", "验证中心支持回测与 Walk-Forward；加密策略通过固定模拟行情运行 paper，AI 分配在运行中心。"],
];
export default function ResearchOverviewPanel() {
  return <div className="p-6 max-w-5xl mx-auto text-[#e6edf3] space-y-5"><h1 className="text-2xl font-semibold">量化研究</h1><p className="text-sm text-[#8b949e]">研究为决策提供证据。模型建议由独立风控约束，研究文字不会直接变成订单。</p><div className="grid md:grid-cols-2 gap-4">{items.map(([title,body])=><div key={title} className="bg-[#161b22] border border-[#303844] rounded-xl p-5"><h2 className="font-medium">{title}</h2><p className="text-sm text-[#a4b0bf] mt-3 leading-6">{body}</p></div>)}</div><p className="text-sm text-[#8b949e]">新策略先用离线行情验证，再评估目标模拟账户的实际表现。</p></div>;
}
