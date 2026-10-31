import { useEffect, useRef, useState } from "react";
import { getAdminServers, type AdminServerHost, type AdminServerRange, type AdminServersSnapshot } from "../../../api";
import { Segmented } from "../../settings/controls";
import { SectionHeader, fmtTime, loadLevel, type LoadLevel } from "./shared";

export const SERVER_RANGES: Array<{ value: AdminServerRange; label: string }> = [
  { value: "1h", label: "1 小时" },
  { value: "24h", label: "24 小时" },
  { value: "7d", label: "7 天" },
];
const REFRESH_MS = 60_000;

const percentOf = (used: number | undefined, total: number | undefined) =>
  used == null || !total ? null : Math.round(used / total * 100);
const gb = (value: number | undefined, digits = 1) => value == null ? "—" : `${Number(value.toFixed(digits))}`;

/** Gauge figures of one host; the disk gauge is the system disk (the top-level total),
 *  the other disks of a multi-disk Windows host are listed in small print. */
export function hostFigures(host: AdminServerHost) {
  const r = host.resources ?? {};
  const cpu = r.cpuPercent == null ? null : Math.round(r.cpuPercent);
  const mem = percentOf(r.memUsedMB, r.memTotalMB);
  const disk = percentOf(r.diskUsedGB, r.diskTotalGB);
  const disks = r.disks ?? [];
  const systemIndex = disks.findIndex(item => item.totalGB === r.diskTotalGB && item.usedGB === r.diskUsedGB);
  const otherDisks = disks.length > 1 ? disks.filter((_, index) => index !== (systemIndex < 0 ? 0 : systemIndex)) : [];
  return { cpu, mem, disk, otherDisks, r };
}

/** 正常 / 告警 (failed service or a gauge ≥80%) / 未采集 (the probe did not answer) / 离线. */
export function hostState(host: AdminServerHost): "ok" | "alert" | "idle" | "down" {
  if (!host.reachability || host.reachability === "unknown") return "idle";
  if (host.reachability !== "online") return "down";
  const { cpu, mem, disk } = hostFigures(host);
  return host.servicesSummary.failed > 0 || [cpu, mem, disk].some(value => loadLevel(value) === "danger") ? "alert" : "ok";
}
const STATE_LABEL = { ok: "正常", alert: "告警", idle: "未采集", down: "离线" } as const;

/** The 服务器 section: an online/alert summary with the range switch, the request and
 *  release chain, and one card per host — three 270° gauges, the CPU curve, load,
 *  service capsules and the host's spec. Re-reads on a range switch and every minute. */
