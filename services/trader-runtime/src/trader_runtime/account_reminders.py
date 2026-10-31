"""收盘前与风险度主动提醒 (2026-10-03)：判定只有 check_positions / check_account 一份，入口有两处——
Runtime 自己的常驻循环（AccountReminderLoop，2026-10-06 起：每 30 秒读每个已装配账户的同一份
账户快照，按该账户的 owner 逐个判定，客户端没开着也照常提醒），以及客户端的持仓/账户读取
（HTTP 与 feed 推送两条路，谁先到谁发）。各按自己的开关只提醒、只发一次（#932 起经统一提醒入口
reminders.deliver：提醒记录的认领与消息同一事务，重启、蓝绿双槽都不重发；同一循环还判定保本与交割）。只对当前已装配的账户生效：
按需装配的 tqkq-pool 账户在所有人离线 30 分钟后被回收，回收后不再提醒。收盘提醒按每个持仓合约自己的 trading_time 算——时段从共享
行情源的合约目录条目（tradingSchedule，带共享缓存）读，不为它占一条 K 线订阅（全站 serial
上限 48、TqSdk 无退订）——只在该合约日盘/夜盘最后一段结束前 5 分钟内才提醒（午休、10:15
小节休息不是收盘）；风险度提醒在账户直报的风险度到 80% 时提醒一次。投递：用户正在看的对话里
一条消息（2026-10-08 落点规则：在线且绑定该账户的最近会话 → 绑定该账户的最近会话 → 用户最近
会话，conversation_cards.reminder_target_session 一份选择；turnId reminder-<kind>-<账户>-<交易日>，
#457 账户维度，两个账户各自一条）、提醒中心一条记录、该用户订阅窗口一条 feed.reminder（客户端重读
提醒中心并弹桌面通知）、语音通话在线时口播，以及（主进程、账户 owner）锁屏推送（页面在前台的设备不推）。依据的数据必须
是同账户同一次读取的新鲜快照（#458）：过期的不提醒也不落库，窗口内恢复新鲜再补发那一次。
绝不平仓、不拒单、不加任何限制，也绝不影响它搭的那次读取。
回放子 Runtime 里同样可用 (2026-10-03)：“现在”用回放时钟（时段按它落窗，投递时间戳也是它），
时段拿不到目录条目的就退回本次回放行情自己的时段；开关读主进程的偏好（回放窗口里改设置
本来就写到主进程）；提醒与落库都只在回放子进程里，随回放一起消失。快照暂不可读（读不到
回放时钟）时这一轮不提醒也不落库。"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
import weakref
from datetime import UTC, datetime
from typing import Any

from .conversation_cards import ask_action, reminder_card, ui_action
from .instruments import cme_hours_of, foreign_trade_name, is_foreign_paper
from .reminder_kinds import reminder_enabled
from .trading_calendar import NS, cme_rule, cme_schedule, cme_trading_day, sessions_of_day, trading_day_of

log = logging.getLogger("uvicorn.error.reminders")

CLOSE_WINDOW_SECONDS = 300   # 收盘前 5 分钟内提醒
RISK_RATIO_THRESHOLD = 80.0  # 百分数，与持仓面板六格同口径（80 黄、100 红）
DIRECTION_WORDS = {"long": "多", "short": "空", "buy": "多", "sell": "空"}

# 回放子进程借父进程实时目录读时段的缓存（按父应用弱引用）。回放自己的目录和行情都不带
# 交易时段（sidecar 的回放导出没有），而收盘判定跟着回放时钟每推进一步跑一次——没有这层
# 缓存，10 步/秒下一秒会打十次目录。整表缓存 60 秒：时段不变，目录却会换月。
_PARENT_SCHEDULES: weakref.WeakKeyDictionary[Any, tuple[dict[str, Any], float]] = weakref.WeakKeyDictionary()
_PARENT_SCHEDULE_RETRY_SECONDS = 60.0


def _now_ns() -> int:
    return time.time_ns()


def _now(app: Any) -> int | None:
    """统一的“现在”：主进程用墙上时钟（_now_ns，测试里打桩的就是它）；回放子进程用回放
    时钟——快照暂不可读时返回 None，这一轮不提醒也不落库。"""
    replay = getattr(app.state, "replay_context", None)
    if replay is None:
        return _now_ns()
    try:
        return int(replay.provider.clock().timestamp() * NS)
    except Exception as error:  # noqa: BLE001 - 回放时钟读不到就静默跳过这一轮
        log.debug("replay clock unavailable for reminders: %s", error)
        return None


def _now_iso(app: Any) -> str:
    """投递时间戳：主进程墙上时钟；回放子进程回放时钟（读不到退回墙上时钟）。"""
    replay = getattr(app.state, "replay_context", None)
    if replay is not None:
        try:
            return replay.provider.clock().isoformat()
        except Exception:
            pass
    return datetime.now(UTC).isoformat()


def _period_seconds(text: Any) -> int:
    """时刻写成秒数。接受 "HH:MM"（合约信息卡的口径）与 "HH:MM:SS"（TqSdk 原样）。"""
    parts = str(text).split(":")
    if len(parts) not in (2, 3) or not all(part.isdigit() for part in parts):
        raise ValueError(text)
    hours, minutes = int(parts[0]), int(parts[1])
    seconds = int(parts[2]) if len(parts) == 3 else 0
    if minutes > 59 or seconds > 59:
        raise ValueError(text)
    return hours * 3600 + minutes * 60 + seconds


def _hhmmss(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def normalize_schedule(schedule: Any) -> dict[str, Any] | None:
    """把合约的 trading_time 统一成 trading_calendar 吃的形状：夜盘跨零点的结束时间有两种
    写法（TqSdk 的 "26:30:00" 与信息卡的 "02:30"），都改成 ≥24 小时那种；不可解析返回 None。"""
    if not isinstance(schedule, dict):
        return None
    result: dict[str, Any] = {}
    for part in ("day", "night"):
        periods = schedule.get(part)
        if not isinstance(periods, (list, tuple)):
            return None
        clean: list[list[str]] = []
        for period in periods:
            if not (isinstance(period, (list, tuple)) and len(period) == 2):
                return None
            try:
                start, end = _period_seconds(period[0]), _period_seconds(period[1])
            except (TypeError, ValueError):
                return None
            if part == "night" and end <= start:
                end += 24 * 3600  # "21:00–02:30" 的 02:30 在次日凌晨
            clean.append([_hhmmss(start), _hhmmss(end)])
        result[part] = clean
    # 外盘（CME）：交易日按美东 18:00 换日，时段按品种（cmeHours）随夏令时重算
    result.update(cme_rule(schedule))
    return result if (result["day"] or result["night"]) else None


def closing_window(schedule: dict[str, Any] | None, now_ns: int) -> tuple[str, str, int] | None:
    """(day|night, 交易日, 剩余分钟)：此刻落在该合约日盘（或夜盘）最后一段结束前 5 分钟内，
    返回该半场与它所属的交易日；否则 None。只看最后一段的结束——午休、10:15 小节休息
    不算收盘；两个半场互不相干，各自只看各自的最后一段。外盘（CME 时段，dayRule cme）没有夜盘，按 CME
    交易日看这个品种当天最后一段的收盘（23 小时品种美东 17:00，CBOT 谷物芝加哥 13:20，瘦肉猪 13:05）。"""
    day = cme_trading_day(now_ns) if (schedule or {}).get("dayRule") == "cme" else trading_day_of(now_ns)
    sessions = sessions_of_day(day, schedule)
    for night in (True, False):
        parts = [item for item in sessions if item.night is night]
        if not parts:
            continue
        end = parts[-1].end_ns
        if end - CLOSE_WINDOW_SECONDS * NS < now_ns <= end:
            return ("night" if night else "day", day, max(1, math.ceil((end - now_ns) / NS / 60)))
    return None


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _money(value: Any) -> str:
    amount = _number(value)
    if amount is None:
        return "0"
    return f"{amount:+,.0f}" if amount == int(amount) else f"{amount:+,.2f}"


def _wan(value: Any, *, usd: bool = False) -> str:
    """金额按万读：415000 → “41.5 万”，500000 → “50 万”；美元账写“万美元”（两本账第三批）；不可读给 “—”。"""
    amount = _number(value)
    if amount is None:
        return "—"
    return f"{f'{amount / 10_000:.1f}'.rstrip('0').rstrip('.')} {'万美元' if usd else '万'}"


def _position_line(item: dict[str, Any], names: dict[str, str]) -> str:
    symbol = str(item.get("symbol") or "")
    direction = DIRECTION_WORDS.get(str(item.get("direction")), str(item.get("direction")))
    pnl = _number(item.get("unrealizedPnl")) or 0
    word = "浮盈" if pnl >= 0 else "浮亏"
    return (f"{names.get(symbol.upper(), symbol)} {direction} {int(item.get('volume') or 0)} 手"
            f"（{word} {_money(pnl)}）")


def close_message(*, session_label: str, minutes: int, positions: list[dict[str, Any]],
                  names: dict[str, str], account_label: str = "") -> str:
    lines = [_position_line(item, names) for item in positions]
    return f"{account_label}收盘提醒：{minutes} 分钟后{session_label}收盘，你还有 {'、'.join(lines)}未平。"


def risk_message(summary: dict[str, Any], account_label: str = "", *, usd: bool = False) -> str | None:
    """风险度（保证金占用 ÷ 权益，账户直报的百分数）到 80% 时的提醒文案；不到或数据
    不可信（非 normal/simulated 的快照、标了 isCached 的缓存，#458）返回 None。只提醒，
    不平仓、不拒单。"""
    if not _fresh(summary):
        return None
    ratio = _number(summary.get("riskRatio"))
    if ratio is None or ratio < RISK_RATIO_THRESHOLD:
        return None
    return (f"{account_label}风险度提醒：现在风险度 {ratio:.0f}%（保证金占用 {_wan(summary.get('margin'), usd=usd)}"
            f" / 权益 {_wan(summary.get('equity'), usd=usd)}），越高越接近被强平；"
            f"可用 {_wan(summary.get('available'), usd=usd)}。")


def _fresh(summary: Any) -> bool:
    """#458：同账户同一次读取的账户快照是否新鲜可依据——None / 非 normal/simulated /
    标了 isCached 的缓存都算过期：过期数据既不提醒也不落库，窗口内恢复新鲜再发那一次。"""
    return (isinstance(summary, dict)
            and str(summary.get("dataStatus") or "") in ("normal", "simulated")
            and not bool(summary.get("isCached")))


def account_name(store: Any, user_id: str, account_id: str) -> str:
    """#457：这条提醒属于哪个账户——用户名下不止一个账户时是账户名（目录里的 display_name，取不到退回
    账户 ID），单账户为空，不添噪音。风险度、收盘、成交、撤单提醒共用。"""
    if store is None or not account_id:
        return ""
    try:
        if len(store.list_account_grants(user_id=user_id)) < 2:
            return ""
        record = store.get_trading_account(account_id) or {}
    except Exception:
        return ""
    return str(record.get("display_name") or account_id)


def _account_label(app: Any, user_id: str, account_id: str) -> str:
    name = account_name(getattr(app.state, "store", None), user_id, account_id)
    return f"{name}：" if name else ""


def _preferences(app: Any, user_id: str) -> dict[str, Any]:
    """用户的账号偏好文档（GET/PUT /api/preferences 同一份；app.state.preference_store 是
    复盘呈现偏好，别拿错）。回放子进程读主进程的那份——回放窗口里改的设置本来就转发到
    父应用去写，子进程库里只有默认值。"""
    store = app.state.store
    replay = getattr(app.state, "replay_context", None)
    parent = getattr(replay, "parent", None) if replay is not None else None
    if parent is not None:
        store = parent.state.store
    return store.get_preferences(user_id)["preferences"] or {}


def _product_key(symbol: str) -> str:
    """SHFE.RB2610 → SHFE.RB；不是“品种字母 + 月份数字”的代码（主连、期权）不归品种。"""
    exchange, _, instrument = symbol.upper().partition(".")
    match = re.fullmatch(r"([A-Z]+)\d+", instrument)
    return f"{exchange}.{match.group(1)}" if exchange and match else ""


async def _parent_schedules(parent: Any) -> dict[str, Any]:
    """父进程实时目录里每个合约的时段（symbol → tradingSchedule），外加每个品种取一份
    （SHFE.RB → 任一在市月份的时段）。回放的导出里没有时段，图表行情里的那份也未必解析
    得出来——实时目录是收盘判定在回放里的最后一层来源；它只列在市合约，回放已到期的
    合约（#486）就按同交易所同品种借，同品种各月份交易时段相同。"""
    cached = _PARENT_SCHEDULES.get(parent)
    if cached is not None and cached[1] > time.monotonic():
        return cached[0]
    schedules: dict[str, Any] = {}
    try:
        provider = parent.state.accounts.default.provider
        for item in (await asyncio.to_thread(provider.instrument_catalog)).get("items") or []:
            symbol, schedule = str(item.get("symbol") or "").upper(), item.get("tradingSchedule")
            if symbol and schedule:
                schedules[symbol] = schedule
                if product := _product_key(symbol):
                    schedules.setdefault(product, schedule)
    except Exception as error:  # noqa: BLE001 - 拿不到就空表 + 短期重试，不阻塞提醒链
        log.debug("parent catalog unavailable for replay close reminder: %s", error)
    _PARENT_SCHEDULES[parent] = (schedules, time.monotonic() + _PARENT_SCHEDULE_RETRY_SECONDS)
    return schedules


