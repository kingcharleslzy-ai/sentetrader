"""本站模拟盘的成交模型（2026-10-13，所有者批准“成交更接近真实交易”）。

TqSdk 官方 SimTrade 的撮合是“理想成交”：价格满足就按下单那一刻的行情整笔成交，
不看盘口量、不排队（10-12 夜盘 rb 对价平今 3091：本站立即成交、快期排队未成，盘口
随即下移）。本模块只定义“真实模式”的模型与纯计算，记账仍全部交给 SimTrade
（paper_book.py 把每一笔成交拆成子单交给 SimTrade 的整笔成交记账）。

快期免费行情只有一档盘口（买一/卖一价与量）、累计成交量 volume 与最新价，所以模型
按一档数据设计，三件事：

1. 时间差：委托到达撮合的时刻 = 下单时刻 + ``latency_ms``；到达后收到的下一笔行情
   才判断与成交（借鉴 vn.py 本地模拟盘 vnpy_paperaccount 的默认行为——
   ``instant_trade`` 关闭时委托等下一笔 tick 才撮合；MIT 许可，只借用行为，不含代码）。
   行情静止（没有新行情）超过 ``arrival_wait_ms`` 就按缓存里的最新盘口到达。
2. 盘口量（吃单）：到达时可成交的部分先吃对手一档的量，按对手价成交；超出部分按
   合成盘口逐档成交——每多吃“一档量”多滑 ``impact_ticks`` 跳，直到委托限价为止，
   限价以外的剩余转为挂单排队（与交易所限价单“能成交部分成交、其余挂在限价上”的
   语义一致）。合成档位借鉴 Nautilus Trader 的 TwoTierFillModel/ThreeTierFillModel
   （一档之外按更差的价位提供流动性；LGPL-3.0，只借用模型思路，不含代码）；同一
   账户同一笔行情内多张单共享这一笔行情的盘口量（Nautilus 的 liquidity_consumption）。
3. 挂单排队（被动成交）：进入队列时记下该价位同方向已有的量（排在队尾）；之后每笔
   行情用累计成交量的增量估算“在本价位成交的量”（最新价恰为本价位时，增量 ×
   ``queue_share`` 计入，保守系数），先消耗排在前面的量，超出部分才成交；该价位
   一档量变小时，队前量不超过当前一档量（hftbacktest 的 RiskAdverseQueueModel、
   Nautilus queue_position 的同一规则：成交推进队列、盘口更新只封顶不后移；
   hftbacktest MIT 许可，只借用规则，不含代码）。价格穿越本价位（最新价或对手价
   越过委托价）视为本价位已被吃穿，剩余全部按委托价成交；对手价恰好等于委托价时
   按对手一档量成交。

“理想模式”保留 SimTrade 原行为（加 #640 的对手价改善），供对比。规则回测
（strategy_backtest）的 slippageTicks 是“每次成交固定滑几跳”的 K 线口径，本模型的
滑点来自盘口价差与冲击，二者不共用参数（见 docs/ARCHITECTURE.md“本站模拟引擎”）。
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

MODE_REALISTIC = "realistic"
MODE_IDEAL = "ideal"
_MODES = (MODE_REALISTIC, MODE_IDEAL)

# 环境变量（paper 引擎容器）：模式与四个参数。
ENV_MODE = "AI_TRADER_PAPER_FILL_MODE"
ENV_LATENCY_MS = "AI_TRADER_PAPER_FILL_LATENCY_MS"
ENV_ARRIVAL_WAIT_MS = "AI_TRADER_PAPER_FILL_ARRIVAL_WAIT_MS"
ENV_IMPACT_TICKS = "AI_TRADER_PAPER_FILL_IMPACT_TICKS"
ENV_QUEUE_SHARE = "AI_TRADER_PAPER_FILL_QUEUE_SHARE"


@dataclass(frozen=True)
class FillModel:
    """成交模型配置（全引擎一份，随引擎启动读取）。

    mode           realistic（真实，生产默认）/ ideal（理想：SimTrade 原行为）
    latency_ms     委托从下单到到达撮合的时间差（默认 300ms，约快期一档快照间隔的一半多）
    arrival_wait_ms 到达后最多等多久的新行情；超时按缓存最新盘口到达（行情静止时盘口没变）
    impact_ticks   吃单超过对手一档量后，每多吃一档量多滑几跳（默认 1）
    queue_share    排队推进的保守系数：最新价等于委托价时，成交量增量的多少计入本价位（默认 0.5）
    """

    mode: str = MODE_REALISTIC
    latency_ms: float = 300.0
    arrival_wait_ms: float = 500.0
    impact_ticks: int = 1
    queue_share: float = 0.5

    def __post_init__(self) -> None:
        if self.mode not in _MODES:
            raise ValueError(f"paper fill mode must be one of {_MODES}")
        if not (math.isfinite(self.latency_ms) and self.latency_ms >= 0):
            raise ValueError("paper fill latency_ms must be >= 0")
        if not (math.isfinite(self.arrival_wait_ms) and self.arrival_wait_ms >= 0):
            raise ValueError("paper fill arrival_wait_ms must be >= 0")
        if self.impact_ticks < 0:
            raise ValueError("paper fill impact_ticks must be >= 0")
        if not (math.isfinite(self.queue_share) and 0 < self.queue_share <= 1):
            raise ValueError("paper fill queue_share must be in (0, 1]")

    @property
    def realistic(self) -> bool:
        return self.mode == MODE_REALISTIC

    @property
    def latency_ns(self) -> int:
        return int(self.latency_ms * 1_000_000)

    @property
    def arrival_wait_ns(self) -> int:
        return int(self.arrival_wait_ms * 1_000_000)

    def describe(self) -> dict[str, Any]:
        """健康接口与成交记录里的模型口径（驼峰键）。"""
        values = asdict(self)
        return {"mode": values["mode"], "latencyMs": values["latency_ms"],
                "arrivalWaitMs": values["arrival_wait_ms"],
                "impactTicks": values["impact_ticks"], "queueShare": values["queue_share"]}

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> FillModel:
        """容器环境变量 → 配置；没设的取默认（真实模式）。非法值让引擎启动失败，
        不悄悄退回某个模式。"""
        def number(name: str, default: float) -> float:
            raw = str(env.get(name) or "").strip()
            return float(raw) if raw else default

        mode = str(env.get(ENV_MODE) or "").strip().lower() or MODE_REALISTIC
        return cls(
            mode=mode,
            latency_ms=number(ENV_LATENCY_MS, cls.latency_ms),
            arrival_wait_ms=number(ENV_ARRIVAL_WAIT_MS, cls.arrival_wait_ms),
            impact_ticks=int(number(ENV_IMPACT_TICKS, cls.impact_ticks)),
            queue_share=number(ENV_QUEUE_SHARE, cls.queue_share),
        )


IDEAL = FillModel(mode=MODE_IDEAL)


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def on_tick(price: float, tick: float) -> float:
    """按最小变动价位取整并消掉浮点尾差（合成档位价格 = 对手价 ± k 跳）。"""
    return round(round(price / tick) * tick, 10)


def counter_side(direction: str, quote: Mapping[str, Any]) -> tuple[float | None, float]:
    """对手一档：买单看卖一，卖单看买一。价格缺失（涨跌停无对手盘）为 None。"""
    if direction == "BUY":
        return _finite(quote.get("ask_price1")), _finite(quote.get("ask_volume1")) or 0.0
    return _finite(quote.get("bid_price1")), _finite(quote.get("bid_volume1")) or 0.0


def same_side(direction: str, quote: Mapping[str, Any]) -> tuple[float | None, float]:
    """同方向一档：买单看买一，卖单看卖一（排队的价位）。"""
    if direction == "BUY":
        return _finite(quote.get("bid_price1")), _finite(quote.get("bid_volume1")) or 0.0
    return _finite(quote.get("ask_price1")), _finite(quote.get("ask_volume1")) or 0.0


def decision_price(direction: str, quote: Mapping[str, Any]) -> float | None:
    """下单时刻的参照价：对手一档价（对价成交的价格）；没有对手盘时用最新价。"""
    price, _ = counter_side(direction, quote)
    return price if price is not None else _finite(quote.get("last_price"))


def _better_or_equal(direction: str, price: float, limit: float, eps: float) -> bool:
    """price 是否在委托限价以内（买：不高于限价；卖：不低于限价）。"""
    return price <= limit + eps if direction == "BUY" else price >= limit - eps


def taker_plan(direction: str, limit: float, remaining: int, quote: Mapping[str, Any],
               tick: float, impact_ticks: int,
               consumed: Mapping[float, float] | None = None) -> list[tuple[float, int, int]]:
    """到达时吃单的成交计划：[(成交价, 手数, 相对对手价的冲击跳数)]。

    对手一档价在限价以内才成交。第 0 档 = 对手一档（扣掉本笔行情里已被同账户别的单
    吃掉的量），之后每档量 = 对手一档量、价格每档再差 impact_ticks 跳，超出限价即停；
    impact_ticks=0 时对手价上量不限（等同理想成交）。手数取整、不超过 remaining。"""
    counter, depth = counter_side(direction, quote)
    if counter is None or remaining <= 0 or tick <= 0:
        return []
    eps = tick * 1e-6
    if not _better_or_equal(direction, counter, limit, eps):
        return []
    # 不能写 `consumed or {}`：已耗量未知（paper_book._AllUsed，空 dict 子类、每价位都
    # 答用尽）也是空的，会被当成“没吃过”整档再供应一次（#837）。
    used = consumed if consumed is not None else {}
    sign = 1 if direction == "BUY" else -1
    plan: list[tuple[float, int, int]] = []
    left = remaining
    level = 0
    # 档数上限 = 从对手价到限价之间的合成档数（同一笔行情里前面的单已吃掉的档
    # 要跳过，不能按本单手数估档数）；一档量为 0 时没有可吃的量。
    if impact_ticks > 0:
        if depth < 1:
            return []
        max_levels = int(abs(limit - counter) / (impact_ticks * tick) + 1e-6) + 1
    else:
        max_levels = 1
    while left > 0 and level < max_levels:
        price = on_tick(counter + sign * level * impact_ticks * tick, tick)
        if not _better_or_equal(direction, price, limit, eps):
            break
        if impact_ticks == 0:
            available = float(left)
        else:
            available = max(0.0, depth - float(used.get(price, 0.0)))
        take = min(left, int(math.floor(available + 1e-9)))
        if take > 0:
            plan.append((price, take, level * impact_ticks))
            left -= take
        if impact_ticks == 0:
            break
        level += 1
    return plan


def join_queue(direction: str, limit: float, quote: Mapping[str, Any], tick: float) -> float | None:
    """进入队列时排在前面的量：委托价就是同方向一档价 → 一档量（排队尾）；委托价比
    同方向一档更优（在价差里）→ 0（本价位只有自己）；更差（看不到那一档）→ None，
    等它成为一档时再按当时一档量排队尾。"""
    best, depth = same_side(direction, quote)
    if best is None:
        return 0.0  # 同方向没有挂单（如跌停时的买方）：本价位只有自己
    eps = tick * 1e-6
    if abs(best - limit) <= eps:
        return float(depth)
    better = limit > best if direction == "BUY" else limit < best
    return 0.0 if better else None


@dataclass
class QueueStep:
    """一笔行情对一张挂单的结果：fill 手数、是否穿价（全部成交）、新的队前量与成交量基准。"""

    fill: int
    crossed: bool
    queue_ahead: float | None
    last_volume: float | None


def queue_step(direction: str, limit: float, remaining: int, queue_ahead: float | None,
               last_volume: float | None, quote: Mapping[str, Any], tick: float,
               queue_share: float, consumed_at_limit: float = 0.0) -> QueueStep:
    """挂单在一笔新行情上的被动成交（价格一律是委托价）。

    * 穿价：买单最新价 < 委托价或卖一 < 委托价（卖单反向）→ 剩余全部成交；
    * 对手价恰等于委托价：按对手一档量（扣同笔行情已用量）成交，队前量清零；
    * 否则排队：成交量增量在最新价恰为委托价时 × queue_share 计入本价位，先消耗队前量，
      超出部分（扣掉同笔行情本账户本价位已成交的量）成交，不足一手的余量记为负的
      队前量带到下一笔；委托价是同方向一档时队前量不超过当前一档量；委托价优于同方向
      一档时队前量为 0；看不到本价位时队前量不变（尚未排上的保持 None）。
    累计成交量倒退（换日重置）或缺失时只重设基准，不推进队列。"""
    eps = tick * 1e-6
    buy = direction == "BUY"
    volume = _finite(quote.get("volume"))
    last = _finite(quote.get("last_price"))
    counter, counter_depth = counter_side(direction, quote)
    if remaining <= 0:
        return QueueStep(0, False, queue_ahead, volume)
    through = (last is not None and (last < limit - eps if buy else last > limit + eps)) or (
        counter is not None and (counter < limit - eps if buy else counter > limit + eps))
    if through:
        return QueueStep(remaining, True, 0.0, volume)
    if counter is not None and abs(counter - limit) <= eps:
        available = max(0.0, counter_depth - consumed_at_limit)
        take = min(remaining, int(math.floor(available + 1e-9)))
        return QueueStep(take, False, 0.0, volume)
    ahead = queue_ahead
    best, depth = same_side(direction, quote)
    if ahead is None:
        ahead = join_queue(direction, limit, quote, tick)
        if ahead is None:
            return QueueStep(0, False, None, volume)
        # 刚排上（本价位此刻才看得见）：之前的成交不属于本价位的队列，只定基准。
        return QueueStep(0, False, ahead, volume)
    traded = 0.0
    if volume is not None and last_volume is not None and volume > last_volume:
        if last is not None and abs(last - limit) <= eps:
            traded = (volume - last_volume) * queue_share
    ahead -= traded
    capacity = 0.0
    if ahead < 0:
        capacity = -ahead
        ahead = 0.0
    if best is not None and abs(best - limit) <= eps:
        ahead = min(ahead, float(depth))
    elif best is None or (limit > best + eps if buy else limit < best - eps):
        ahead = 0.0
    # 同一笔行情里本账户在本价位已成交的量（别的挂单、吃单）先占用这份被动成交量：
    # 同价位多张挂单共享一份，不各拿一份（审查 P1，2026-10-13）。
    capacity = max(0.0, capacity - consumed_at_limit)
    take = min(remaining, int(math.floor(capacity + 1e-9)))
    # 不足一手的余量留到下一笔行情（记为负的队前量），不每笔丢掉：系数 0.5、每笔
    # 只成交 1 手的价位也能累计成交（审查 P1，2026-10-13）。
    leftover = capacity - take
    if take < remaining and leftover > 1e-9:
        ahead = -leftover
    return QueueStep(take, False, ahead, volume)
