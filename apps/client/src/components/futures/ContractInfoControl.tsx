import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Info } from "lucide-react";
import type { OrderEstimate } from "@ai-trader/ui-events";
import { getOrderEstimate, type InstrumentCatalogItem, type MarketQuoteSnapshot } from "../../api";
import { ChartPopover } from "../chartIndicatorEditor";
import { SectionCard, StatTiles } from "../Blocks";
import { useDisclosureSurface } from "../../uiSurfaces";
import { CME_HOURS_HELP, cmeHours } from "../../marketSession";
import { decimalsForTick, formatFee, formatNumber, isForeignSymbol, moneyUnit } from "./support";
import { deliveryBadgeText, scheduleLines } from "./deliveryInfo";
import "./ContractInfoControl.css";

/**
 * 合约信息卡 (2026-10-02)：合约头部的 ⓘ 与交割提醒标。卡分四块——一手（乘数、每跳盈亏、
 * 涨跌停）、资金（每手保证金/手续费，优先开仓 1 手的下单估算）、时间（日盘夜盘各段）、
 * 交割（年月、最后可持仓日、剩余交易日）。全部只读；交割数据来自合约目录的
 * deliveryNotice，与持仓行、下单台提示读同一份。外盘合约（美元）没有涨跌停与国内交割规则：时间块写 CME
 * 时段（北京时间），交割块换成“到期”（美元子账户下单的月份合约，取自下单估算）。
 */
interface Props {
  quote: MarketQuoteSnapshot;
  /** The same contract's catalog entry: 交割年月与 deliveryNotice 之源；不在目录时只显示行情里有的。 */
  item?: InstrumentCatalogItem;
  /** Connected Runtime: the 资金 block prefers the read-only order estimate (开仓 1 手). */
  runtimeUrl?: string;
}

export function ContractInfoControl({ quote, item, runtimeUrl }: Props) {
  const infoRef = useRef<HTMLButtonElement>(null);
  const [popover, setPopover] = useState<{ rect: DOMRect; seq: number }>();
  const openPopover = () => {
    const rect = infoRef.current?.getBoundingClientRect();
    if (rect) setPopover({ rect, seq: (popover?.seq ?? 0) + 1 });
  };
  useDisclosureSurface("contract-info", {
    state: popover !== undefined, available: true,
    apply: open => (open ? openPopover() : setPopover(undefined))
  });
  const notice = item?.deliveryNotice ?? null;
  return (
    <div className="fdw-contract-marks">
      {notice ? <button type="button" className="fdw-delivery-badge" data-level={notice.level}
        aria-label={`交割月提醒：${notice.text}`} title={notice.text} onClick={openPopover}>
        {deliveryBadgeText(notice)}
      </button> : null}
      <button type="button" ref={infoRef} className="fdw-contract-info-button" aria-label="合约信息"
        aria-haspopup="dialog" title="合约信息" onClick={openPopover}><Info aria-hidden="true" /></button>
      {/* Portaled out of `.fdw-market-head` (overflow: hidden), same rule as the alerts bell: a
       *  clipped popover would be reported invisible by the observation's visibility walk. */}
      {popover ? createPortal(
        <ChartPopover title="合约信息" anchorRect={popover.rect} onClose={() => setPopover(undefined)} width={360}>
          <ContractInfoCard quote={quote} item={item} runtimeUrl={runtimeUrl} />
        </ChartPopover>, document.body) : null}
    </div>
  );
}