async def _catalog_items(app: Any) -> list[dict[str, Any]]:
    """合约目录条目：主进程读共享行情源（自带共享缓存；同一条持仓读取上的交割提醒读同一
    份），时段与名称都从这里取——绝不走 shared.market/snapshot 占 K 线订阅。回放子进程读
    回放目录；条目没有时段（sidecar 的回放导出不带）就先退回本次回放行情自己的时段
    （ReplayProvider.market_snapshot 会把当日图表行情里的 tradingSchedule 带上），还没有
    就借父进程的实时目录——先按完整代码，已到期不在目录里就按同交易所同品种（#486）。"""
    replay = getattr(app.state, "replay_context", None)
    if replay is not None:
        try:
            items = list((await asyncio.to_thread(replay.provider.instrument_catalog)).get("items") or [])
        except Exception as error:  # noqa: BLE001 - 提醒拿不到目录就静默跳过
            log.debug("replay close reminder catalog unavailable: %s", error)
            return []
        parent = getattr(replay, "parent", None)
        fallback = await _parent_schedules(parent) if parent is not None else {}
        for item in items:
            if item.get("tradingSchedule"):
                continue
            try:
                market = await asyncio.to_thread(replay.provider.market_snapshot, "",
                                                 str(item.get("symbol") or ""))
            except Exception:
                market = {}
            symbol = str(item.get("symbol") or "").upper()
            schedule = market.get("tradingSchedule") or fallback.get(symbol) or fallback.get(_product_key(symbol))
            if schedule:
                item["tradingSchedule"] = schedule
        return items
    shared = getattr(app.state, "shared_market", None)
    if shared is None:
        return []
    try:
        return list((await shared.instruments()).get("items") or [])
    except Exception as error:  # noqa: BLE001 - 提醒拿不到目录就静默跳过
        log.debug("close reminder catalog unavailable: %s", error)
        return []