export function ServersSection({ baseUrl, range, onRangeChange }: {
  baseUrl: string;
  range: AdminServerRange;
  onRangeChange: (range: AdminServerRange) => void;
}) {
  const [snapshot, setSnapshot] = useState<AdminServersSnapshot>();
  const [failed, setFailed] = useState(false);
  const request = useRef(0);

  useEffect(() => {
    const read = () => {
      const id = ++request.current;
      getAdminServers(baseUrl, range)
        .then(result => { if (id === request.current) { setSnapshot(result); setFailed(false); } })
        .catch(() => { if (id === request.current) setFailed(true); });
    };
    read();
    const timer = window.setInterval(read, REFRESH_MS);
    return () => { window.clearInterval(timer); request.current += 1; };
  }, [baseUrl, range]);

  const ready = snapshot?.status === "ok" ? snapshot : undefined;
  const hosts = ready?.hosts ?? [];
  const states = hosts.map(hostState);
  const online = hosts.filter(host => host.reachability === "online").length;
  const idle = states.filter(state => state === "idle").length;
  const alerts = states.filter(state => state === "alert" || state === "down").length;
  const rangeLabel = SERVER_RANGES.find(item => item.value === range)?.label ?? range;

  const hint = ready ? `项目相关的 ${hosts.length} 台主机 · 运维台每 5 分钟采集一次` : "项目相关的主机 · 运维台每 5 分钟采集一次";
  const header = <SectionHeader title="服务器" hint={hint} action={<>
    {ready ? <span className="fdw-admin-servers-summary" role="status" data-stale={ready.stale ? "" : undefined}>
      <span className="fdw-admin-dot" data-tone={alerts ? "warn" : "ok"} />
      {online} 台在线 · {idle} 台未采集 · {alerts} 告警
      {ready.stale ? <strong>数据已过期（上次 {fmtTime(ready.fetchedAt)}）</strong> : null}
    </span> : null}
    <Segmented label="时间范围" value={range} options={SERVER_RANGES} onChange={onRangeChange} />
  </>} />;

  if (snapshot?.status === "unconfigured") return <>
    {header}
    <div className="fdw-admin-section-body"><p className="fdw-admin-empty">运维台令牌未配置</p></div>
  </>;
  if (!ready) return <>
    {header}
    <div className="fdw-admin-section-body">
      <p className="fdw-admin-empty">{failed ? "运维台暂时读不到，一分钟后自动重试" : "读取中"}</p>
    </div>
  </>;
  const byId = new Map(hosts.map(host => [host.id, host]));
  return <>
    {header}
    <div className="fdw-admin-section-body">
      {failed ? <p className="fdw-admin-load-error" role="alert">刷新失败，显示的是上次读到的数据</p> : null}
      <ServerChain byId={byId} updatedAt={ready.updatedAt} />
      {hosts.length === 0 ? <p className="fdw-admin-empty">运维台没有返回主机</p> : (
        <div className="fdw-admin-hosts" role="list" aria-label="主机">
          {hosts.map((host, index) => <HostCard key={host.id} host={host} state={states[index]} rangeLabel={rangeLabel} />)}
        </div>
      )}
    </div>
  </>;
}

/** The request and release chain: a fixed drawing of the project's topology whose
 *  nodes take their status from the matching host's reachability. */
function ServerChain({ byId, updatedAt }: { byId: Map<string, AdminServerHost>; updatedAt: string }) {
  const tone = (id: string) => {
    const host = byId.get(id);
    if (!host) return "idle";
    const state = hostState(host);
    return state === "ok" ? "ok" : state === "idle" ? "idle" : state === "alert" ? "warn" : "danger";
  };
  const runtime = byId.get("tencent-ai-trader")?.resources;
  const runtimeDetail = runtime ? [
    runtime.containers != null ? `${runtime.containers} 个容器` : null,
    runtime.uptimeSeconds != null ? `运行 ${uptimeText(runtime.uptimeSeconds)}` : null,
  ].filter(Boolean).join(" · ") : "";
  const node = (place: string, title: string, id?: string, detail?: string, main = false) => (
    <div className="fdw-admin-chain-node" data-main={main ? "" : undefined} data-tone={id ? tone(id) : undefined}>
      <span className="fdw-admin-chain-place">{id ? <span className="fdw-admin-dot" data-tone={tone(id)} /> : null}{place}</span>
      <strong>{title}</strong>
      {detail ? <span className="fdw-admin-chain-detail">{detail}</span> : null}
    </div>
  );
  const link = (label: string) => <div className="fdw-admin-chain-link" aria-hidden="true"><span>{label}</span><i /></div>;
  const builder = (id: string, label: string) => <span className="fdw-admin-chain-builder">
    <span className="fdw-admin-dot" data-tone={tone(id)} />{label}
  </span>;
  return <section className="fdw-admin-panel fdw-admin-chain" aria-label="请求与发布链路">
    <header className="fdw-admin-panel-head">
      <h3>请求与发布链路</h3>
      <span className="fdw-admin-mono-note">采集于 {fmtTime(updatedAt)} · UTC+8</span>
    </header>
    <div className="fdw-admin-chain-row">
      {node("用户", "网页 · 手机 · 桌面")}
      {link("HTTPS")}
      {node("阿里云 · 杭州", "网页 · 更新源", "aliyun-qilinshuzhi")}
      {link("API · 推送")}
      {node("腾讯云 · 上海", "Runtime · 模拟引擎", "tencent-ai-trader", runtimeDetail, true)}
      {link("行情订阅")}
      {node("快期行情", "多账号接力")}
    </div>
    <div className="fdw-admin-chain-builders">
      <span className="fdw-admin-chain-caption">构建与测试</span>
      {builder("mac-mini-cc", "后台 Mac · Mac CI 与签名")}
      {builder("nas-ci", "NAS · Linux CI")}
      {builder("gpu-4090d", "4090D · Windows CI")}
      <span className="fdw-admin-chain-caption">→ 发布到上海 / 杭州</span>
      <span className="fdw-admin-chain-egress">{builder("zgo-la", "美国出口 · CFTC 持仓数据")}</span>
    </div>
  </section>;
}

