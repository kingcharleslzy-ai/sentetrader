import { useEffect, useRef, useState } from "react";
import { ColorType, LineSeries, TickMarkType, createChart, type IChartApi, type ISeriesApi, type Time, type UTCTimestamp } from "lightweight-charts";
import { getAdminRum, type AdminRumFigures, type AdminRumMetric, type AdminRumSnapshot, type AdminServerRange } from "../../../api";
import { Segmented, Select } from "../../settings/controls";
import { SERVER_RANGES } from "./ServersSection";
import { AdminTiles, SectionHeader, fmtTime } from "./shared";

const REFRESH_MS = 60_000;
type Ready = Extract<AdminRumSnapshot, { status: "ok" }>;
type MetricKey = keyof Ready["metrics"];
const CLIENTS = [["web", "网页"], ["desktop", "桌面"], ["mobile", "手机"]] as const;

/** The metrics in reading order, with their names on this page. */
export const RUM_METRICS: Array<{ key: MetricKey; label: string }> = [
  { key: "lcp", label: "页面打开 LCP" },
  { key: "inp", label: "操作响应 INP" },
  { key: "cls", label: "版面跳动 CLS" },
  { key: "fcp", label: "首次绘制 FCP" },
  { key: "ttfb", label: "首字节 TTFB" },
  { key: "price_latency", label: "价格延迟" },
  { key: "kline_first_paint", label: "K 线首屏" },
  { key: "sse_down", label: "断线时长" },
  { key: "memory", label: "页面内存" },
];
const ERROR_LABELS: Record<string, string> = {
  chunk: "资源加载", network: "网络请求", type: "类型错误", reference: "引用错误", range: "范围错误",
  syntax: "语法错误", security: "安全限制", rejection: "未处理的拒绝", other: "其他",
};
const NETWORK_LABELS: Record<string, string> = { wifi: "Wi-Fi", ethernet: "有线", cellular: "蜂窝", unknown: "未知" };

/** One figure with its unit: ms under 10 s, seconds above; CLS shows as the standard score (‰ / 1000).
 *  `atLeast` marks a quantile that landed in the open last bucket: only its lower bound is known. */
export function rumValue(value: number | null | undefined, unit: string, atLeast = false): string {
  if (value == null) return "—";
  const text = unit === "‰" ? (value / 1000).toFixed(3)
    : unit === "MB" ? `${Math.round(value)} MB`
    : value >= 10_000 ? `${(value / 1000).toFixed(1)} 秒` : `${Math.round(value)} ms`;
  return atLeast ? `≥ ${text}` : text;
}

/** A quantile of a metric with its "at least" mark. */
function quantileText(figures: AdminRumFigures | undefined, key: "p50" | "p75" | "p95", unit: string): string {
  return rumValue(figures?.[key], unit, Boolean(figures?.atLeast?.includes(key)));
}

/** The 用户体验 section (真实用户监测, 2026-10-13): tiles of the headline quantiles, the chosen
 *  metric's p50/p95 over time, and the per-client and per-network breakdown. Re-reads on a range
 *  switch and every minute, like 服务器. */
