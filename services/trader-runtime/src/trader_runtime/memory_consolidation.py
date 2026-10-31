"""私人记忆 3b（2026-09-30）：每天北京时间 06:00 为前 24 小时有活动的用户整理一次。

两件事：程序统计（memory_stats 算画像/经验，按 statKey 就地更新，用户删掉的不加回）＋
从用户原话提炼（一次便宜模型调用，最多 5 个操作，逐条程序校验——引用的原话必须真在
材料里、不能是授权、约定层只收原话；不合格丢弃）。AI 的回复不作为依据，任何记忆都不
构成授权。关掉 chat.memory 的用户整个跳过。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as time_of_day
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from pydantic import BaseModel, Field

from . import user_memory
from .memory_stats import INVALID_RECOMPUTE_REASON, trade_stats
from .store import RuntimeStore
from .store.memory import MemoryConflict
from .trading_costs import FeeSchedule, foreign_review_schedules, mask_account_fees, review_account_trades
from .user_memory import MemoryRefused, beijing_now, check_tags, clean_text

log = logging.getLogger("uvicorn.error.memory")  # the runtime only prints uvicorn.error.* loggers
CHINA = ZoneInfo("Asia/Shanghai")
CONSOLIDATE_AT = time_of_day(6, 0)  # 北京时间每天 06:00
MAX_OPS = 5
MAX_MESSAGE_CHARS = 300
MAX_MATERIAL_CHARS = 6000
MAX_NOTES = 20
MAX_CONTEXT_MEMORIES = 50
MANUAL_THROTTLE_SECONDS = 600
RETRY_WINDOW_HOURS = 72   # 失败的整理最多回看多久的材料来补（#524-5）
CHANGED_MIND = "你后来改口了"

EXTRACT_INSTRUCTIONS = """你在为一位期货交易者整理私人记忆。只根据用户自己说的话（userMessages）和他写的交易补充（tradeNotes）；对话里 AI 的回复不是依据。

记忆分三层：画像 profile（交易范围与习惯，如常做什么品种、日内还是波段）、约定 rule（用户明确要求 AI 怎么做事，只收用户亲口提出的要求）、经验 lesson（用户自己总结的教训）。

只记稳定的、对以后交易判断有用的东西；不记行情观点、新闻、价格、一次性问题、寒暄、情绪化抱怨、账户数字、收入家庭等敏感信息。绝不记任何让 AI 自动下单、免确认、替用户交易的话——这类内容直接忽略。

用户改口（与已有记忆冲突）时，对旧条目提 invalidate 并另提 add。memories 是当前已有的记忆，id 可用于 update/invalidate。