function uptimeText(seconds: number) {
  const days = Math.floor(seconds / 86_400);
  return days >= 1 ? `${days} 天` : `${Math.max(0, Math.floor(seconds / 3600))} 小时`;
}

function HostCard({ host, state, rangeLabel }: { host: AdminServerHost; state: ReturnType<typeof hostState>; rangeLabel: string }) {
  const { cpu, mem, disk, otherDisks, r } = hostFigures(host);
  const { running, failed, total } = host.servicesSummary;
  const cpuValues = host.series.map(point => point.cpu).filter((value): value is number => typeof value === "number");
  const peak = cpuValues.length ? Math.round(Math.max(...cpuValues)) : null;
  const other = Math.max(0, total - running - failed);
  const load = [r.load1, r.load5, r.load15].map(value => value == null ? "—" : value.toFixed(2)).join(" ");
  const spec = [
    r.cpuCores != null ? `${r.cpuCores} 核` : null,
    r.memTotalMB != null ? `${gb(r.memTotalMB / 1024)} GB` : null,
    r.diskTotalGB != null ? `${gb(r.diskTotalGB, 0)} GB` : null,
  ].filter(Boolean).join(" · ");
  return <article className="fdw-admin-host" role="listitem" aria-label={host.displayName} data-state={state}>
    <header className="fdw-admin-host-head">
      <div className="fdw-admin-host-title">
        <strong>{host.displayName}</strong>
        {host.role ? <span>{host.role}</span> : null}
      </div>
      <span className="fdw-admin-state-chip" data-tone={state}><span className="fdw-admin-dot" />{STATE_LABEL[state]}</span>
    </header>
    <div className="fdw-admin-gauges">
      <Gauge label="CPU" value={cpu} detail={r.cpuCores != null ? `${r.cpuCores} 核` : "—"} />
      <Gauge label="内存" value={mem} detail={r.memTotalMB != null ? `${gb((r.memUsedMB ?? 0) / 1024)}/${gb(r.memTotalMB / 1024)} GB` : "—"} />
      <Gauge label="磁盘" value={disk} detail={r.diskTotalGB != null ? `${gb(r.diskUsedGB, 0)}/${gb(r.diskTotalGB, 0)} GB` : "—"} />
    </div>
    {otherDisks.length ? <p className="fdw-admin-host-disks">
      {otherDisks.map(item => `${item.name ?? "其他盘"} ${gb(item.usedGB, 0)}/${gb(item.totalGB, 0)} GB`).join(" · ")}
    </p> : null}
    <div className="fdw-admin-host-curve">
      <div className="fdw-admin-host-curve-head">
        <span>{rangeLabel} CPU{peak != null ? ` · 峰值 ${peak}%` : ""}</span><span className="fdw-admin-mono">负载 {load}</span>
      </div>
      <CpuCurve series={host.series} peak={peak} />
    </div>
    <div className="fdw-admin-capsules" aria-label="服务">
      {total === 0 ? <span className="fdw-admin-capsule" data-tone="idle">无登记服务</span> : <>
        {running ? <span className="fdw-admin-capsule" data-tone="ok"><span className="fdw-admin-dot" />运行 {running}</span> : null}
        {failed ? <span className="fdw-admin-capsule" data-tone="danger"><span className="fdw-admin-dot" />失败 {failed}</span> : null}
        {other ? <span className="fdw-admin-capsule" data-tone="idle"><span className="fdw-admin-dot" />其他 {other}</span> : null}
      </>}
    </div>
    <footer className="fdw-admin-host-foot">
      <span>{host.id}</span>
      {spec ? <span>{spec}</span> : null}
      <span className="fdw-admin-host-up">运行 {r.uptimeSeconds != null ? uptimeText(r.uptimeSeconds) : "—"}</span>
    </footer>
  </article>;
}