export function ExperienceSection({ baseUrl, range, onRangeChange }: {
  baseUrl: string;
  range: AdminServerRange;
  onRangeChange: (range: AdminServerRange) => void;
}) {
  const [snapshot, setSnapshot] = useState<AdminRumSnapshot>();
  const [failed, setFailed] = useState(false);
  const [metric, setMetric] = useState<MetricKey>("price_latency");
  const request = useRef(0);

  useEffect(() => {
    const read = () => {
      const id = ++request.current;
      getAdminRum(baseUrl, range)
        .then(result => { if (id === request.current) { setSnapshot(result); setFailed(false); } })
        .catch(() => { if (id === request.current) setFailed(true); });
    };
    read();
    const timer = window.setInterval(read, REFRESH_MS);
    return () => { window.clearInterval(timer); request.current += 1; };
  }, [baseUrl, range]);

  const ready = snapshot?.status === "ok" ? snapshot : undefined;
  // A sampled deployment keeps only a share of the page sessions: every figure here is that share, so the rate
  // is always on screen (not only when someone remembers the environment variable).
  const headerHint = [
    snapshot && !snapshot.enabled ? "采集已关闭，以下是关闭前的数据"
      : ready ? `更新于 ${fmtTime(new Date(ready.to * 1000).toISOString())}` : undefined,
    ready && ready.sampleRate < 1 ? `仅采样 ${Math.round(ready.sampleRate * 100)}% 的页面会话` : undefined,
  ].filter(Boolean).join(" · ") || undefined;
  const header = <SectionHeader title="用户体验" helpKey="experience"
    help={<>各端登录后的交易页每分钟上报一次匿名技术指标（次数、总和与分段计数，不含对话、合约、委托或网址），保留 14 天。分位数由分段计数估算；价格延迟＝行情源捕获报价到浏览器画出这一帧，已用服务器时间校正。Runtime 环境变量 AI_TRADER_RUM_ENABLED=0 关闭采集。</>}
    hint={headerHint}
    action={<Segmented label="时间范围" value={range} options={SERVER_RANGES} onChange={onRangeChange} />} />;

  if (!ready) return <>
    {header}
    <div className="fdw-admin-section-body">
      <p className="fdw-admin-empty">{failed ? "读取失败，一分钟后自动重试" : snapshot?.status === "unavailable" ? "这个 Runtime 没有接收器" : "读取中"}</p>
    </div>
  </>;
  const m = ready.metrics;
  const chosen = m[metric];
  const errors = ready.counters.errors;
  return <>
    {header}
    <div className="fdw-admin-section-body">
      {failed ? <p className="fdw-admin-load-error" role="alert">刷新失败，显示的是上次读到的数据</p> : null}
      <AdminTiles label="用户体验" items={[
        { label: "会话", value: ready.totals.sessions, note: `${ready.totals.users} 位用户 · 可见 ${Math.round(ready.totals.visibleMinutes)} 分钟` },
        { label: "页面打开 p75", value: quantileText(m.lcp, "p75", "ms"), note: m.lcp ? `${m.lcp.n} 次` : "无数据" },
        { label: "操作响应 p75", value: quantileText(m.inp, "p75", "ms"), note: m.inp ? `${m.inp.n} 次` : "无数据" },
        { label: "价格延迟 p95", value: quantileText(m.price_latency, "p95", "ms"), note: m.price_latency ? `p50 ${quantileText(m.price_latency, "p50", "ms")}` : "无数据" },
        { label: "页面报错", value: errors.total },
        { label: "断线重连", value: ready.counters.sseReconnect.total, note: m.sse_down ? `断线 p95 ${quantileText(m.sse_down, "p95", "ms")}` : undefined },
      ]} />
      <section className="fdw-admin-panel" aria-label="指标时序">
        <header className="fdw-admin-panel-head">
          <h3>时序</h3>
          <span className="fdw-admin-panel-hint">p50 · p95，每 {ready.slotSeconds >= 3600 ? `${ready.slotSeconds / 3600} 小时` : `${ready.slotSeconds / 60} 分钟`}一点</span>
          <div className="fdw-admin-panel-actions">
            <Select label="指标" value={metric} options={RUM_METRICS.map(item => ({ value: item.key, label: item.label }))}
              onChange={value => setMetric(value as MetricKey)} />
          </div>
        </header>
        {chosen && chosen.series.length ? <RumSeriesChart series={chosen.series} unit={chosen.unit} />
          : <p className="fdw-admin-host-curve-empty">这段时间没有数据</p>}
      </section>
      <div className="fdw-admin-table-card">
        <table className="fdw-admin-table fdw-admin-rum-table" aria-label="按客户端">
          <thead>
            <tr><th>p50 / p95</th>{CLIENTS.map(([key, label]) =>
              <th key={key}>{label}<span className="fdw-admin-rum-sub">{ready.totals.byClient[key]?.sessions ?? 0} 会话</span></th>)}</tr>
          </thead>
          <tbody>
            {RUM_METRICS.map(item => <tr key={item.key}>
              <th scope="row">{item.label}</th>
              {CLIENTS.map(([key]) => <td key={key} className="fdw-admin-mono">{pair(m[item.key]?.byClient[key], m[item.key]?.unit ?? "ms")}</td>)}
            </tr>)}
            <tr><th scope="row">页面报错</th>{CLIENTS.map(([key]) => <td key={key} className="fdw-admin-mono">{errors.byClient[key] ?? 0}</td>)}</tr>
            <tr><th scope="row">断线重连</th>{CLIENTS.map(([key]) => <td key={key} className="fdw-admin-mono">{ready.counters.sseReconnect.byClient[key] ?? 0}</td>)}</tr>
          </tbody>
        </table>
      </div>
      <div className="fdw-admin-overview-grid">
        <section className="fdw-admin-panel" aria-label="网络类型">
          <header className="fdw-admin-panel-head"><h3>网络类型</h3><span className="fdw-admin-panel-hint">会话数</span></header>
          <Breakdown rows={Object.entries(ready.totals.byNetwork).map(([key, value]) => [NETWORK_LABELS[key] ?? key, value.sessions])} empty="没有会话" />
        </section>
        <section className="fdw-admin-panel" aria-label="报错类别">
          <header className="fdw-admin-panel-head"><h3>报错类别</h3><span className="fdw-admin-panel-hint">次数</span></header>
          <Breakdown rows={Object.entries(errors.byCategory).map(([key, n]) => [ERROR_LABELS[key] ?? key, n])} empty="没有报错" />
        </section>
      </div>
    </div>
  </>;
}