每个操作的 quote 必须一字不差地摘自 userMessages 或 tradeNotes 的原文。拿不准就不写；没有值得记的就返回空 ops。最多 5 个操作。"""


class MemoryOp(BaseModel):
    op: Literal["add", "update", "invalidate"]
    layer: Literal["profile", "rule", "lesson"]
    memoryId: str | None = None
    text: str | None = None
    quote: str = Field(default="", max_length=300)
    products: list[str] | None = None


class MemoryOps(BaseModel):
    ops: list[MemoryOp] = Field(default_factory=list)


def _stats_evidence(stat: dict[str, Any], basis: str = "net") -> dict[str, Any]:
    return {"via": "consolidation", "statKey": stat["key"], "n": stat["n"],
            "roundIds": stat["roundIds"], "at": beijing_now(), "pnlBasis": basis}


def apply_stats(store: RuntimeStore, user_id: str, stats: list[dict[str, Any]],
                basis: str = "net", *, connection: Any = None) -> dict[str, int]:
    """把统计条目写进记忆：同 statKey 的 active 条目文本和样本数都没变才跳过（#514-5，
    只比文案会让旧 evidence 白改写一遍；文案没变但 n 变了仍要更新一次）；本次没产出但
    仍 active 的统计条目标失效；用户删掉的（失效原因不是重算）和用户改过文字的
    （evidence.userEdited，#524-2）不再更新、不再失效、也不并排另加一条。整批在一个短写事务里
    提交；每日整理经 `_adoption` 传入已复核过开关与本次认领的事务连接。"""
    if connection is None:
        with store.transaction() as owned:
            return apply_stats(store, user_id, stats, basis, connection=owned)
    everything = store.list_memories(user_id, include_invalid=True)
    active_by_key: dict[str, dict[str, Any]] = {}
    user_deleted: set[str] = set()
    user_edited: set[str] = set()
    for item in everything:
        if item["source"] != "stats":
            continue
        key = str((item["evidence"] or {}).get("statKey") or "")
        if not key:
            continue
        if item["status"] == "active":
            active_by_key[key] = item
            if (item["evidence"] or {}).get("userEdited"):
                user_edited.add(key)
        elif item["invalidReason"] != INVALID_RECOMPUTE_REASON:
            user_deleted.add(key)
    added = updated = invalidated = skipped = 0
    for stat in stats:
        key, current = stat["key"], active_by_key.get(stat["key"])
        if key in user_edited:
            skipped += 1  # 用户改过文字的统计条目：以用户为准，不覆盖也不并排再记
            continue
        if current is not None:
            if current["text"] == stat["text"] \
                    and int((current["evidence"] or {}).get("n") or 0) == int(stat["n"]):
                skipped += 1
                continue
            store.update_memory_text_and_evidence(
                current["id"], user_id=user_id, text=stat["text"],
                evidence=_stats_evidence(stat, basis), actor="consolidation", connection=connection)
            updated += 1
        elif key in user_deleted:
            skipped += 1  # 用户删过的统计，不再自动加回来
        else:
            store.insert_memory(user_id, stat["layer"], stat["text"], "stats",
                                _stats_evidence(stat, basis), stat["tags"], actor="consolidation",
                                connection=connection)
            added += 1
    for key, current in active_by_key.items():
        if key in user_edited or key in {stat["key"] for stat in stats}:
            continue   # 用户改过的、本轮还有产出的统计条目都不失效
        store.set_memory_status(current["id"], user_id=user_id, status="invalid",
                                reason=INVALID_RECOMPUTE_REASON, actor="consolidation", connection=connection)
        invalidated += 1
    return {"added": added, "updated": updated, "invalidated": invalidated, "skipped": skipped}


@dataclass
class _Gate:
    """`_adoption` 的复核结果：skipped 非空就一条都不写；live 是仍有效的材料 key。"""
    connection: Any
    skipped: str | None = None
    live: set[str] = field(default_factory=set)


@contextmanager
def _adoption(store: RuntimeStore, user_id: str, *, attempt: dict[str, str] | None,
              materials: list[dict[str, Any]] | None = None) -> Iterator[_Gate]:
    """统计与原话提炼共用的采纳前复核（#524-3/#524-4）：在写记忆的同一个短写事务里重读
    当前的记忆开关、本次整理认领是否仍有效（没被取消、没记失败、没被别人接手）、材料是否
    仍在且还是读取时那一版。开关或认领不过，本次一条都不写；材料不在的候选逐条拒掉。模型
    等待和统计计算都在这之前，不持写锁。attempt 为 None 只用于直接调用，每日/手动整理总会带上。"""
    with store.transaction() as connection:
        skipped: str | None = None
        if not user_memory._memory_enabled(store, user_id):
            skipped = "disabled"    # 等待期间用户把记忆关了
        elif attempt is not None and not store.memory_run_current(
                user_id, attempt["day"], attempt["attempt"], connection=connection):
            skipped = "cancelled"   # 这次整理已取消、已记失败或已被新的一次接手
        tracked = [material for material in materials or [] if material.get("key")]
        live = store.memory_materials_live(user_id, tracked, connection=connection) if skipped is None else set()
        yield _Gate(connection, skipped, live)


def _material(item: str | dict[str, Any]) -> dict[str, Any]:
    """正式整理传带来源身份的材料（store.user_texts_between / notes_between）；直接传字符串的
    调用没有来源可复核，也不算比删除更新的新话。"""
    if isinstance(item, str):
        return {"key": None, "text": item, "at": None}
    return item


def _clip_material(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每条截 300 字，从最新往前装，总共 ≤ 6000 字。返回实际送给模型的那几条：seen 是模型看到的
    截断文字（原话只在这里核），text 保留原文供来源复核。"""
    picked: list[dict[str, Any]] = []
    total = 0
    for item in words:  # user_texts_between 已是最新在前
        clipped = item["text"][:MAX_MESSAGE_CHARS]
        if total + len(clipped) > MAX_MATERIAL_CHARS and picked:
            break
        picked.append({**item, "seen": clipped})
        total += len(clipped)
    return list(reversed(picked))