async def check_positions(app: Any, *, user_id: str, account_id: str,
                          positions: list[dict[str, Any]],
                          summary: dict[str, Any] | None = None) -> None:
    """持仓读取的顺风车（与交割提醒同一处；feed 推送路径同样调它）：开关开着、且此刻
    落在某些持仓合约自己的收盘前 5 分钟内 → 一条合并提醒。绝不抛出。#458：只有同一账户
    同一次读取的账户快照（`summary`）新鲜才提醒——过期（None / 缓存）的不发也不落库。
    回放子进程里“此刻”是回放时钟（读不到时钟这一轮跳过）。"""
    try:
        if not _fresh(summary):
            return
        held = [item for item in positions if int(_number(item.get("volume")) or 0) > 0]
        if not held:
            return
        if not reminder_enabled(_preferences(app, user_id), "market_close"):
            return
        now_ns = _now(app)
        if now_ns is None:
            return
        names: dict[str, str] = {}
        schedules: dict[str, dict[str, Any] | None] = {}
        # 只整理持仓合约的时段与名称（2026-10-13 省 CPU）：目录约 900 条，每个读者每帧逐条解析
        # 时段曾占 Runtime 数个百分点；下面只按持仓代码查这两张表，结果与整表整理相同。
        wanted = {str(position.get("symbol") or "").upper() for position in held}
        wanted |= {symbol.strip() for symbol in wanted}
        for item in await _catalog_items(app):
            symbol = str(item.get("symbol") or "").upper()
            if symbol and symbol in wanted:
                names[symbol] = str(item.get("instrumentName") or item.get("symbol") or "")
                schedules[symbol] = normalize_schedule(item.get("tradingSchedule"))
        # 美元子账户的外盘月份合约（CME.GCEZ26）不在快期目录里：名字由代码推出，时段是 CME 的（外盘代码统一，2026-10-21）。
        for symbol in wanted:
            if symbol not in schedules and (foreign := foreign_trade_name(symbol)):
                names[symbol] = foreign
                schedules[symbol] = normalize_schedule(cme_schedule(hours=cme_hours_of(symbol)))
        account_label = _account_label(app, user_id, account_id)
        closing: dict[tuple[str, str], list[Any]] = {}
        for position in held:
            symbol = str(position.get("symbol") or "").strip()
            schedule = schedules.get(symbol.upper())
            window = closing_window(schedule, now_ns)
            if window is None:
                continue
            kind, trade_day, minutes = window
            label = "外盘" if (schedule or {}).get("dayRule") == "cme" else "夜盘" if kind == "night" else "日盘"
            batch = closing.setdefault((kind, trade_day), [minutes, [], label])
            batch[1].append(position)
        for (kind, trade_day), (minutes, batch, label) in closing.items():
            text = close_message(session_label=label, minutes=minutes, positions=batch,
                                 names=names, account_label=account_label)
            lots = sum(int(_number(item.get("volume")) or 0) for item in batch)
            figures: list[tuple[Any, ...]] = [("距收盘", f"{minutes} 分钟", "warning"),
                                              ("未平持仓", f"{len(batch)} 个 · {lots} 手")]
            figures += [("持仓", _position_line(item, names)) for item in batch[:3]]
            card = {"kind": "market_close", "title": "收盘提醒",
                    "summary": f"{account_label}{minutes} 分钟后{label}收盘，还有 {len(batch)} 个持仓未平",
                    "figures": figures,
                    "actions": [ask_action("问 AI 怎么办", f"{minutes} 分钟后{label}收盘，我这些持仓要不要平？"),
                                ui_action("打开持仓", {"panel": "positions"})]}
            symbols = sorted({str(item.get("symbol") or "") for item in batch} - {""})
            await _deliver(app, user_id=user_id, account_id=account_id, kind=f"market_close_{kind}",
                           trade_day=trade_day, title="收盘提醒", text=text, card=card,
                           symbols=",".join(symbols), symbol=symbols[0] if len(symbols) == 1 else "")
    except Exception as error:  # noqa: BLE001 - 顺风车绝不弄坏它搭的读取
        log.debug("close reminder check failed: %s", error)


