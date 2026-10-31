import { useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Bell } from "lucide-react";
import { chartSymbolOf, sameContract } from "../../api";
import { ChartPopover } from "../chartIndicatorEditor";
import { useDisclosureSurface } from "../../uiSurfaces";
import { alertLine, type PriceAlertsHandle } from "../../hooks/usePriceAlerts";
import "./PriceAlertsControl.css";

/** 到价提醒 (2026-10-02): the phone's 看盘 设提醒 button and its popover — this contract's form and
 *  alerts, the rest read-mostly. On the computer the chart toolbar's bell is the 提醒中心 since
 *  2026-10-15 (我的提醒's 新建提醒 reuses `AlertForm`); a crossing no longer lists here — it
 *  is a row of the day in the 提醒中心. */
interface Props {
  alerts: PriceAlertsHandle;
  symbol?: string;
  name?: string;
  priceTick?: number | null;
  lastPrice?: number | null;
  /** The “其他合约” rows switch the chart to that contract (the `symbol` command's target). */
  onSwitchContract?: (symbol: string) => void;
  /** 手机版看盘: a worded button (设提醒) instead of the bell; the popover is the same. */
  label?: string;
}

export function PriceAlertsControl({ alerts, symbol, name, priceTick, lastPrice, onSwitchContract, label }: Props) {
  const buttonRef = useRef<HTMLButtonElement>(null);
  const [popover, setPopover] = useState<{ rect: DOMRect; seq: number }>();
  const openPopover = () => {
    const rect = buttonRef.current?.getBoundingClientRect();
    if (rect) { alerts.refresh(); setPopover({ rect, seq: (popover?.seq ?? 0) + 1 }); }
  };
  useDisclosureSurface("price-alerts", {
    state: popover !== undefined, available: alerts.available,
    apply: open => (open ? openPopover() : setPopover(undefined))
  });
  return (
    <div className="fdw-price-alerts">
      <button type="button" ref={buttonRef} aria-haspopup="dialog"
        aria-label={`${label ?? "到价提醒"}（${alerts.active.length} 条生效）`} title="到价提醒" onClick={openPopover}>
        {label ?? <Bell aria-hidden="true" />}
        {alerts.active.length > 0 ? <span className="fdw-alert-badge">{alerts.active.length}</span> : null}
      </button>
      {/* Portaled out of `.fdw-chart-tools` (overflow: hidden): a fixed-position child of the
       *  toolbar is not clipped on screen, but the observation's visibility walk measures it
       *  against the toolbar's bounds and would report the popover's title row as invisible. */}
      {popover ? createPortal(
        <ChartPopover title="到价提醒" anchorRect={popover.rect} onClose={() => setPopover(undefined)} width={340}>
          <PriceAlertsPopover alerts={alerts} symbol={symbol} name={name}
            priceTick={priceTick} lastPrice={lastPrice} formKey={`${symbol ?? ""}:${popover.seq}`}
            onSwitchContract={onSwitchContract} />
        </ChartPopover>, document.body) : null}
    </div>
  );
}

/** The popover's body. `formKey` remounts the form when the popover reopens or the contract
 *  changes, so the price field re-defaults to the latest price instead of keeping a stale edit. */
