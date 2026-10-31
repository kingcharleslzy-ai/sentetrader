import { Minus, Plus } from "lucide-react";
import { useEffect, useState } from "react";
import type { Dispatch, SetStateAction, ReactNode } from "react";
import type { OrderEstimate } from "@ai-trader/ui-events";
import { useDisclosureSurface, useUiSurface } from "../../uiSurfaces";
import { foreignQuoteOnly, getOrderEstimate, type AccountPosition, type DeliveryNotice, type MarketWorkbenchSnapshot, type StopMotion } from "../../api";
import { StopMotionFields } from "./StopMotionFields";
import "./TradeTicket.css";
import { decimalsForTick, formatFee, formatNumber, moneyUnit, targetPlanPreview, type CloseBucket, type ManualSimulationRequest, type PriceMode, type TicketDraft } from "./support";
import { deliveryTicketHint } from "./deliveryInfo";
import type { ManualOrderOutcome } from "../../hooks/useSimulationOrders";
import { readNumber, STOP_PRICE_RULE as STOP_RULE, TAKE_PRICE_RULE as TAKE_RULE, useNumberText, type NumberRule } from "../../numberField";
import { HelpTip } from "../Blocks";
import { usePlaybooks } from "../playbooks/PlaybookPage";
import "./ContractInfoControl.css";

type TradeMode = "open" | "close" | "target";

/** What one position can close now: its volume less the frozen (working close orders) lots. */
const closable = (position: AccountPosition) =>
  Math.max(0, position.volume - (position.frozenToday ?? 0) - (position.frozenHistorical ?? 0));

/** The most lots one ticket sends (2026-10-13, #745/#789): the order estimate's own limit; an exchange
 *  daily open limit (quote.openLimit) lowers it for an open. The Runtime still checks every order. */
export const MAX_TICKET_VOLUME = 1000;
const TARGET_RULE: NumberRule = { label: "目标净持仓", min: -999, max: 999, integer: true, unit: "手" };

