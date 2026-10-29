/**
 * TradingView Lightweight Charts 蜡烛图组件.
 * 支持 EMA / BBands 价格叠加层 + 成交量直方图.
 */
import { useEffect, useRef } from "react";
import {
  createChart,
  ColorType,
  LineStyle,
  type IChartApi,
  type UTCTimestamp,
} from "lightweight-charts";
import type { OhlcBar, IndicatorSeries } from "../api/client";
import type { OverlayKey } from "../store/chartStore";
import { registerChartApi, handleDrawingClick } from "../store/chartDrawing";

interface Props {
  bars: OhlcBar[];
  indicatorData: Record<string, IndicatorSeries>;
  activeOverlays: Set<OverlayKey>;
  showVolume: boolean;
  height?: number;
}

function toUTC(isoStr: string): UTCTimestamp {
  return Math.floor(new Date(isoStr).getTime() / 1000) as UTCTimestamp;
}

function buildAlignedPoints(
  bars: OhlcBar[],
  series: (number | null)[],
): { time: UTCTimestamp; value: number }[] {
  const pts: { time: UTCTimestamp; value: number }[] = [];
  for (let i = 0; i < bars.length; i++) {
    const v = series[i];
    if (v != null && isFinite(v)) {
      pts.push({ time: toUTC(bars[i].timestamp), value: v });
    }
  }
  return pts;
}

export default function CandlestickChart({
  bars,
  indicatorData,
  activeOverlays,
  showVolume,
  height = 480,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!containerRef.current || bars.length === 0) return;

    // ── 创建图表 ────────────────────────────────────────────────────────────
    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#0f172a" },
        textColor: "#94a3b8",
      },
      grid: {
        vertLines: { color: "#1e293b" },
        horzLines: { color: "#1e293b" },
      },
      width: containerRef.current.clientWidth,
      height,
      timeScale: {
        borderColor: "#334155",
        timeVisible: true,
        secondsVisible: false,
      },
      rightPriceScale: { borderColor: "#334155" },
      crosshair: {
        vertLine: { color: "#475569", labelBackgroundColor: "#1e293b" },
        horzLine: { color: "#475569", labelBackgroundColor: "#1e293b" },
      },
    });
    chartRef.current = chart;

    // ── 注册图表实例供绘图工具使用 ──────────────────────────────────────────
    // (candleSeries 在下方创建后立即更新)

    // ── 蜡烛图主序列 ────────────────────────────────────────────────────────
    const candleSeries = chart.addCandlestickSeries({
      upColor: "#22c55e",
      downColor: "#ef4444",
      borderVisible: false,
      wickUpColor: "#22c55e",
      wickDownColor: "#ef4444",
    });
    candleSeries.setData(
      bars.map((b) => ({
        time: toUTC(b.timestamp),
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      })),
    );

    // 注册图表实例 + 订阅绘图点击
    registerChartApi(chart, candleSeries, containerRef.current);
    chart.subscribeClick(handleDrawingClick);

    // ── 成交量直方图 ────────────────────────────────────────────────────────
    if (showVolume) {
      const volSeries = chart.addHistogramSeries({
        color: "#475569",
        priceFormat: { type: "volume" },
        priceScaleId: "vol",
      });
      chart.priceScale("vol").applyOptions({
        scaleMargins: { top: 0.85, bottom: 0 },
      });
      volSeries.setData(
        bars.map((b) => ({
          time: toUTC(b.timestamp),
          value: b.volume,
          color: b.close >= b.open ? "#22c55e44" : "#ef444444",
        })),
      );
    }

    // ── EMA 叠加 ────────────────────────────────────────────────────────────
    if (activeOverlays.has("ema") && indicatorData["ema"]?.["ema"]) {
      const emaSeries = chart.addLineSeries({
        color: "#f59e0b",
        lineWidth: 1,
        priceScaleId: "right",
        lastValueVisible: true,
        priceLineVisible: false,
        title: "EMA(20)",
      });
      emaSeries.setData(buildAlignedPoints(bars, indicatorData["ema"]["ema"]));
    }

    // ── 布林带叠加 ──────────────────────────────────────────────────────────
    if (activeOverlays.has("bbands") && indicatorData["bbands"]) {
      const bb = indicatorData["bbands"];
      const lineOpts = {
        color: "#818cf8",
        lineWidth: 1 as const,
        priceScaleId: "right",
        lastValueVisible: false,
        priceLineVisible: false,
      };

      if (bb["bb_upper"]) {
        const upper = chart.addLineSeries({ ...lineOpts, title: "BB上" });
        upper.setData(buildAlignedPoints(bars, bb["bb_upper"]));
      }
      if (bb["bb_mid"]) {
        const mid = chart.addLineSeries({
          ...lineOpts,
          lineStyle: LineStyle.Dashed,
          title: "BB中",
        });
        mid.setData(buildAlignedPoints(bars, bb["bb_mid"]));
      }
      if (bb["bb_lower"]) {
        const lower = chart.addLineSeries({ ...lineOpts, title: "BB下" });
        lower.setData(buildAlignedPoints(bars, bb["bb_lower"]));
      }
    }

    chart.timeScale().fitContent();

    // ── 响应式宽度 ──────────────────────────────────────────────────────────
    const onResize = () => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth });
      }
    };
    window.addEventListener("resize", onResize);

    return () => {
      window.removeEventListener("resize", onResize);
      registerChartApi(null, null, null);
      chart.remove();
      chartRef.current = null;
    };
  }, [bars, indicatorData, activeOverlays, showVolume, height]);

  if (bars.length === 0) {
    return (
      <div
        style={{
          height,
          background: "#0f172a",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "#475569",
          fontSize: 14,
          borderRadius: 8,
        }}
      >
        暂无数据 — 请先拉取行情数据
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      style={{ width: "100%", height }}
      data-testid="candlestick-chart"
    />
  );
}