function PriceAlertsPopover({ alerts, symbol, name, priceTick, lastPrice, formKey, onSwitchContract }: Props & { formKey: string }) {
  // A 外盘主连 chart's alerts sit on its product's MT5 month (外盘代码统一 2026-10-21): sameContract counts them as this chart's.
  const mine = alerts.active.filter(alert => sameContract(alert.symbol, symbol));
  const others = alerts.active.filter(alert => !sameContract(alert.symbol, symbol));
  const [othersOpen, setOthersOpen] = useState(false);
  useDisclosureSurface("price-alerts-others", {
    state: othersOpen, available: others.length > 0, apply: open => setOthersOpen(open)
  });
  return (
    <div className="fdw-alerts-popover">
      <AlertForm key={formKey} alerts={alerts} symbol={symbol} priceTick={priceTick} lastPrice={lastPrice} />
      <div className="fdw-alert-section">
        <span className="fdw-alert-section-title">{name ?? symbol ?? "本合约"}的生效提醒</span>
        {mine.length ? <ul className="fdw-alert-list">
          {mine.map(alert => (
            <li key={alert.id}>
              <span className="fdw-alert-line"><b data-direction={alert.direction}>{alert.direction === "above" ? "涨到" : "跌到"}</b> {alert.price}{alert.note ? <i>{alert.note}</i> : null}</span>
              <button type="button" aria-label={`删除提醒：${alertLine(alert)}`} title={`删除：${alertLine(alert)}`} onClick={() => alerts.cancel(alert.id)}>×</button>
            </li>
          ))}
        </ul> : <p className="fdw-alert-empty">还没有本合约的提醒</p>}
        {others.length ? (
          <div className="fdw-alert-others">
            <button type="button" className="fdw-alert-others-toggle" aria-expanded={othersOpen}
              onClick={() => setOthersOpen(!othersOpen)}>其他合约 {others.length} 条{othersOpen ? " ▾" : " ▸"}</button>
            {othersOpen ? <ul className="fdw-alert-list">
              {others.map(alert => (
                <li key={alert.id}>
                  <button type="button" className="fdw-alert-switch" aria-label={`${alert.name} ${alert.symbol}`}
                    title={`切到 ${alert.symbol}（价 ${alert.price}）`} onClick={() => onSwitchContract?.(chartSymbolOf(alert.symbol))}>
                    {alert.name} {alert.direction === "above" ? "涨到" : "跌到"} {alert.price}
                  </button>
                  <button type="button" aria-label={`删除提醒：${alertLine(alert)}`} title={`删除：${alertLine(alert)}`} onClick={() => alerts.cancel(alert.id)}>×</button>
                </li>
              ))}
            </ul> : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** The create form: a price (defaulting to the latest price, stepped by one tick), the direction
 *  the Runtime will judge against that quote, an optional note. The direction is never sent —
 *  `create` omits it and the Runtime refuses equal-or-contradictory prices with a Chinese line. */
export function AlertForm({ alerts, symbol, priceTick, lastPrice }: { alerts: PriceAlertsHandle; symbol?: string; priceTick?: number | null; lastPrice?: number | null }) {
  const [priceText, setPriceText] = useState(() => lastPrice != null ? String(lastPrice) : "");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const price = Number(priceText);
  const valid = priceText.trim() !== "" && Number.isFinite(price) && price > 0;
  const direction = valid && lastPrice != null ? (price > lastPrice ? "above" : price < lastPrice ? "below" : "equal") : null;
  const step = (dir: -1 | 1) => () => {
    const base = valid ? price : lastPrice ?? 0;
    const tick = priceTick && priceTick > 0 ? priceTick : 1;
    const next = base + dir * tick;
    if (next > 0) setPriceText(String(Number(next.toFixed(6))));
  };
  const add = async () => {
    if (!valid || !direction || direction === "equal" || busy || !symbol) return;
    setBusy(true);
    const ok = await alerts.create(symbol, price, note.trim() || undefined);
    setBusy(false);
    if (ok) setNote("");
  };
  const directionText = direction === "above" ? `涨到 ${priceText}`
    : direction === "below" ? `跌到 ${priceText}`
    : direction === "equal" ? "改成要提醒的价" : "等最新价";
  return (
    <div className="fdw-alert-form">
      <div className="fdw-alert-price-row">
        <input type="number" inputMode="decimal" aria-label="提醒价" step={priceTick ?? undefined}
          value={priceText} onChange={event => setPriceText(event.target.value)} />
        <div role="group" aria-label="价格步进">
          <button type="button" aria-label="价格减一跳" onClick={step(-1)}>−</button>
          <button type="button" aria-label="价格加一跳" onClick={step(1)}>＋</button>
        </div>
        <span className="fdw-alert-direction" data-direction={direction === "equal" ? "none" : direction ?? "none"}
          title={direction === "equal" ? "现价就是这个数，换一个不同的价" : undefined}>{directionText}</span>
      </div>
      <div className="fdw-alert-note-row">
        <input type="text" aria-label="备注" placeholder="备注（最多 60 字，可空）" maxLength={60}
          value={note} onChange={event => setNote(event.target.value)} />
        <button type="button" className="fdw-alert-add" disabled={!direction || direction === "equal" || busy || !symbol}
          onClick={() => void add()}>{busy ? "设置中…" : "添加"}</button>
      </div>
      {alerts.createError ? <small className="fdw-alert-error" role="alert">{alerts.createError}</small> : null}
    </div>
  );
}