def _time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _newer_than_deletions(material: dict[str, Any], deleted_at: list[datetime | None]) -> bool:
    """删除抑制按来源版本划界（#524-3）：用户删掉一条记忆以后，删除之前就有的材料（同一句话
    原样重放）不再把它加回来；删除之后用户新说的话、新改的补充是新的决定，照常可以记。"""
    said = _time(material.get("at"))
    # 材料时间是截断的（补充到毫秒，升级前的旧补充到秒），真实时间 ≥ said：只有 said 晚于删除才
    # 确定在删除之后；分不清先后时按删除之前处理——宁可少记，不把删掉的加回来。
    return said is not None and all(at is not None and said > at for at in deleted_at)


def _validate_op(op: MemoryOp, materials: list[dict[str, Any]], live: set[str],
                 active: list[dict[str, Any]], everything: list[dict[str, Any]]) -> str | None:
    """返回拒绝原因（作为 rejected 计数的键），合格返回 None。原话必须出自仍有效的材料：
    来源会话被删、补充被改或删以后，等待模型期间的旧候选不再采纳（#524-3）。"""
    quote = user_memory._normalized(op.quote)
    if len(quote) < 2:
        return "原话太短"
    quoted = [material for material in materials if quote in user_memory._normalized(material["seen"])]
    if not quoted:
        return "原话对不上"
    supporting = [material for material in quoted if not material.get("key") or material["key"] in live]
    if not supporting:
        return "原话已删除或已改"
    cleaned: str | None = None
    if op.op in ("add", "update"):
        if not op.text:
            return "缺少内容"
        try:
            cleaned = clean_text(op.text)
            check_tags(list(op.products or []))
        except MemoryRefused as error:
            return str(error)
    own = [item for item in active if item["source"] == "user"]
    if op.op in ("update", "invalidate"):
        if not op.memoryId or not any(item["id"] == op.memoryId for item in own):
            return "目标不是你可改的记忆"
    if op.op == "add" and cleaned is not None:
        same = [item for item in everything
                if user_memory._normalized(item["text"]) == user_memory._normalized(cleaned)]
        if any(item["status"] == "active" for item in same):
            return "重复"
        deleted_at = [_time(item["invalidAt"]) for item in same]
        if deleted_at and not any(_newer_than_deletions(material, deleted_at) for material in supporting):
            return "重复"   # 用户删掉的话，删除前的同一批材料不原样加回来（#524-4）
    return None