async def check_account(app: Any, *, user_id: str, account_id: str,
                        summary: dict[str, Any]) -> None:
    """账户读取的顺风车（六格数据同一条路：/api/account/summary 与 feed.account）：
    风险度 ≥ 80% 且开关开着 → 每个交易日一条。绝不抛出。#458：缓存的快照不提醒也不落库。
    回放子进程里“交易日”取回放账户快照的 tradingDay（没有才按回放时钟推）。"""
    try:
        usd = is_foreign_paper(account_id)
        text = risk_message(summary, account_label=_account_label(app, user_id, account_id), usd=usd)
        if text is None:
            return
        if not reminder_enabled(_preferences(app, user_id), "risk_ratio"):
            return
        trade_day = str(summary.get("tradingDay") or "")
        if not trade_day:
            now = _now(app)
            if now is None:
                return
            trade_day = trading_day_of(now)
        ratio = _number(summary.get("riskRatio")) or 0.0
        label = _account_label(app, user_id, account_id)
        card = {"kind": "risk_ratio", "title": "风险度提醒",
                "summary": f"{label}风险度 {ratio:.0f}%，越高越接近被强平",
                "figures": [("风险度", f"{ratio:.0f}%", "danger" if ratio >= 100 else "warning"),
                            ("保证金占用", _wan(summary.get("margin"), usd=usd)),
                            ("权益", _wan(summary.get("equity"), usd=usd)),
                            ("可用", _wan(summary.get("available"), usd=usd))],
                "actions": [ask_action("问 AI 怎么办", f"风险度到 {ratio:.0f}% 了，怎么降下来？"),
                            ui_action("打开持仓", {"panel": "positions"})]}
        await _deliver(app, user_id=user_id, account_id=account_id, kind="risk_ratio",
                       trade_day=trade_day, title="风险度提醒", text=text, card=card)
    except Exception as error:  # noqa: BLE001
        log.debug("risk reminder check failed: %s", error)