export function TradeTicket({
  snapshot,
  runtimeUrl,
  deliveryNotice,
  volume,
  setVolume,
  selectedPrice,
  selectedPosition,
  simulationEnabled,
  simulationBlockedReason,
  busy,
  draft,
  closeRequest,
  currentPositions,
  onSubmit,
  onTargetPosition,
  variant = "desktop"
}: {
  snapshot: MarketWorkbenchSnapshot;
  /** Connected Runtime base URL: the read-only order estimate (保证金约 / 可开手数) comes from it. */
  runtimeUrl?: string;
  /** 交割月提醒 (2026-10-02): the open mode shows one reminder line; it never disables anything. */
  deliveryNotice?: DeliveryNotice | null;
  volume: number;
  setVolume: Dispatch<SetStateAction<number>>;
  selectedPrice?: number;
  selectedPosition?: AccountPosition;
  simulationEnabled: boolean;
  simulationBlockedReason?: string;
  busy: boolean;
  draft?: TicketDraft;
  /** "平仓" from the positions bar: switch to the close page for that position with its available volume. */
  closeRequest?: { id: number; direction: "long" | "short" };
  /** Every position on the current contract; the quick row's smart close works from these. */
  currentPositions: AccountPosition[];
  onSubmit: (request: ManualSimulationRequest) => Promise<ManualOrderOutcome | undefined | void>;
  /** 目标持仓: the Runtime plans the difference and puts the first leg's card in the conversation. */
  onTargetPosition: (request: { symbol: string; targetNet: number; priceIntent?: "passive" | "aggressive" | "last" }) => Promise<boolean>;
  /** 手机版下单页 (2026-10-04): the same ticket laid out top to bottom — sides with their opponent
   *  price, price then its mode, volume, 止损/止盈 folded, the estimate and one large confirm button
   *  naming the order; no quick row. Logic, checks and the submit path are the desktop's. */
  variant?: "desktop" | "mobile";
}) {
  const mobile = variant === "mobile";
  const [tradeMode, setTradeMode] = useState<TradeMode>("open");
  useUiSurface("ticketMode", { state: tradeMode, apply: value => setTradeMode(value === "close" || value === "target" ? value : "open") }, [tradeMode]);
  // The phone page opens on 对手价 (the order fills at the price the side button shows).
  const [priceMode, setPriceMode] = useState<PriceMode>(mobile ? "opponent" : "limit");
  // The phone folds 止损 / 止盈 under one row (`disclosure: ticket-protection`); the desktop shows them.
  const [protectionOpen, setProtectionOpen] = useDisclosureSurface("ticket-protection", { available: mobile && tradeMode === "open" });
  const [side, setSide] = useState<"buy_open" | "sell_open">("buy_open");
  const [bucket, setBucket] = useState<CloseBucket>("auto");
  const quote = snapshot.quote;
  const tick = quote.priceTick ?? 1;
  const decimals = decimalsForTick(tick);
  // The limit price is editable: it starts from the chart pick or the last price and steps by one tick.
  // The Runtime re-validates tick alignment, freshness and limits on submit regardless.
  const [priceInput, setPriceInput] = useState<string>("");
  const [editedPrice, setEditedPrice] = useState<number | undefined>(undefined);
  useUiSurface("ticketPrice", { state: { price: editedPrice ?? null, priceMode }, apply: value => {
    if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return;
    setEditedPrice(value); setPriceInput(value.toFixed(decimals)); setPriceMode("limit");
  } }, [editedPrice, priceMode, decimals]);
  useEffect(() => {
    const seed = selectedPrice ?? quote.lastPrice ?? undefined;
    setEditedPrice(seed);
    if (selectedPrice !== undefined) setPriceMode("limit");
    setPriceInput(seed === undefined ? "" : seed.toFixed(decimals));
  }, [selectedPrice, quote.symbol]);
  const stepPrice = (direction: 1 | -1) => {
    const base = editedPrice ?? quote.lastPrice ?? 0;
    const next = Number((Math.round(base / tick) * tick + direction * tick).toFixed(decimals));
    if (next <= 0) return;
    setEditedPrice(next);
    setPriceInput(next.toFixed(decimals));
  };
  const commitPriceInput = (raw: string) => {
    setPriceInput(raw);
    const value = Number(raw);
    setEditedPrice(raw.trim() !== "" && Number.isFinite(value) && value > 0 ? value : undefined);
  };
  // 委托价 must lie between 跌停 and 涨停 when the quote has them (a Sina backup quote may not): above 0 otherwise.
  const limitsKnown = quote.lowerLimit != null && quote.upperLimit != null && quote.lowerLimit > 0 && quote.upperLimit > quote.lowerLimit;
  const priceRule: NumberRule = limitsKnown
    ? { label: "委托价", min: quote.lowerLimit!, max: quote.upperLimit!,
        rangeText: `跌停 ${formatNumber(quote.lowerLimit, decimals)} 到涨停 ${formatNumber(quote.upperLimit, decimals)} 之间` }
    : { label: "委托价", min: 0, minExclusive: true };
  const priceProblem = readNumber(priceInput, priceRule).problem;
  // Leaving the field snaps a typed price onto the tick grid; the Runtime rejects off-tick prices anyway.
  const snapPriceInput = () => {
    if (editedPrice === undefined) return;
    const snapped = Number((Math.round(editedPrice / tick) * tick).toFixed(decimals));
    if (snapped <= 0) return;
    setEditedPrice(snapped);
    setPriceInput(snapped.toFixed(decimals));
  };
  // 下单顺带保护 (2026-10-02): optional 止损 / 止盈 riding the open ticket. The reference is the price
  // this order will actually use (限价 → 编辑价, 对手价 → 对手一档, 最新价 → 最新价); the direction and
  // tick checks mirror the Runtime's card-time validation (its 中文拒绝 is the same wording), and the
  // money line turns each level into 元 = (委托价−价) × 合约乘数 × 手数 so a beginner sees 亏到哪 / 赚到哪
  // before confirming. Empty = 不设；成交后按实际成交量挂上，撤单不挂。
  const [stopInput, setStopInput] = useState("");
  const [stopPrice, setStopPrice] = useState<number | undefined>(undefined);
  // 打法本 (2026-10-25): the setup an open order is placed under (kept between orders; "" = none).
  const playbooks = usePlaybooks(runtimeUrl);
  const [playbookId, setPlaybookId] = useState("");
  const playbookChosen = playbooks.items.some(item => item.id === playbookId) ? playbookId : "";
  const [takeInput, setTakeInput] = useState("");
  const [takePrice, setTakePrice] = useState<number | undefined>(undefined);
  // 止损怎么动 (2026-10-25 第 4 批): 固定 / 追踪 / 浮盈 1R 后移到成本, sent with the stop and cleared with it.
  const [stopMotion, setStopMotion] = useState<StopMotion>({});
  const atrTimeframe = snapshot.durationSeconds === 604800 ? 86400 : snapshot.durationSeconds;
  useEffect(() => {
    setStopInput("");
    setStopPrice(undefined);
    setTakeInput("");
    setTakePrice(undefined);
  }, [quote.symbol]);
  const protectionReference = (priceMode === "limit" ? editedPrice
    : priceMode === "opponent" ? (side === "buy_open" ? quote.askPrice1 : quote.bidPrice1)
    : quote.lastPrice) ?? undefined;
  const commitProtectionInput = (kind: "stop" | "take", raw: string) => {
    const read = readNumber(raw, kind === "stop" ? STOP_RULE : TAKE_RULE);
    const parsed = read.problem === null && read.value !== null ? read.value : undefined;
    if (kind === "stop") { setStopInput(raw); setStopPrice(parsed); }
    else { setTakeInput(raw); setTakePrice(parsed); }
  };
  const stepProtection = (kind: "stop" | "take", direction: 1 | -1) => {
    const current = kind === "stop" ? stopPrice : takePrice;
    const base = current ?? protectionReference ?? quote.lastPrice ?? 0;
    const next = Number((Math.round(base / tick) * tick + direction * tick).toFixed(decimals));
    if (next <= 0) return;
    if (kind === "stop") { setStopPrice(next); setStopInput(next.toFixed(decimals)); }
    else { setTakePrice(next); setTakeInput(next.toFixed(decimals)); }
  };
  const snapProtection = (kind: "stop" | "take") => {
    const edited = kind === "stop" ? stopPrice : takePrice;
    if (edited === undefined) return;
    const snapped = Number((Math.round(edited / tick) * tick).toFixed(decimals));
    if (snapped <= 0) return;
    if (kind === "stop") { setStopPrice(snapped); setStopInput(snapped.toFixed(decimals)); }
    else { setTakePrice(snapped); setTakeInput(snapped.toFixed(decimals)); }
  };
  const offTick = (value: number) => Math.abs(value / tick - Math.round(value / tick)) > 1e-6;
  // Valid only for the chosen side: 止损在委托价内侧认输、止盈在外侧走人（多空镜像）。
  const protectionIssue = tradeMode !== "open" ? undefined : (() => {
    // A typed level that is not a price (0, -1) is refused as typed, never read as 不设.
    const typed = readNumber(stopInput, STOP_RULE).problem ?? readNumber(takeInput, TAKE_RULE).problem;
    if (typed) return typed;
    const reference = protectionReference;
    if (stopPrice !== undefined) {
      if (offTick(stopPrice)) return `止损 ${stopPrice} 不在最小变动价位上`;
      if (reference !== undefined && (side === "buy_open" ? stopPrice >= reference : stopPrice <= reference))
        return side === "buy_open" ? `止损要低于委托价 ${formatNumber(reference, decimals)}` : `止损要高于委托价 ${formatNumber(reference, decimals)}`;
    }
    if (takePrice !== undefined) {
      if (offTick(takePrice)) return `止盈 ${takePrice} 不在最小变动价位上`;
      if (reference !== undefined && (side === "buy_open" ? takePrice <= reference : takePrice >= reference))
        return side === "buy_open" ? `止盈要高于委托价 ${formatNumber(reference, decimals)}` : `止盈要低于委托价 ${formatNumber(reference, decimals)}`;
    }
    return undefined;
  })();
  const stopAmount = stopPrice !== undefined && protectionReference !== undefined
    ? Math.abs(protectionReference - stopPrice) * (quote.volumeMultiple ?? Number.NaN) * volume : null;
  const takeAmount = takePrice !== undefined && protectionReference !== undefined
    ? Math.abs(takePrice - protectionReference) * (quote.volumeMultiple ?? Number.NaN) * volume : null;
  const clearProtection = () => {
    setStopInput("");
    setStopPrice(undefined);
    setTakeInput("");
    setTakePrice(undefined);
    setStopMotion({});
  };
  const exchange = quote.symbol.split(".")[0]?.toUpperCase();
  const separateClose = exchange === "SHFE" || exchange === "INE";
  const availableToday = Math.max(0, (selectedPosition?.todayVolume ?? 0) - (selectedPosition?.frozenToday ?? 0));
  const availableHistorical = Math.max(0, (selectedPosition?.historicalVolume ?? 0) - (selectedPosition?.frozenHistorical ?? 0));
  // Another position starts on 自动 again: a 平今 / 平昨 is only ever what the user picked for this one.
  useEffect(() => {
    setBucket("auto");
  }, [selectedPosition?.symbol, selectedPosition?.direction]);
  const pickedBucket = separateClose && bucket !== "auto" ? bucket : undefined;

  useEffect(() => {
    if (!draft) return;
    setTradeMode("open");
    setSide(draft.side);
    setVolume(draft.volume);
    setPriceMode("limit");
  }, [draft]);

  useEffect(() => {
    if (!closeRequest) return;
    setTradeMode("close");
  }, [closeRequest]);
  // The requested position arrives with the next account snapshot; size the ticket to what it can close.
  const closeRequestKey = closeRequest ? `${closeRequest.id}:${selectedPosition?.symbol}:${selectedPosition?.direction}` : "";
  useEffect(() => {
    if (!closeRequest || selectedPosition?.direction !== closeRequest.direction) return;
    const total = closable(selectedPosition);
    if (total > 0) setVolume(total);
  }, [closeRequestKey]);

  const priceIntent = priceMode === "opponent" ? "aggressive" : priceMode === "last" ? "last" : "passive";
  const limitPrice = priceMode === "limit" ? editedPrice : undefined;
  // 目标持仓: the signed wanted net (0 = 全平, negative = 净空), previewed from live positions; the
  // Runtime re-plans from its own fresh read when the card is issued.
  const [targetNet, setTargetNet] = useState(1);
  const targetBox = useNumberText(targetNet, TARGET_RULE, value => { if (value !== null) setTargetNet(value); });
  const [targetSeeded, setTargetSeeded] = useState(false);
  const targetPreview = targetPlanPreview(currentPositions, quote.symbol, targetNet);
  useEffect(() => {
    if (tradeMode !== "target") {
      setTargetSeeded(false);
      return;
    }
    // Entering the mode seeds the stepper at the current net (or 1 手 when flat): the honest
    // starting point — an unchanged target says so instead of issuing a card.
    if (targetSeeded) return;
    setTargetSeeded(true);
    setTargetNet(targetPreview.currentNet !== 0 ? targetPreview.currentNet : 1);
  }, [tradeMode, targetSeeded, targetPreview.currentNet]);
  const submitTarget = async () => {
    if (targetBox.problem) return;
    await onTargetPosition({ symbol: quote.symbol, targetNet, priceIntent });
  };
  // 手数 (#789): the box keeps what is typed; only a whole number in 1…cap becomes the ticket's volume,
  // and anything else blocks 确认下单 and the 一键 buttons with its reason instead of becoming another number.
  const openLimit = quote.openLimit ?? 0;
  const openVolumeCap = openLimit > 0 ? Math.min(MAX_TICKET_VOLUME, openLimit) : MAX_TICKET_VOLUME;
  const volumeCap = tradeMode === "open" ? openVolumeCap : MAX_TICKET_VOLUME;
  const volumeRule: NumberRule = { label: "手数", min: 1, max: volumeCap, integer: true, unit: "手" };
  const volumeBox = useNumberText(volume, volumeRule, value => { if (value !== null) setVolume(value); });
  // 只接了参考行情的外盘品种（伦铜、美棉、布伦特…）没有可下单的月份合约：整张票不能下，写明原因（外盘代码统一 2026-10-21）。
  const quoteOnlyProblem = foreignQuoteOnly(quote.symbol) ? `${quote.name || "这个外盘品种"}只接了参考行情，没有可下单的月份合约` : null;
  // The first reason the open/close ticket cannot go: 仅行情, 手数, then (限价) 委托价.
  const ticketProblem = quoteOnlyProblem ?? (tradeMode === "target" ? null
    : volumeBox.problem ?? (priceMode === "limit" ? priceProblem : null));
  const submit = async (
    side: ManualSimulationRequest["side"],
    source: ManualSimulationRequest["source"] = "three-key"
  ) => {
    const close = side === "buy_close" || side === "sell_close";
    const accepted = await onSubmit({
      side,
      volume,
      priceIntent,
      limitPrice,
      positionBucket: close ? pickedBucket : undefined,
      symbol: close ? selectedPosition?.symbol : undefined,
      protection: close ? undefined : stopPrice !== undefined || takePrice !== undefined
        ? { stopLossPrice: stopPrice, takeProfitPrice: takePrice, ...(stopPrice !== undefined ? stopMotion : {}) }
        : undefined,
      playbookId: close || !playbookChosen ? undefined : playbookChosen,
      source
    });
    // 成交后保护按实际成交量挂上；提交成功即清空，下一次开仓重新决定亏到哪 / 赚到哪。
    if (!close && accepted?.accepted) clearProtection();
  };
  const closeAvailable = pickedBucket === "today" ? availableToday : pickedBucket === "historical" ? availableHistorical
    : selectedPosition ? closable(selectedPosition) : 0;
  const priceMissing = tradeMode !== "target" && priceMode === "limit" && editedPrice === undefined;
  const disabled = busy || !simulationEnabled || priceMissing || ticketProblem !== null || protectionIssue !== undefined;
  const smartCloseSide = selectedPosition?.direction === "long" ? "sell_close" : "buy_close";
  // 下单前的“钱”估算（只读，展示用，不拦截下单）：开仓给保证金约/手续费约/可开手数，平仓给释放
  // 保证金约。价格、手数、方向或模式变化后 300ms 重取；缺数据的字段是 null 就显示 —。
  const [estimate, setEstimate] = useState<OrderEstimate | undefined>();
  const estimateSide = tradeMode === "close" ? (smartCloseSide === "sell_close" ? "sell" : "buy") : side === "buy_open" ? "buy" : "sell";
  // 平今/平昨分开报的交易所按所选桶估算释放保证金；自动不带桶，Runtime 按同一份偏好分配估算，和提交时一致。
  const estimateBucket = tradeMode === "close" && selectedPosition ? pickedBucket : undefined;
  // A failed read says so (#745: it used to leave the old figures or a bare —).
  const [estimateFailed, setEstimateFailed] = useState(false);
  useEffect(() => {
    // An invalid 手数 / 委托价 is not estimated: the line shows why instead (below).
    if (!runtimeUrl || tradeMode === "target" || ticketProblem !== null) {
      setEstimate(undefined);
      setEstimateFailed(false);
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      getOrderEstimate(runtimeUrl, {
        symbol: quote.symbol, side: estimateSide,
        offset: tradeMode === "close" ? "close" : "open",
        volume, price: priceMode === "limit" ? editedPrice : undefined,
        ...(estimateBucket ? { positionBucket: estimateBucket } : {}),
      }, controller.signal).then(result => { setEstimate(result); setEstimateFailed(false); }).catch(() => {
        if (controller.signal.aborted) return;
        setEstimate(undefined);
        setEstimateFailed(true);
      });
    }, 300);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [runtimeUrl, tradeMode, estimateSide, estimateBucket, volume, editedPrice, priceMode, quote.symbol, ticketProblem]);
  const estimateText = ticketProblem ?? (estimateFailed ? "估算暂时读不到" : undefined) ?? (estimate === undefined ? "—"
    : tradeMode === "close"
      ? `释放保证金约 ${formatNumber(estimate.releasedMargin, 0)}`
      : `保证金约 ${formatNumber(estimate.margin, 0)} · 手续费约 ${formatFee(estimate.fee)}`
        + (estimate.maxOpenVolume === null ? "" : ` · 可开 ${estimate.maxOpenVolume} 手`));

  const modeSwitch = (
    <header>
      <div className="fdw-trade-mode" data-mode={tradeMode} role="group" aria-label="交易操作"><button type="button" aria-pressed={tradeMode==="open"} onClick={()=>setTradeMode("open")}>开仓</button><button type="button" aria-pressed={tradeMode==="close"} onClick={()=>setTradeMode("close")}>平仓</button><button type="button" aria-pressed={tradeMode==="target"} onClick={()=>setTradeMode("target")}>目标持仓</button></div>
    </header>
  );
  const sideChoice = tradeMode==="open" && <div className="fdw-side-choice" role="group" aria-label="选择开仓方向">
    <button type="button" className={`buy ${side === "buy_open" ? "active" : ""}`} onClick={() => setSide("buy_open")}><strong>买多</strong>{mobile ? <small>卖一 {formatNumber(quote.askPrice1, decimals)}</small> : null}</button>
    <button type="button" className={`sell ${side === "sell_open" ? "active" : ""}`} onClick={() => setSide("sell_open")}><strong>卖空</strong>{mobile ? <small>买一 {formatNumber(quote.bidPrice1, decimals)}</small> : null}</button>
  </div>;
  const targetFields = tradeMode==="target" && <div className="fdw-ticket-fields">
    <label>
      <span>目标净持仓</span>
      <div><button type="button" aria-label="目标净持仓减一" onClick={() => targetBox.replace(Math.max(-999, targetNet - 1))}><Minus /></button><input aria-label="目标净持仓手数" data-testid="target-net-input" type="number" step="1" value={targetBox.text} aria-invalid={targetBox.problem ? true : undefined} onChange={targetBox.onChange} onBlur={targetBox.onBlur} /><button type="button" aria-label="目标净持仓加一" onClick={() => targetBox.replace(Math.min(999, targetNet + 1))}><Plus /></button></div>
    </label>
    <small className="fdw-target-plan" data-testid="target-plan-summary" data-tone={targetBox.problem ? "warning" : undefined}>{targetBox.problem ?? targetPreview.summary}</small>
  </div>;
  // 对手价 / 最新价 stand in for the typed price; the phone names the price they come to now.
  const marketPriceLabel = `${priceMode === "opponent" ? "对手价" : "最新价"}${mobile ? ` ${formatNumber(protectionReference, decimals)}` : ""}`;
  const priceField = (modes?: ReactNode) => <label>
    <span className="fdw-price-heading">价格{modes}</span>
    {priceMode === "limit"
      ? <div><button type="button" aria-label="委托价减一跳" onClick={() => stepPrice(-1)}><Minus /></button><input aria-label="模拟委托价" type="number" inputMode="decimal" min="0" step={tick} value={priceInput} aria-invalid={priceProblem ? true : undefined} onChange={(event) => commitPriceInput(event.target.value)} onBlur={snapPriceInput} /><button type="button" aria-label="委托价加一跳" onClick={() => stepPrice(1)}><Plus /></button></div>
      : <strong>{marketPriceLabel}</strong>}
  </label>;
  const volumeField = <label>
    <span>手数</span>
    <div><button type="button" aria-label="手数减一" onClick={() => volumeBox.replace(Math.min(volumeCap, Math.max(1, volume - 1)))}><Minus /></button><input aria-label="模拟委托手数" type="number" min="1" max={volumeCap} step="1" value={volumeBox.text} aria-invalid={volumeBox.problem ? true : undefined} onChange={volumeBox.onChange} onBlur={volumeBox.onBlur} /><button type="button" aria-label="手数加一" onClick={() => volumeBox.replace(Math.min(volumeCap, volume + 1))}><Plus /></button></div>
  </label>;
  const protectionFields = tradeMode==="open" && <div className="fdw-ticket-fields" data-testid="ticket-protection">
    <label>
      <span>止损</span>
      <div><button type="button" aria-label="止损价减一跳" onClick={() => stepProtection("stop", -1)}><Minus /></button><input aria-label="止损价" data-testid="stop-loss-input" type="number" inputMode="decimal" min="0" step={tick} placeholder="不设" value={stopInput} onChange={(event) => commitProtectionInput("stop", event.target.value)} onBlur={() => snapProtection("stop")} /><button type="button" aria-label="止损价加一跳" onClick={() => stepProtection("stop", 1)}><Plus /></button></div>
    </label>
    <label>
      <span>止盈</span>
      <div><button type="button" aria-label="止盈价减一跳" onClick={() => stepProtection("take", -1)}><Minus /></button><input aria-label="止盈价" data-testid="take-profit-input" type="number" inputMode="decimal" min="0" step={tick} placeholder="不设" value={takeInput} onChange={(event) => commitProtectionInput("take", event.target.value)} onBlur={() => snapProtection("take")} /><button type="button" aria-label="止盈价加一跳" onClick={() => stepProtection("take", 1)}><Plus /></button></div>
    </label>
    {stopPrice !== undefined ? <StopMotionFields value={stopMotion} onChange={setStopMotion} atrTimeframe={atrTimeframe} /> : null}
    {playbooks.items.length ? <label className="fdw-ticket-playbook">
      <span>打法</span>
      <select aria-label="打法" data-testid="ticket-playbook" value={playbookChosen} onChange={event => setPlaybookId(event.target.value)}>
        <option value="">不选</option>
        {playbooks.items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>
    </label> : null}
  </div>;
  const protectionEstimate = tradeMode==="open" && (stopPrice !== undefined || takePrice !== undefined || protectionIssue !== undefined) ? (
    <p className="fdw-ticket-estimate" data-testid="ticket-protection-estimate" data-tone={protectionIssue ? "warning" : undefined}>
      {protectionIssue ?? [
        stopPrice !== undefined ? `止损约亏 ${formatNumber(stopAmount, 0)} ${moneyUnit(quote.symbol)}` : null,
        takePrice !== undefined ? `止盈约赚 ${formatNumber(takeAmount, 0)} ${moneyUnit(quote.symbol)}` : null
      ].filter(Boolean).join(" · ")}
    </p>
  ) : null;
  const estimateLine = tradeMode!=="target" ? (
    <p className="fdw-ticket-estimate" data-testid="ticket-estimate" data-tone={ticketProblem || estimate?.insufficient ? "warning" : undefined}
      role={ticketProblem ? "alert" : undefined} title={ticketProblem ? undefined : estimate?.basis}>
      {estimateText}
      {tradeMode==="open" && !ticketProblem && estimate?.insufficient ? <strong>可用资金不够开 {volume} 手</strong> : null}
    </p>
  ) : null;
  const deliveryLine = tradeMode==="open" && deliveryNotice ? (
    <p className="fdw-ticket-delivery" data-level={deliveryNotice.level} data-testid="ticket-delivery-hint">
      {deliveryTicketHint(deliveryNotice)}
    </p>
  ) : null;
  const priceModes = tradeMode==="target" ? (
    <div className="fdw-price-modes" role="group" aria-label="委托价格方式">
      <button type="button" aria-label="挂单" className={priceMode !== "opponent" && priceMode !== "last" ? "active" : ""} onClick={() => setPriceMode("limit")}>挂单</button>
      <button type="button" aria-label="对手价" className={priceMode === "opponent" ? "active" : ""} onClick={() => setPriceMode("opponent")}>对手价</button>
      <button type="button" aria-label="最新价" className={priceMode === "last" ? "active" : ""} onClick={() => setPriceMode("last")}>最新价</button>
    </div>
  ) : mobile ? (
    <div className="fdw-price-modes" role="group" aria-label="委托价格方式">
      <button type="button" aria-label="对手价" className={priceMode === "opponent" ? "active" : ""} onClick={() => setPriceMode("opponent")}>对手价</button>
      <button type="button" aria-label="最新价" className={priceMode === "last" ? "active" : ""} onClick={() => setPriceMode("last")}>最新价</button>
      <button type="button" aria-label="限价" className={priceMode === "limit" ? "active" : ""} onClick={() => setPriceMode("limit")}>限价</button>
    </div>
  ) : (
    <div className="fdw-price-modes" role="group" aria-label="委托价格方式">
      <button type="button" aria-label="限价" className={priceMode === "limit" ? "active" : ""} onClick={() => setPriceMode("limit")}>限价</button>
      <button type="button" aria-label="对手价" className={priceMode === "opponent" ? "active" : ""} onClick={() => setPriceMode("opponent")}>对手价</button>
      <button type="button" aria-label="最新价" className={priceMode === "last" ? "active" : ""} onClick={() => setPriceMode("last")}>最新价</button>
    </div>
  );
  const closePlan = tradeMode==="close" && selectedPosition && separateClose ? (
    <div className="fdw-close-plan">
      <span>{selectedPosition.direction==="long"?"多仓":"空仓"} · 可平 {closeAvailable}手<HelpTip helpKey="ticket:close-bucket" label="平今平昨">自动：按设置 → 交易 → 高级交易设置里的“平今平昨”分配今仓和昨仓，跨今昨时一次确认、分两笔发出；点平今或平昨就只平那一侧。</HelpTip></span>
      <div role="group" aria-label="平今平昨">
        <button type="button" className={bucket === "auto" ? "active" : ""} aria-pressed={bucket === "auto"} onClick={() => setBucket("auto")}>自动</button>
        <button type="button" className={bucket === "today" ? "active" : ""} aria-pressed={bucket === "today"} disabled={!availableToday} onClick={() => setBucket("today")}>平今 {availableToday}</button>
        <button type="button" className={bucket === "historical" ? "active" : ""} aria-pressed={bucket === "historical"} disabled={!availableHistorical} onClick={() => setBucket("historical")}>平昨 {availableHistorical}</button>
      </div>
    </div>
  ) : null;
  const draftNote = draft && tradeMode==="open" ? (
    <div className="fdw-ticket-draft" data-testid="ticket-draft">
      <span>图表草稿</span>
      <strong>{draft.side === "buy_open" ? "买多" : "卖空"} {draft.volume} 手</strong>
      {draft.stopPrice !== undefined ? <small>止损候选 {formatNumber(draft.stopPrice, decimals)} · <button type="button" data-testid="fill-stop-from-draft" onClick={() => { const stop = draft.stopPrice; if (stop === undefined) return; setStopPrice(stop); setStopInput(stop.toFixed(decimals)); }}>点击填入</button></small> : null}
    </div>
  ) : null;
  // The phone's confirm names the order it sends: 确认买多 1 手 @ 3,113 (the price the mode resolves to now).
  const orderPrice = priceMode === "limit" ? editedPrice : protectionReference;
  const confirmLabel = busy ? "提交中…" : mobile ? ticketProblem ? `确认${side === "buy_open" ? "买多" : "卖空"}` : `确认${side === "buy_open" ? "买多" : "卖空"} ${volume} 手 @ ${formatNumber(orderPrice, decimals)}` : `${side === "buy_open" ? "买多开仓" : "卖空开仓"} ${volume} 手 · ${priceMode === "limit" ? formatNumber(editedPrice, decimals) : priceMode === "opponent" ? "对手价" : "最新价"}`;
  const confirmButtons = <>
    {tradeMode==="open" && <button type="button" className="fdw-confirm-order" data-side={mobile ? side : undefined} disabled={disabled} onClick={() => void submit(side)}>{confirmLabel}</button>}
    {tradeMode==="target" && <button type="button" className="fdw-confirm-order" data-testid="generate-target-card" disabled={busy || !simulationEnabled || quoteOnlyProblem !== null || targetBox.problem !== null || targetPreview.unchanged} onClick={() => void submitTarget()}>{busy ? "生成中…" : !targetBox.problem && targetPreview.unchanged ? "已是目标持仓" : "生成方案卡"}</button>}
    {tradeMode==="close" && <div className="fdw-close-action">
      <button
        type="button"
        className="close"
        disabled={disabled || !selectedPosition || closeAvailable < volume}
        onClick={() => void submit(smartCloseSide)}
      >
        <strong>{mobile ? "智能平仓" : `${selectedPosition?.direction === "long" ? "卖出平仓" : "买入平仓"} ${volume} 手 · ${priceMode === "limit" ? formatNumber(editedPrice, decimals) : priceMode === "opponent" ? "对手价" : "最新价"}`}</strong>
        <span>{selectedPosition ? `${selectedPosition.direction === "long" ? "卖平多仓" : "买平空仓"} · 可平 ${closeAvailable}` : "先选择持仓"}</span>
      </button>
    </div>}
  </>;
  const blockedReason = !simulationEnabled
    ? simulationBlockedReason ?? (snapshot.marketSessionStatus === "closed" ? "休市，暂不可下单" : "行情或模拟网关未就绪，暂不可下单")
    : mobile ? "" : busy ? "正在提交，请等待委托回报" : quoteOnlyProblem
      ?? (tradeMode === "target" ? targetBox.problem : ticketProblem ?? protectionIssue
        ?? (priceMissing ? "请输入委托价" : tradeMode === "close" && !selectedPosition ? "先选择持仓"
          : tradeMode === "close" && closeAvailable < volume ? `当前可平 ${closeAvailable} 手` : ""));
  const blockedLine = <small data-testid="ticket-blocked-reason">{blockedReason}</small>;

  if (mobile) {
    const protectionSummary = stopPrice === undefined && takePrice === undefined ? "不设"
      : [stopPrice !== undefined ? `止损 ${formatNumber(stopPrice, decimals)}` : "", takePrice !== undefined ? `止盈 ${formatNumber(takePrice, decimals)}` : ""].filter(Boolean).join(" · ");
    return (
      <section className="fdw-ticket fdm-ticket" aria-label="三键模拟交易台" data-mode={tradeMode}>
        {modeSwitch}
        {sideChoice}
        {targetFields}
        {tradeMode==="target" ? priceModes : null}
        {tradeMode!=="target" ? <div className="fdw-ticket-fields fdm-ticket-price">{priceField()}{priceModes}</div> : null}
        {tradeMode!=="target" ? <div className="fdw-ticket-fields">{volumeField}</div> : null}
        {closePlan}
        {tradeMode==="open" ? (
          <details className="fdm-ticket-protection" open={protectionOpen} onToggle={event => setProtectionOpen(event.currentTarget.open)}>
            <summary><span>止损 / 止盈</span><strong>{protectionSummary}</strong></summary>
            {protectionFields}
          </details>
        ) : null}
        {protectionEstimate}
        {estimateLine}
        {deliveryLine}
        {draftNote}
        <div className="fdm-ticket-confirm">{confirmButtons}{blockedLine}</div>
      </section>
    );
  }
  return (
    <section className="fdw-ticket" aria-label="三键模拟交易台">
      {modeSwitch}
      {sideChoice}
      {targetFields}
      {tradeMode!=="target" && <div className="fdw-ticket-fields fdw-ticket-entry-fields">
        {priceField(priceModes)}
        {volumeField}
      </div>}
      {protectionFields}
      {protectionEstimate}
      {estimateLine}
      {deliveryLine}
      {tradeMode === "target" ? priceModes : null}
      {closePlan}
      {draftNote}
      {confirmButtons}
      {blockedLine}
    </section>
  );
}