function ContractInfoCard({ quote, item, runtimeUrl }: Props) {
  const [estimate, setEstimate] = useState<OrderEstimate | undefined>();
  useEffect(() => {
    setEstimate(undefined);
    if (!runtimeUrl) return;
    const controller = new AbortController();
    getOrderEstimate(runtimeUrl, { symbol: quote.symbol, side: "buy", offset: "open", volume: 1 }, controller.signal)
      .then(setEstimate).catch(() => { if (!controller.signal.aborted) setEstimate(undefined); });
    return () => controller.abort();
  }, [runtimeUrl, quote.symbol]);
  const decimals = decimalsForTick(quote.priceTick);
  const perTick = quote.priceTick != null && quote.volumeMultiple != null
    ? quote.priceTick * quote.volumeMultiple : null;
  const unit = moneyUnit(quote.symbol);
  const margin = estimate?.margin ?? quote.marginPerLot ?? null;
  const fee = estimate?.fee ?? quote.commissionPerLot ?? null;
  const schedule = scheduleLines(quote.tradingSchedule);
  const notice = item?.deliveryNotice ?? null;
  const deliveryMonth = item && item.deliveryYear > 0
    ? `${item.deliveryYear}-${String(item.deliveryMonth).padStart(2, "0")}` : "—";
  // 外盘 (外盘盘点 3B 2026-10-21): no 涨跌停, no 日盘/夜盘, no domestic delivery rule — CME's hours and the month
  // the USD sub-account orders (the estimate names it) instead.
  if (isForeignSymbol(quote.symbol)) {
    const cme = cmeHours(quote.tradingSchedule);
    const month = estimate && /^CME\./i.test(estimate.symbol) ? estimate.symbol : undefined;
    return (
      <div className="fdw-contract-info-card" data-testid="contract-info-card">
        <SectionCard title="一手" note="一手规模 = 最新价 × 乘数">
          <StatTiles columns={2} items={[
            { label: "合约乘数", value: quote.volumeMultiple != null ? formatNumber(quote.volumeMultiple) : "—", note: `${unit} / 点` },
            { label: "跳一下盈亏", value: perTick != null ? formatNumber(perTick) : "—", note: `${unit} / 手 = 最小变动 × 乘数` },
          ]} />
        </SectionCard>
        <SectionCard title="资金" note="开仓 1 手口径的参考值，实际以成交回报为准">
          <StatTiles columns={2} items={[
            { label: "每手保证金", value: margin != null ? `约 ${formatNumber(margin, 0)}` : "—", note: `${unit} / 手` },
            { label: "每手手续费", value: fee != null ? `约 ${formatFee(fee)}` : "—", note: `${unit} / 手 · 每边` },
          ]} />
        </SectionCard>
        <SectionCard title="时间" note={cme ? "北京时间" : "暂无交易时段数据"}
          hint={cme ? CME_HOURS_HELP : undefined}>
          {cme ? <div className="fdw-contract-schedule" data-testid="contract-cme-schedule">
            {cme.periods.flatMap((period, index) => [
              ...(index > 0 ? [<p key={`pause-${index}`}><b>盘中休息</b><span>{cme.periods[index - 1].end}–{period.start}</span></p>] : []),
              <p key={`trade-${index}`}><b>交易</b><span>{period.start}–{period.end}</span></p>])}
            <p><b>每日停盘</b><span>{cme.closes}–{cme.opens}</span></p>
            <p><b>周末休市</b><span>周六 {cme.closes}–周一 {cme.opens}</span></p>
          </div> : <p className="fdw-contract-schedule-empty">—</p>}
        </SectionCard>
        {month ? <SectionCard title="到期" tone={notice ? "warning" : undefined}
          hint="图表是快期外盘主连，不到期；美元子账户下单用当时的主力月份合约，到期前会提醒换月，到期按结算价了结。">
          <StatTiles columns={2} items={[{ label: "下单月份", value: month }]} />
          {notice ? <p className="fdw-contract-delivery-line" data-level={notice.level}>{notice.text}</p> : null}
        </SectionCard> : null}
      </div>
    );
  }
  return (
    <div className="fdw-contract-info-card" data-testid="contract-info-card">
      <SectionCard title="一手" note="一手规模 = 最新价 × 乘数">
        <StatTiles columns={2} items={[
          { label: "合约乘数", value: quote.volumeMultiple != null ? formatNumber(quote.volumeMultiple) : "—", note: `${unit} / 点` },
          { label: "跳一下盈亏", value: perTick != null ? formatNumber(perTick) : "—", note: `${unit} / 手 = 最小变动 × 乘数` },
          { label: "涨停价", value: formatNumber(quote.upperLimit, decimals) },
          { label: "跌停价", value: formatNumber(quote.lowerLimit, decimals) },
        ]} />
      </SectionCard>
      <SectionCard title="资金" note="开仓 1 手口径的参考值，实际以成交回报为准">
        <StatTiles columns={2} items={[
          { label: "每手保证金", value: margin != null ? `约 ${formatNumber(margin, 0)}` : "—", note: `${unit} / 手` },
          { label: "每手手续费", value: fee != null ? `约 ${formatFee(fee)}` : "—", note: `${unit} / 手` },
        ]} />
      </SectionCard>
      <SectionCard title="时间" note={schedule.day || schedule.night ? undefined : "该合约暂无交易时段数据"}>
        {schedule.day || schedule.night ? <div className="fdw-contract-schedule">
          <p><b>日盘</b><span>{schedule.day || "—"}</span></p>
          <p><b>夜盘</b><span>{schedule.night || "无夜盘"}</span></p>
        </div> : <p className="fdw-contract-schedule-empty">—</p>}
      </SectionCard>
      <SectionCard title="交割" tone={notice ? "warning" : undefined}
        note={!notice ? "剩余交易日含今天，按交易所日历扣节假日"
          : notice.estimated === false ? "剩余交易日含今天，已扣交易所节假日"
            : "剩余交易日含今天；交易日历暂缺，按工作日估算，长假前后实际更少"}>
        <StatTiles columns={3} items={[
          { label: "交割月", value: deliveryMonth },
          { label: "最后可持仓日", value: notice ? notice.lastHoldDay : "—",
            note: notice?.kind === "expiry" ? "第三个周五 · 现金交割" : "个人账户此日前须平仓" },
          { label: "剩余交易日", value: notice ? String(notice.tradingDaysLeft) : "—" },
        ]} />
        {notice ? <p className="fdw-contract-delivery-line" data-level={notice.level}>{notice.text}</p> : null}
      </SectionCard>
    </div>
  );
}