async def _deliver(app: Any, *, user_id: str, account_id: str, kind: str, trade_day: str, title: str,
                   text: str, card: dict[str, Any] | None = None, symbols: str = "", symbol: str = "") -> bool:
    """The one delivery entry (reminders.deliver, #932): the claim, the message and its reminder card commit
    in one transaction — together with the reminder_notices row that 今日复盘 tags by and a slot still
    running the previous build reads — then the feed.reminder event, the spoken
    line on a live call (a replay's on the main app's calls bound to this replay) and the lock-screen push.
    #457: every identifier carries the account — two accounts' reminders are two messages, two lines, two
    pushes; one account, one day, one kind still goes out once."""
    from .reminders import ReminderSpec, deliver
    from .web_push import reminder_message
    store = app.state.store
    reminder_id = f"reminder-{kind}-{account_id}-{trade_day}"
    at = _now_iso(app)
    reminder = (reminder_card(turn_id=reminder_id, kind=card["kind"], title=card["title"], summary=card["summary"],
                              at=at, figures=card.get("figures"), actions=card.get("actions"))
                if card is not None else None)
    close = kind.startswith("market_close")
    half = kind.removeprefix("market_close_")

    def legacy(connection: Any) -> bool:
        return bool(store.record_reminder_notice(user_id=user_id, account_id=account_id, trade_day=trade_day,
                                                 kind=kind, text=text, connection=connection, symbols=symbols))

    result = await deliver(app, ReminderSpec(
        kind="market_close" if close else "risk_ratio", user_id=user_id, account_id=account_id,
        dedupe_key=f"{account_id}:{trade_day}" + (f":{half}" if close else ""), trading_day=trade_day, at=at,
        title=title, body=(card or {}).get("summary") or text, symbol=symbol, message=text, turn_id=reminder_id,
        cards=[("card.reminder", reminder)] if reminder is not None else [], speech=text, voice_kind=reminder_id,
        push=reminder_message(kind=kind, text=text, card_id=f"reminder-{reminder_id}", account_id=account_id),
        legacy=legacy))
    return result.reminder is not None


