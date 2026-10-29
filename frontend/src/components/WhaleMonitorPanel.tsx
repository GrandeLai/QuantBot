/**
 * WhaleMonitorPanel — ETH 链上巨鲸 CEX 资金流监控
 *
 * 功能：
 * - 展示 ETH 流入/流出已知 CEX 热钱包的总量
 * - 压力评分（0-1），1 = 强烈做空压力，0 = 强积累
 * - 最近大额转账列表（交易所名称 + 方向箭头 + ETH 金额）
 * - 支持时间窗口（6/24/72h）和最小金额（50/100/500 ETH）选择
 *
 * Phase F.10 — #0E1014 / #151619 / #00C087 dark-theme tokens
 * 注意：需要后端 ETHERSCAN_API_KEY 环境变量；未配置时显示提示。
 */
import { useEffect, useState } from "react";
import {
  type CEXInflowData,
  type WhaleTransferData,
  fetchCEXInflow,
} from "../api/client";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

type WhaleSig = CEXInflowData["signal"];

function signalColor(s: WhaleSig): string {
  if (s === "heavy_inflow") return "#ef4444";
  if (s === "elevated_inflow") return "#f59e0b";
  if (s === "neutral") return "#64748b";
  if (s === "accumulation") return "#34d399";
  return "#00C087"; // heavy_accumulation
}

function signalCN(s: WhaleSig): string {
  const map: Record<WhaleSig, string> = {
    heavy_inflow: "强烈做空压力",
    elevated_inflow: "偏高做空压力",
    neutral: "中性",
    accumulation: "吸筹积累",
    heavy_accumulation: "强烈吸筹",
  };
  return map[s] ?? s;
}

function fmtEth(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return n.toFixed(0);
}