/** "p50 / p95"; each figure keeps its unit on its line, a narrow table wraps at the slash. */
function pair(figures: AdminRumFigures | undefined, unit: string) {
  if (!figures || !figures.n) return "—";
  const keep = (text: string) => text.replaceAll(" ", "\u00a0");
  return `${keep(quantileText(figures, "p50", unit))} / ${keep(quantileText(figures, "p95", unit))}`;
}

function Breakdown({ rows, empty }: { rows: Array<[string, number]>; empty: string }) {
  if (!rows.length) return <p className="fdw-admin-empty">{empty}</p>;
  const sorted = [...rows].sort((a, b) => b[1] - a[1]);
  return <dl className="fdw-admin-rum-breakdown">
    {sorted.map(([label, value]) => <div key={label}><dt>{label}</dt><dd className="fdw-admin-mono">{value}</dd></div>)}
  </dl>;
}

const shanghai = (options: Intl.DateTimeFormatOptions) => {
  const format = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", hour12: false, ...options });
  return (time: Time) => format.format(new Date(Number(time) * 1000));
};
const clock = shanghai({ hour: "2-digit", minute: "2-digit" });
const day = shanghai({ month: "2-digit", day: "2-digit" });
const stamp = shanghai({ month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });

const cssVar = (name: string, fallback: string) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

/** p50 and p95 over time on the console's own palette (re-read when the theme flips). */
function RumSeriesChart({ series, unit }: { series: AdminRumMetric["series"]; unit: string }) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<{ api: IChartApi; p50: ISeriesApi<"Line">; p95: ISeriesApi<"Line"> } | undefined>(undefined);

  useEffect(() => {
    if (!container.current) return;
    const api = createChart(container.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, attributionLogo: false, fontSize: 11 },
      grid: { vertLines: { visible: false } },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.1, bottom: 0.05 } },
      // 北京时间, like every other time on this page.
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false, fixLeftEdge: true, fixRightEdge: true,
        tickMarkFormatter: (time: Time, type: TickMarkType) => type <= TickMarkType.DayOfMonth ? day(time) : clock(time) },
      localization: {
        timeFormatter: stamp,
        priceFormatter: (value: number) => rumValue(value, unit),
      },
      handleScroll: false,
      handleScale: false,
    });
    const p50 = api.addSeries(LineSeries, { lineWidth: 2, priceLineVisible: false, lastValueVisible: false, title: "p50" });
    const p95 = api.addSeries(LineSeries, { lineWidth: 2, lineStyle: 2, priceLineVisible: false, lastValueVisible: false, title: "p95" });
    chart.current = { api, p50, p95 };
    const paint = () => {
      const ink = cssVar("--fdw-muted", "#5c615b");
      api.applyOptions({ layout: { textColor: ink }, grid: { horzLines: { color: cssVar("--fdw-line", "#e3e3dd") } } });
      p50.applyOptions({ color: cssVar("--fdw-accent", "#2f5bff") });
      p95.applyOptions({ color: cssVar("--warning", "#8f5300") });
    };
    paint();
    const observer = new MutationObserver(paint);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-app-theme"] });
    return () => { observer.disconnect(); api.remove(); chart.current = undefined; };
  }, [unit]);

  useEffect(() => {
    const current = chart.current;
    if (!current) return;
    const points = (key: "p50" | "p95") => series.filter(point => point[key] != null)
      .map(point => ({ time: point.t as UTCTimestamp, value: point[key] as number }));
    current.p50.setData(points("p50"));
    current.p95.setData(points("p95"));
    current.api.timeScale().fitContent();
  }, [series, unit]);

  return <div className="fdw-admin-rum-chart" ref={container} role="img" aria-label="所选指标的 p50 与 p95 时序" />;
}