REMINDER_LOOP_SECONDS = 30.0


async def check_breakeven(app: Any, **kwargs: Any) -> int:
    from .breakeven import check_breakeven as judge_breakeven
    return await judge_breakeven(app, **kwargs)


async def check_delivery(app: Any, **kwargs: Any) -> int:
    from .delivery_reminders import check_positions as judge_delivery
    return await judge_delivery(app, **kwargs)


class AccountReminderLoop:
    """收盘前与风险度的常驻入口 (2026-10-06): every REMINDER_LOOP_SECONDS, each assembled account's
    summary and positions — one read, the same snapshot for both checks (#458) — are judged for each
    of the account's owners by check_positions / check_account, exactly as a client's read would. The
    dedup row makes the two entries (and two blue-green slots) one reminder. Main runtime only: a
    replay keeps reminding from its own advance."""

    def __init__(self, app: Any, *, interval: float = REMINDER_LOOP_SECONDS) -> None:
        self.app = app
        self.interval = interval
        self._task: asyncio.Task[None] | None = None
        from .mt5_bridge_alerts import Mt5BridgeAlerts
        self.mt5_alerts = Mt5BridgeAlerts(app)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self.mt5_alerts.reset()
            self.mt5_alerts.log_started()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 — one bad round never ends the loop
                log.warning("account_reminder_loop_failed", exc_info=True)

    async def run_once(self) -> int:
        """One round; returns how many accounts were read."""
        app, store = self.app, self.app.state.store
        try:
            await self.mt5_alerts.check()
        except Exception:  # noqa: BLE001 — an admin alert never blocks account reminders
            log.warning("mt5_bridge_alert_check_failed", exc_info=True)
        read = 0
        for services in list(app.state.accounts.all()):
            owners = store.account_owner_ids(services.account_id)
            if not owners:
                continue
            try:
                summary = services.provider.account_summary("")
                positions = services.provider.positions()
            except Exception as error:  # noqa: BLE001 — an account still connecting is read next round
                log.debug("reminder loop read skipped account=%s: %s", services.account_id, error)
                continue
            read += 1
            for user_id in owners:
                await check_positions(app, user_id=user_id, account_id=services.account_id,
                                      positions=positions, summary=summary)
                await check_account(app, user_id=user_id, account_id=services.account_id, summary=summary)
                # 保本提醒 (2026-10-06): the same snapshot judges whether a protected position's profit
                # has covered its risk (breakeven.py).
                await check_breakeven(app, user_id=user_id, account_id=services.account_id, services=services,
                                      positions=positions, summary=summary)
                # 交割提醒 (#932): judged here, on the same read, instead of on a client's positions read.
                await check_delivery(app, user_id=user_id, positions=positions, account_id=services.account_id)
        return read