async def extract_from_words(adapter: Any, store: RuntimeStore, user_id: str,
                             texts: list[str] | list[dict[str, Any]], notes: list[str] | list[dict[str, Any]], *,
                             now: datetime | None = None, attempt: dict[str, str] | None = None) -> dict[str, Any]:
    """从用户自己的原话和补充里提炼记忆：一次模型调用 + 逐条程序校验。AI 的回复不是依据。
    模型最长等 60 秒，回来后经 `_adoption` 在一个短写事务里复核开关、本次认领与材料，再对着
    整份记忆清单逐条验（#524-3）：用户等这一分钟里关掉记忆、删掉原话或改掉某条，都不写。"""
    words = [item for item in map(_material, texts or []) if user_memory._normalized(item["text"])]
    supplements = [item for item in map(_material, notes or []) if user_memory._normalized(item["text"])]
    if not words and not supplements:
        return {"skipped": "no_words"}
    active = [item for item in store.list_memories(user_id) if item["status"] == "active"]
    model_memories = [item for item in active if item["source"] == "user"][:MAX_CONTEXT_MEMORIES]
    # Only these versions were visible to this model call. Never authorize a new
    # target by refreshing its version after await; successful same-batch writes
    # advance only that already-authorized target.
    authorized = {item["id"]: item["revision"] for item in model_memories}
    # 只有真正送给模型的材料才能作原话依据：没进 prompt 的旧补充、超出字数的旧消息不算（#524）。
    words = _clip_material(words)
    # 补充取最新的（#524-9，notes_between 已按新到旧取好转正）。
    supplements = [{**item, "seen": item["text"][:MAX_MESSAGE_CHARS]} for item in supplements[-MAX_NOTES:]]
    payload = {"now": (now or datetime.now(UTC)).astimezone(CHINA).isoformat(timespec="minutes"),
               "userMessages": [item["seen"] for item in words],
               # 现有记忆封顶 #524-11：几千条时不把 prompt 撑爆，最旧的先不进材料。
               "tradeNotes": [{"text": item["seen"]} for item in supplements],
               "memories": [{"id": item["id"], "layer": item["layer"], "text": item["text"]}
                            for item in model_memories]}
    try:
        result = await adapter.complete_structured(
            instructions=EXTRACT_INSTRUCTIONS, input_text=json.dumps(payload, ensure_ascii=False),
            schema=MemoryOps, timeout_seconds=60, usage_user=user_id)
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 — 模型失败只让提炼空手而归，不影响统计部分
        log.warning("memory_extract_failed user=%s error=%s", user_id, type(error).__name__)
        return {"error": type(error).__name__}
    materials = [*words, *supplements]
    with _adoption(store, user_id, attempt=attempt, materials=materials) as gate:
        if gate.skipped is not None:
            return {"skipped": gate.skipped}
        connection = gate.connection
        everything = store.list_memories(user_id, include_invalid=True)
        active = [item for item in everything if item["status"] == "active"]
        applied = 0
        rejected: dict[str, int] = {}
        ops: dict[str, int] = {"add": 0, "update": 0, "invalidate": 0}
        at = beijing_now()
        for op in list(getattr(result.value, "ops", []) or [])[:MAX_OPS]:
            reason = _validate_op(op, materials, gate.live, active, everything)
            if reason is None and op.op != "add" and op.memoryId not in authorized:
                reason = "目标记忆未提供给本轮提炼"
            if reason is not None:
                rejected[reason] = rejected.get(reason, 0) + 1
                continue
            if op.op == "add":
                memory = store.insert_memory(user_id, op.layer, clean_text(op.text or ""), "user",
                                             {"via": "consolidation", "quote": op.quote, "at": at},
                                             list(op.products or []), actor="consolidation", connection=connection)
                active.append(memory)         # 同一批的后续操作看得到刚加的这条（重复判定）
                everything.append(memory)
                ops["add"] += 1
                applied += 1
                continue
            memory_id = op.memoryId or ""
            before = next(item for item in active if item["id"] == memory_id)
            try:
                if op.op == "update":
                    evidence = {**(before["evidence"] or {}), "via": "consolidation", "quote": op.quote, "at": at}
                    after = store.update_memory_text_and_evidence(
                        memory_id, user_id=user_id, text=clean_text(op.text or ""), evidence=evidence,
                        actor="consolidation", expected_revision=authorized[memory_id], connection=connection)
                else:
                    after = store.set_memory_status(memory_id, user_id=user_id, status="invalid",
                                                    reason=CHANGED_MIND, actor="consolidation",
                                                    expected_revision=authorized[memory_id], connection=connection)
            except MemoryConflict:
                rejected["目标记忆已更新"] = rejected.get("目标记忆已更新", 0) + 1
                continue
            # 同批后续操作对着改过的版本再验（#524-5），不留在过期快照上。本批自己因改口失效的
            # 条目既不再当目标，也不算“用户删过”：同批改记到别的层照常可以。
            assert after is not None
            authorized[memory_id] = after["revision"]
            everything = [after if item["id"] == memory_id else item for item in everything
                          if op.op == "update" or item["id"] != memory_id]
            active = [item for item in everything if item["status"] == "active"]
            ops[op.op] += 1
            applied += 1
    return {"applied": applied, "ops": ops, "rejected": rejected, "tokens": int(getattr(result, "tokens", 0) or 0)}