function fmtUsd(n: number | null): string {
  if (n == null) return "—";
  if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(1)}M`;
  return `$${n.toFixed(0)}`;
}

function truncateAddr(addr: string): string {
  if (addr.length <= 10) return addr;
  return `${addr.slice(0, 6)}…${addr.slice(-4)}`;
}

function truncateHash(hash: string): string {
  if (hash.length <= 10) return hash;
  return `${hash.slice(0, 10)}…`;
}

// ---------------------------------------------------------------------------
// Transfer row
// ---------------------------------------------------------------------------

function TransferRow({ t }: { t: WhaleTransferData }) {
  const isInflow = t.direction === "inflow";
  const color = isInflow ? "#ef4444" : "#00C087";
  const arrow = isInflow ? "→ CEX" : "← CEX";

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "80px 90px 1fr 70px",
        gap: 6,
        alignItems: "center",
        padding: "5px 10px",
        borderBottom: "1px solid #1a1d23",
        fontSize: 11,
      }}
    >
      <div style={{ color: "#94a3b8", fontFamily: "monospace" }}>
        {truncateHash(t.tx_hash)}
      </div>
      <div style={{ color: "#64748b" }}>{t.exchange_name}</div>
      <div style={{ color: "#e2e8f0" }}>
        <span style={{ color }}>⬤</span>{" "}
        {truncateAddr(isInflow ? t.from_address : t.to_address)}{" "}
        <span style={{ color: "#475569" }}>{arrow}</span>
      </div>
      <div style={{ color, fontWeight: 700, textAlign: "right" }}>
        {fmtEth(t.value_eth)} ETH
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

type Hours = 6 | 24 | 72;
type MinEth = 50 | 100 | 500;

export default function WhaleMonitorPanel() {
  const [hours, setHours] = useState<Hours>(24);
  const [minEth, setMinEth] = useState<MinEth>(100);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<CEXInflowData | null>(null);

  async function load(h: Hours, m: MinEth) {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchCEXInflow(h, m);
      setData(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "查询失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load(hours, minEth);
  }, [hours, minEth]);

  const sc = data ? signalColor(data.signal) : "#64748b";
  const sl = data ? signalCN(data.signal) : "";
  const scorePct = data ? Math.round(data.pressure_score * 100) : 50;

  const btnStyle = (active: boolean): React.CSSProperties => ({
    background: active ? "#00C087" : "#1f2937",
    color: active ? "#0E1014" : "#94a3b8",
    border: "none",
    borderRadius: 4,
    padding: "3px 10px",
    fontWeight: 700,
    fontSize: 11,
    cursor: "pointer",
  });

  return (
    <div
      style={{
        background: "#0E1014",
        border: "1px solid #2a2d35",
        borderRadius: 10,
        padding: 16,
        marginBottom: 16,
        fontFamily: "ui-monospace, monospace",
      }}
    >
      {/* 标题 */}
      <div
        style={{
          fontWeight: 700,
          fontSize: 14,
          color: "#e2e8f0",
          marginBottom: 12,
          borderBottom: "1px solid #2a2d35",
          paddingBottom: 8,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        <span>链上巨鲸 CEX 资金流监控（ETH）</span>
        {data && !data.api_key_missing && (
          <span
            style={{
              fontSize: 11,
              padding: "2px 8px",
              borderRadius: 4,
              background: sc + "22",
              color: sc,
              fontWeight: 700,
            }}
          >
            {sl}
          </span>
        )}
      </div>

      {/* 参数控制 */}
      <div
        style={{
          display: "flex",
          gap: 16,
          marginBottom: 14,
          alignItems: "center",
          flexWrap: "wrap",
        }}
      >
        <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
          <span style={{ fontSize: 11, color: "#64748b", marginRight: 4 }}>时间窗口</span>
          {([6, 24, 72] as Hours[]).map((h) => (
            <button key={h} style={btnStyle(hours === h)} onClick={() => setHours(h)}>
              {h}h
            </button>
          ))}
        </div>
        <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
          <span style={{ fontSize: 11, color: "#64748b", marginRight: 4 }}>最小金额</span>
          {([50, 100, 500] as MinEth[]).map((m) => (
            <button key={m} style={btnStyle(minEth === m)} onClick={() => setMinEth(m)}>
              {m} ETH
            </button>
          ))}
        </div>
        {loading && (
          <span style={{ fontSize: 11, color: "#64748b" }}>刷新中...</span>
        )}
      </div>

      {/* API key 缺失提示 */}
      {data?.api_key_missing && (
        <div
          style={{
            background: "#f59e0b22",
            border: "1px solid #f59e0b55",
            borderRadius: 6,
            padding: "8px 12px",
            color: "#f59e0b",
            fontSize: 12,
            marginBottom: 12,
          }}
        >
          ⚠ 未配置 ETHERSCAN_API_KEY — 链上数据不可用。请在 .env 文件中设置该变量。
        </div>
      )}

      {/* 错误 */}
      {error && (
        <div
          style={{
            background: "#ef444422",
            border: "1px solid #ef444455",
            borderRadius: 6,
            padding: "8px 12px",
            color: "#ef4444",
            fontSize: 12,
            marginBottom: 12,
          }}
        >
          {error}
        </div>
      )}

      {/* 主要指标 */}
      {data && !data.api_key_missing && (
        <>
          {/* 压力评分条 */}
          <div
            style={{
              background: "#151619",
              border: `1px solid ${sc}33`,
              borderRadius: 8,
              padding: "12px 14px",
              marginBottom: 10,
            }}
          >
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                fontSize: 11,
                color: "#64748b",
                marginBottom: 6,
              }}
            >
              <span>CEX 资金流压力评分（0 = 积累 / 1 = 卖压）</span>
              <span style={{ color: sc, fontWeight: 700 }}>{scorePct} / 100</span>
            </div>
            {/* Gradient bar: left=green (accumulation), right=red (sell pressure) */}
            <div
              style={{
                height: 10,
                background: "linear-gradient(to right, #00C087, #64748b, #ef4444)",
                borderRadius: 5,
                position: "relative",
              }}
            >
              <div
                style={{
                  position: "absolute",
                  left: `calc(${scorePct}% - 6px)`,
                  top: -3,
                  width: 16,
                  height: 16,
                  borderRadius: "50%",
                  background: sc,
                  border: "2px solid #0E1014",
                  transition: "left 0.3s ease",
                }}
              />
            </div>

            {/* 三列指标 */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr 1fr",
                gap: 8,
                marginTop: 12,
              }}
            >
              {[
                ["流入 (↑CEX)", `${fmtEth(data.inflow_eth)} ETH`, "#ef4444"],
                ["流出 (↓CEX)", `${fmtEth(data.outflow_eth)} ETH`, "#00C087"],
                [
                  "净流入",
                  `${data.net_flow_eth >= 0 ? "+" : ""}${fmtEth(data.net_flow_eth)} ETH`,
                  data.net_flow_eth > 0 ? "#ef4444" : "#00C087",
                ],
              ].map(([label, val, color]) => (
                <div
                  key={label as string}
                  style={{
                    background: "#0E1014",
                    border: `1px solid ${color as string}33`,
                    borderRadius: 5,
                    padding: "6px 10px",
                    textAlign: "center",
                  }}
                >
                  <div style={{ fontSize: 10, color: "#64748b" }}>{label as string}</div>
                  <div
                    style={{ fontSize: 13, fontWeight: 700, color: color as string }}
                  >
                    {val as string}
                  </div>
                </div>
              ))}
            </div>

            {data.inflow_usd != null && (
              <div style={{ marginTop: 6, fontSize: 10, color: "#475569" }}>
                流入 USD 估值：{fmtUsd(data.inflow_usd)}
              </div>
            )}
          </div>

          {/* 转账列表 */}
          {data.recent_transfers.length > 0 && (
            <div
              style={{
                background: "#151619",
                border: "1px solid #2a2d35",
                borderRadius: 8,
                overflow: "hidden",
                marginBottom: 10,
              }}
            >
              {/* 表头 */}
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "80px 90px 1fr 70px",
                  gap: 6,
                  padding: "6px 10px",
                  background: "#0E1014",
                  fontSize: 10,
                  color: "#64748b",
                  fontWeight: 700,
                  textTransform: "uppercase",
                }}
              >
                <div>TxHash</div>
                <div>交易所</div>
                <div>地址</div>
                <div style={{ textAlign: "right" }}>金额</div>
              </div>
              {data.recent_transfers.slice(0, 15).map((t) => (
                <TransferRow key={t.tx_hash} t={t} />
              ))}
            </div>
          )}

          {data.recent_transfers.length === 0 && (
            <div
              style={{
                textAlign: "center",
                color: "#475569",
                fontSize: 12,
                padding: "12px 0",
              }}
            >
              过去 {data.hours}h 内未发现 ≥{data.min_eth} ETH 的大额转账
            </div>
          )}

          <div style={{ marginTop: 4, fontSize: 10, color: "#475569" }}>
            转账数：{data.transfer_count}　│　窗口：{data.hours}h　│　来源：Etherscan　│　不构成投资建议
          </div>
        </>
      )}
    </div>
  );
}