// The 270° arc: r=39 in a 100-unit box, starting bottom-left (rotated 135°).
const ARC = 2 * Math.PI * 39 * 0.75;

/** One 270° gauge: the arc filled to the value in its level's color (≥60% warning,
 *  ≥80% danger), a large monospace figure in the middle, label and detail below. */
export function Gauge({ label, value, detail }: { label: string; value: number | null; detail: string }) {
  const level: LoadLevel = loadLevel(value);
  const fill = value == null ? 0 : ARC * Math.max(0, Math.min(100, value)) / 100;
  return <div className="fdw-admin-gauge" data-level={level} aria-label={`${label} ${value == null ? "—" : `${value}%`}`} role="img">
    <div className="fdw-admin-gauge-dial">
      <svg viewBox="0 0 100 100" aria-hidden="true">
        <circle cx="50" cy="50" r="46" className="fdw-admin-gauge-rim" strokeDasharray={`${(2 * Math.PI * 46 * 0.75).toFixed(1)} 400`} transform="rotate(135 50 50)" />
        <circle cx="50" cy="50" r="39" className="fdw-admin-gauge-track" strokeDasharray={`${ARC.toFixed(1)} 400`} transform="rotate(135 50 50)" />
        {value != null ? <circle cx="50" cy="50" r="39" className="fdw-admin-gauge-fill" strokeDasharray={`${fill.toFixed(1)} 400`} transform="rotate(135 50 50)" /> : null}
      </svg>
      <span className="fdw-admin-gauge-value">{value == null ? "—" : value}{value == null ? null : <small>%</small>}</span>
    </div>
    <span className="fdw-admin-gauge-label">{label}</span>
    <span className="fdw-admin-gauge-detail">{detail}</span>
  </div>;
}

/** The CPU curve over the selected range: a line plus a faint area, drawn on time. The
 *  height scales to the range's peak (named in the header) so a quiet host still shows
 *  its shape; a busy one keeps the full 0–100 scale. */
function CpuCurve({ series, peak }: { series: AdminServerHost["series"]; peak: number | null }) {
  const points = series
    .map(point => ({ t: Date.parse(point.at), v: point.cpu }))
    .filter((point): point is { t: number; v: number } => Number.isFinite(point.t) && typeof point.v === "number");
  if (points.length < 2) return <p className="fdw-admin-host-curve-empty">曲线数据不足</p>;
  const first = points[0].t, span = Math.max(1, points[points.length - 1].t - first);
  const top = Math.min(100, Math.max(10, (peak ?? 100) * 1.15));
  const xy = points.map(point => [(point.t - first) / span * 300, 44 - Math.max(0, Math.min(top, point.v)) / top * 40 - 2]);
  const line = xy.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  return <svg className="fdw-admin-curve" viewBox="0 0 300 44" preserveAspectRatio="none" aria-hidden="true">
    <path d={`M${xy[0][0].toFixed(1)},44 L${line.replaceAll(" ", " L")} L${xy[xy.length - 1][0].toFixed(1)},44 Z`} />
    <polyline points={line} vectorEffect="non-scaling-stroke" />
  </svg>;
}