class MemoryConsolidator:
    """每天 06:00（北京）跑一次 run()；认领表保证两个进程/重启都不重复整理同一天。"""

    def __init__(self, application: FastAPI, store: RuntimeStore, desk: Any,
                 adapter_for: Callable[[str], Any] | None = None) -> None:
        self._application = application
        self.store = store
        self.desk = desk  # ctx.services.research_desk，view_at 读开仓当时的调研判断
        self._adapter_for = adapter_for  # 测试注入；默认按用户取 application.state.adapter
        self._task: asyncio.Task[None] | None = None
        self._manual_at: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}   # #524-7: 每日整理与“现在整理一次”按用户串行

    def _user_lock(self, user_id: str) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = self._locks[user_id] = asyncio.Lock()
        return lock

    def _adapter(self, user_id: str) -> Any:
        if self._adapter_for is not None:
            return self._adapter_for(user_id)
        adapter = self._application.state.adapter
        return adapter.adapter_for_user(user_id) if hasattr(adapter, "adapter_for_user") else adapter

    def _product_names(self) -> dict[str, str]:
        try:
            items = self._application.state.data_provider.instrument_catalog()["items"]
        except Exception:  # noqa: BLE001 — 拿不到目录就用合约代码里的品种
            items = []
        names = {}
        for item in items:
            name = str(item.get("instrumentName") or "").rstrip("0123456789")
            if name:
                names[item["symbol"]] = name
        return names

    def _stats_for(self, user_id: str, now: datetime, attempt: dict[str, str] | None = None) -> dict[str, Any]:
        """在工作线程里算统计（不持写锁），再经 `_adoption` 复核开关与本次认领后一次提交
        （#524-4）：等统计期间用户关了记忆、或这次整理已取消/记失败/被接手，线程晚回来也不写。"""
        stats, basis = self._compute_stats(user_id, now)
        with _adoption(self.store, user_id, attempt=attempt) as gate:
            if gate.skipped is not None:
                return {"skipped": gate.skipped}
            return apply_stats(self.store, user_id, stats, basis, connection=gate.connection)

    def _compute_stats(self, user_id: str, now: datetime) -> tuple[list[dict[str, Any]], str]:
        from .strategy_workbench import apply_round_playbooks, journal_fields, order_sources  # 与复盘同一条链路
        rounds: list[dict[str, Any]] = []
        fees = getattr(self._application.state, "fee_settings", None)
        schedule = fees.get(user_id) if fees is not None else FeeSchedule().model_dump()
        sources = order_sources(self.store, user_id)
        names = self._product_names()
        basis = "net"
        accounts = getattr(self._application.state, "accounts", None)
        for account in self.store.list_trading_accounts():
            if account["execution_owner_user_id"] != user_id:
                continue
            fills = self.store.list_account_trades(trading_account_id=account["id"])
            multipliers: dict[str, float] = {}
            for fill in fills:  # 成交里存的 volumeMultiple，没有的合约跳过
                multiple = fill.get("volumeMultiple")
                if multiple and fill["symbol"] not in multipliers:
                    multipliers[fill["symbol"]] = float(multiple)
            if not multipliers:
                continue
            # 外盘合约（美元子账户）与复盘同一取法：按外盘引擎的每手美元手续费；账户没装配、读不到时遮成毛收益。
            services = accounts.get(account["id"]) if accounts is not None else None
            foreign = foreign_review_schedules(getattr(services, "provider", None), list(multipliers))
            result = review_account_trades(fills, schedule, multipliers, days=90, sources=sources, schedules=foreign)
            # 与复盘同一层费用遮罩 (#514-4)：账户默认费率时统计退回毛收益，不套残留的自定义
            # 费率；evidence 的 pnlBasis 注明口径，trade_stats 对 net=None 自动落在毛收益上。
            # 有一个账户落在毛收益，整份统计就注明毛收益（不让后读的美元子账户把口径改成净）。
            if mask_account_fees(result, schedule, charged=foreign) == "gross":
                basis = "gross"
            journal_fields(result["trades"], self.store.list_trade_notes(trading_account_id=account["id"]),
                           self.desk)
            apply_round_playbooks(self.store, result["trades"], trading_account_id=account["id"], user=user_id)
            rounds.extend(result["trades"])
        return trade_stats(rounds, names=names, now_ns=int(now.timestamp() * 1e9)), basis

    async def _consolidate_user(self, user_id: str, start: datetime, end: datetime,
                                now: datetime, attempt: dict[str, str] | None = None) -> dict[str, Any]:
        stats = await asyncio.to_thread(self._stats_for, user_id, now, attempt)
        words = await extract_from_words(
            self._adapter(user_id), self.store, user_id,
            await asyncio.to_thread(self.store.user_texts_between, user_id, start.isoformat(), end.isoformat()),
            await asyncio.to_thread(self.store.notes_between, user_id, start.isoformat(), end.isoformat()),
            now=now, attempt=attempt)
        return {"stats": stats, "words": words}

    def _window_start(self, user_id: str, now: datetime, retry: dict[str, str]) -> str:
        """材料起点：平常是 24 小时前；上次整理失败没补上的，从失败那次的起点接着整理（最多
        回看 RETRY_WINDOW_HOURS），失败那天的材料不因滑出窗口而丢掉（#524-5）。"""
        start = now - timedelta(hours=24)
        carried = retry.get(user_id)
        return min(start, datetime.fromisoformat(carried)).isoformat() if carried else start.isoformat()

    async def _attempt(self, user_id: str, day: str, claim: dict[str, str], now: datetime) -> dict[str, Any]:
        """跑一次已认领的整理并按令牌收尾：任何一段失败、取消或抛错都记 error（可重试），
        全部完成才记 done；被别人接手后的收尾不覆盖新的一次。"""
        attempt = {"day": day, "attempt": claim["attempt"]}
        try:
            detail = await self._consolidate_user(user_id, datetime.fromisoformat(claim["windowStart"]), now, now,
                                                  attempt=attempt)
        except BaseException:
            # 取消或抛错先撤销本次认领的采纳资格，统计线程晚回来也写不进来，再把异常传出去。
            self.store.record_memory_run(user_id, day, "error", None, attempt=claim["attempt"])
            raise
        failed = any(isinstance(part, dict) and "error" in part for part in detail.values())
        self.store.record_memory_run(user_id, day, "error" if failed else "done", detail, attempt=claim["attempt"])
        return detail

    async def run(self, now: datetime | None = None) -> dict[str, Any]:
        """整理窗口 [now-24h, now) 内有活动、或上次整理失败待补的用户；单个用户出错不影响其他用户。"""
        now = now or datetime.now(UTC)
        start, end = now - timedelta(hours=24), now
        start_ns, end_ns = int(start.timestamp() * 1e9), int(end.timestamp() * 1e9)
        day = now.astimezone(CHINA).date().isoformat()
        retry = self.store.memory_retry_windows((now - timedelta(hours=RETRY_WINDOW_HOURS)).isoformat())
        users = sorted({*self.store.users_with_texts_between(start.isoformat(), end.isoformat()),
                        *self.store.users_with_trades_between(start_ns, end_ns),
                        *self.store.users_with_notes_between(start.isoformat(), end.isoformat()),
                        *retry})
        results: dict[str, Any] = {}
        for user_id in users:
            if not user_memory._memory_enabled(self.store, user_id):
                continue
            # 同一用户的每日整理与手动“现在整理一次”串行（#524-7），认领在锁内：谁也插不进同一天。
            async with self._user_lock(user_id):
                claim = self.store.claim_memory_run(user_id, day, window_start=self._window_start(user_id, now, retry))
                if claim is None:
                    continue
                try:
                    detail = await self._attempt(user_id, day, claim, now)
                except Exception:  # noqa: BLE001 — 一个用户的失败到此为止（已记 error）
                    log.exception("memory_consolidation_failed user=%s", user_id)
                    continue
                words = detail["words"]
                log.info("memory_consolidated user=%s stats=%s words=%s tokens=%s", user_id, detail["stats"],
                         words.get("applied", words.get("error", words.get("skipped"))), words.get("tokens", 0))
                results[user_id] = detail
        return results

    def allow_manual(self, user_id: str) -> bool:
        """“现在整理一次”的节流：同一用户 10 分钟内只允许一次。"""
        now = time.monotonic()
        if now - self._manual_at.get(user_id, -MANUAL_THROTTLE_SECONDS) < MANUAL_THROTTLE_SECONDS:
            return False
        self._manual_at[user_id] = now
        return True

    async def run_manual(self, user_id: str, now: datetime | None = None) -> dict[str, Any]:
        """设置页的“现在整理一次”：只整理当前用户，接手当天的认领行（新令牌，旧的一次再写不进来）；
        与每日整理串行（#524-7）。失败如实记 error，不写 done。"""
        now = now or datetime.now(UTC)
        day = now.astimezone(CHINA).date().isoformat()
        retry = self.store.memory_retry_windows((now - timedelta(hours=RETRY_WINDOW_HOURS)).isoformat())
        async with self._user_lock(user_id):
            claim = self.store.claim_memory_run(user_id, day, window_start=self._window_start(user_id, now, retry),
                                                force=True)
            assert claim is not None   # force 总能接手
            return await self._attempt(user_id, day, claim, now)

    async def _safe_run(self) -> None:
        try:
            await self.run()
        except Exception:  # noqa: BLE001 — 失败的整理不影响明天的循环
            log.exception("memory_consolidation_error")

    async def _loop(self) -> None:
        catch_up = True
        while True:
            now = datetime.now(UTC).astimezone(CHINA)
            target = datetime.combine(now.date(), CONSOLIDATE_AT, tzinfo=CHINA)
            if target <= now:
                if catch_up:  # 启动时今天 06:00 已过：稍等启动尘埃落定再补跑一次
                    catch_up = False
                    await asyncio.sleep(60)
                    await self._safe_run()
                    continue
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            catch_up = False
            await self._safe_run()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
