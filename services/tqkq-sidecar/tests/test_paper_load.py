"""本站模拟盘 PaperEngine 多账户并发压测（下架快期模拟盘验收第 1 步，所有者 2026-10-09 同意）。

保护什么：多账户并发下单/撤单/幂等重发下——
* 串户：任何账户的 orders/trades/query 里不得出现别的账户的 clientOrderId；
* 账不平：并发结果必须与"该账户在独立引擎里按同样轮次单独重放"逐项一致；
* 重复成交：同 commandId+clientOrderId 重发、两线程同轮并发同发，只产生一笔委托/成交；
* 重启丢账：第 1 天收盘结算后重启引擎，账本（account/positions/activity）逐项相等。
默认只跑小版本（5 账户，整个文件 <2 秒）。完整版（50/100 账户、每账户 20-50 笔）：
    PAPER_LOAD_FULL=1 uv run --project services/tqkq-sidecar pytest \
        services/tqkq-sidecar/tests/test_paper_load.py -q -s
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest

from test_paper_book import IC_RATE, RB_RATE, TOKEN, TRADING_DATES, au_quote
from tqkq_readonly_sidecar.paper import PaperEngine, PaperServer, VirtualClock
from tqkq_readonly_sidecar.paper_fill import IDEAL, FillModel

TOL = 0.01
FULL = bool(os.environ.get("PAPER_LOAD_FULL"))
# 成交模型（2026-10-13）：理想模式（SimTrade 原行为）与真实模式都跑一遍。脚本按“对价单
# 当场成交”记账，真实模式取时间差 0（到达即下单时刻），盘口量（卖一 9/买一 12）大于
# 每笔手数，于是走的是外层撮合 → 子单拆分记账这条路径，逐户一致与重启一致照样成立。
FILL_MODELS = {"ideal": IDEAL, "realistic": FillModel(latency_ms=0)}
START = datetime.fromisoformat("2026-11-02T09:00:00+08:00")
DAY1, DAY2 = "2026-11-02", "2026-11-03"

# 合约规格：覆盖 SHFE / CFFEX（完整版再加 DCE）；margin/commission 全程不变，生成器据此做资金预算。
SYMBOL_SPECS = [
    {"symbol": "SHFE.au2612", "product": "au", "base": 820.0, "step": 0.5, "tick": 0.02,
     "multiple": 1000.0, "margin": 80_000.0, "commission": 20.0, "lower": 740.0, "upper": 900.0,
     "name": "沪金2612", "rate": None},
    {"symbol": "SHFE.rb2701", "product": "rb", "base": 3071.0, "step": 3.0, "tick": 1.0,
     "multiple": 10.0, "margin": 4_000.0, "commission": 0.3116, "lower": 2800.0, "upper": 3300.0,
     "name": "螺纹钢2701", "rate": RB_RATE},
    {"symbol": "CFFEX.IC2612", "product": "IC", "base": 6000.0, "step": 10.0, "tick": 1.0,
     "multiple": 200.0, "margin": 100_000.0, "commission": 252.0, "lower": 5400.0, "upper": 6600.0,
     "name": "中证500期货2612", "rate": IC_RATE},
]
DCE_SPEC = {"symbol": "DCE.i2609", "product": "i", "base": 820.0, "step": 1.5, "tick": 0.5,
            "multiple": 100.0, "margin": 60_000.0, "commission": 8.0, "lower": 760.0, "upper": 880.0,
            "name": "铁矿石2609", "rate": None}

# 轮次时间表：(日, 时刻, 标签)。小版本 4 轮——open1 一轮内串行完成开仓/平今/挂撤单/
# 幂等重发/资金不足拒单，午休轮拒单，close 轮收盘结算后重启引擎，第 2 天一轮平昨/
# 并发同发/费率平仓；完整版再插入 extra 轮把每账户指令量放大到 20-50 笔。
PLAN = [
    (DAY1, "09:31", "open1"), (DAY1, "12:00", "lunch"), (DAY1, "15:10", "close"),
    (DAY2, "09:31", "day2"),
]
FULL_EXTRA = [
    (DAY1, "10:01", "extra"), (DAY1, "10:45", "extra"), (DAY1, "13:40", "extra"),
    (DAY1, "14:10", "extra"), (DAY2, "10:01", "extra"), (DAY2, "10:35", "extra"),
    (DAY2, "10:50", "extra"), (DAY2, "13:50", "extra"), (DAY2, "14:20", "extra"),
]


def _spec_price(spec: dict, index: int) -> float:
    # 确定的行情价格序列，始终落在最小变动价位上、涨跌停以内。
    return spec["base"] + (index % 6) * spec["step"]


def _make_quote(spec: dict, moment: str, index: int, settlement=None, pre_settlement=None) -> dict:
    price = _spec_price(spec, index)
    quote = au_quote(
        moment, price, bid=price - spec["tick"], ask=price + spec["tick"],
        symbol=spec["symbol"], margin=spec["margin"], commission=spec["commission"],
        settlement=settlement if settlement is not None else float("nan"),
        pre_settlement=pre_settlement if pre_settlement is not None else float("nan"))
    quote.update({"product_id": spec["product"], "volume_multiple": spec["multiple"],
                  "price_tick": spec["tick"], "upper_limit": spec["upper"],
                  "lower_limit": spec["lower"], "instrument_name": spec["name"]})
    return quote


@dataclass
class Cmd:
    kind: str                        # submit / cancel
    coid: str = ""
    command_id: str = ""
    request: dict = field(default_factory=dict)  # submit 请求原文；重发复用同一字典
    symbol: str = ""
    offset: str = ""
    expect_error: str | None = None  # None / "closed"（休市拒单）/ "insufficient"（资金不足拒单）
    duplicate: bool = False          # 原样重发（幂等检查）
    twin: bool = False               # 同轮双线程并发发同一条
    resting: bool = False            # 挂单（随后同轮撤掉）


@dataclass
class Round:
    moment: str
    tag: str
    quotes: list[dict]
    commands: dict[int, list[Cmd]] = field(default_factory=dict)
    stale: dict = field(default_factory=dict)  # 比本轮早 1 秒的旧行情，引擎按乱序整笔丢弃

    def quote_of(self, symbol: str) -> dict:
        return next(quote for quote in self.quotes if quote["symbol"] == symbol)


@dataclass
class AccountSpec:
    index: int
    account_id: str
    init_balance: float

    @property
    def low(self) -> bool:
        return self.init_balance < 200_000.0


@dataclass
class Script:
    rounds: list[Round]
    accounts: list[AccountSpec]
    restart_after: int
    counts: dict[str, int]

    def coids_upto(self, index: int, round_index: int) -> list[str]:
        # 该账户截至某轮的全部 submit clientOrderId（含挂单），按出现顺序去重。
        coids = [cmd.coid for round_ in self.rounds[:round_index + 1]
                 for cmd in round_.commands.get(index, []) if cmd.kind == "submit"]
        return list(dict.fromkeys(coids))


class Trader:
    """按固定种子生成一个账户的指令脚本；自己记账（今/昨仓位、保证金）保证指令合法。"""

    def __init__(self, meta: AccountSpec, specs: list[dict]) -> None:
        self.meta = meta
        self.rng = random.Random(0x5EED + meta.index * 7919)
        self.rb = next(s for s in specs if s["symbol"] == "SHFE.rb2701")
        self.au = next(s for s in specs if s["symbol"] == "SHFE.au2612")
        self.ic = next(s for s in specs if s["symbol"] == "CFFEX.IC2612")
        # 低余额账户正常交易只做 rb；IC 仅用来触发资金不足拒单。
        self.trade_specs = [self.rb] if meta.low else list(specs)
        self.pos: dict[tuple[str, str], list[int]] = {}  # (symbol, long/short) -> [今仓, 昨仓]
        self.margin_used = 0.0
        self.budget = meta.init_balance * 0.6
        self.seq = 0
        self.history: list[Cmd] = []  # 已即时成交的 submit，供幂等重发挑选

    def _coid(self) -> str:
        self.seq += 1
        return f"a{self.meta.index:03d}-{self.seq:03d}"

    def _emit(self, round_: Round, cmd: Cmd) -> None:
        round_.commands.setdefault(self.meta.index, []).append(cmd)

    def _budget_ok(self, spec: dict, volume: int) -> bool:
        return self.margin_used + volume * spec["margin"] + 20_000.0 <= self.budget

    def _vol(self, symbol: str, side: str) -> list[int]:
        return self.pos.get((symbol, side), [0, 0])

    def _open(self, round_: Round, spec: dict, volume: int, *, fee=None, expect=None, twin=False):
        if expect is None and not self._budget_ok(spec, volume):
            return None
        quote = round_.quote_of(spec["symbol"])
        direction = self.rng.choice(("BUY", "SELL"))
        price = quote["ask_price1"] if direction == "BUY" else quote["bid_price1"]
        coid = self._coid()
        request = {"commandId": f"cmd-{coid}", "clientOrderId": coid, "symbol": spec["symbol"],
                   "direction": direction, "offset": "OPEN", "volume": volume,
                   "priceType": "LIMIT", "limitPrice": price}
        if fee is not None:
            request["feeRate"] = fee
        cmd = Cmd(kind="submit", coid=coid, command_id=request["commandId"], request=request,
                  symbol=spec["symbol"], offset="OPEN", expect_error=expect, twin=twin)
        self._emit(round_, cmd)
        if expect is None:
            side = "long" if direction == "BUY" else "short"
            self.pos.setdefault((spec["symbol"], side), [0, 0])[0] += volume
            self.margin_used += volume * spec["margin"]
            self.history.append(cmd)
        return cmd

    def _close(self, round_: Round, spec: dict, offset: str, volume: int, *, fee=None):
        # 找一个有可平仓量的方向：SHFE 的 CLOSE 只平昨、CLOSETODAY 只平今；其他交易所 CLOSE 今昨都平。
        # CLOSETODAY 只对上期所/能源中心合法（引擎门口校验），其他交易所不生成。
        if offset == "CLOSETODAY" and not spec["symbol"].startswith(("SHFE", "INE")):
            return None
        for side in ("long", "short"):
            today, his = self._vol(spec["symbol"], side)
            available = (his if offset == "CLOSE" else today) if spec["symbol"].startswith("SHFE") \
                else his + today
            if available > 0:
                break
        else:
            return None
        real = min(volume, available)
        quote = round_.quote_of(spec["symbol"])
        direction = "SELL" if side == "long" else "BUY"
        price = quote["bid_price1"] if direction == "SELL" else quote["ask_price1"]
        coid = self._coid()
        request = {"commandId": f"cmd-{coid}", "clientOrderId": coid, "symbol": spec["symbol"],
                   "direction": direction, "offset": offset, "volume": real,
                   "priceType": "LIMIT", "limitPrice": price}
        if fee is not None:
            request["feeRate"] = fee
        self._emit(round_, Cmd(kind="submit", coid=coid, command_id=request["commandId"],
                               request=request, symbol=spec["symbol"], offset=offset))
        cell = self.pos[(spec["symbol"], side)]
        if spec["symbol"].startswith("SHFE"):
            cell[1 if offset == "CLOSE" else 0] -= real
        else:  # 非上期所 CLOSE 按先昨后今的顺序扣减，与 SimTrade 冻结顺序一致
            his_take = min(real, his)
            cell[1] -= his_take
            cell[0] -= real - his_take
        self.margin_used -= real * spec["margin"]
        return None

    def _rest_pair(self, round_: Round, spec: dict) -> None:
        # 挂单用远离盘口但在涨跌停内、合最小变动价位的价，并在同一轮里撤掉。
        quote = round_.quote_of(spec["symbol"])
        direction = self.rng.choice(("BUY", "SELL"))
        price = (quote["bid_price1"] - 5 * spec["tick"] if direction == "BUY"
                 else quote["ask_price1"] + 5 * spec["tick"])
        if not spec["lower"] <= price <= spec["upper"]:
            return
        coid = self._coid()
        request = {"commandId": f"cmd-{coid}", "clientOrderId": coid, "symbol": spec["symbol"],
                   "direction": direction, "offset": "OPEN", "volume": 1,
                   "priceType": "LIMIT", "limitPrice": price}
        self._emit(round_, Cmd(kind="submit", coid=coid, command_id=request["commandId"],
                               request=request, symbol=spec["symbol"], offset="OPEN", resting=True))
        self._emit(round_, Cmd(kind="cancel", coid=coid, command_id=f"cmd-{coid}-xc"))

    def _duplicates(self, round_: Round, count: int) -> None:
        # 原样重发（同 commandId+clientOrderId）：只挑已即时成交的 submit，可重复挑同一笔。
        for _ in range(count):
            if not self.history:
                break
            cmd = self.rng.choice(self.history)
            self._emit(round_, Cmd(kind="submit", coid=cmd.coid, command_id=cmd.command_id,
                                   request=cmd.request, symbol=cmd.symbol, offset=cmd.offset,
                                   duplicate=True))

    def _rollover(self) -> None:
        for cell in self.pos.values():
            cell[1] += cell[0]
            cell[0] = 0

    def _extra(self, round_: Round) -> None:
        options = ["open"]
        held = [s for s in self.trade_specs
                for side in ("long", "short") if sum(self._vol(s["symbol"], side)) > 0]
        shfe_today = [s for s in self.trade_specs if s["symbol"].startswith("SHFE")
                      for side in ("long", "short") if self._vol(s["symbol"], side)[0] > 0]
        if shfe_today:
            options.append("closetoday")
        if held:
            options.append("close")
        pick = self.rng.choice(options)
        if pick == "open":
            spec = self.rng.choice(self.trade_specs)
            fee = spec["rate"] if spec["rate"] and self.rng.random() < 0.5 else None
            self._open(round_, spec, 1, fee=fee)
        elif pick == "closetoday":
            self._close(round_, self.rng.choice(shfe_today), "CLOSETODAY", 1)
        else:
            self._close(round_, self.rng.choice(held), "CLOSE", 1)

    def act(self, round_: Round) -> None:
        # 角色按 index%5 分布：0=低余额+挂撤单，1=低余额，2=平今+费率平仓+并发同发，
        # 3=平昨+并发同发，4=费率开仓；低余额账户只做 rb，IC 仅用来触发资金不足拒单。
        tag, idx = round_.tag, self.meta.index % 5
        if tag == "open1":
            if self.meta.low:
                self._open(round_, self.rb, 1, fee=RB_RATE if idx == 0 else None)
                self._open(round_, self.ic, 2, expect="insufficient")
                if idx == 0:
                    self._rest_pair(round_, self.rb)
            else:
                self._open(round_, self.rb, 2, fee=RB_RATE if self.meta.index % 2 == 0 else None)
                self._open(round_, self.ic, 1, fee=IC_RATE if self.meta.index % 2 == 1 else None)
                if idx == 2:
                    self._close(round_, self.rb, "CLOSETODAY", 1)
                if idx == 3:
                    self._open(round_, self.au, 1)
            self._duplicates(round_, 2)
        elif tag == "lunch":
            if self.meta.low:  # 午间休市下单应被拒
                self._open(round_, self.rb, 1, expect="closed")
        elif tag == "close":
            # 收盘后一笔休市拒单：拒单同样走落盘事务，把收盘行情标记后的账本持久化，
            # 重启前后读数才可比（引擎设计：行情驱动的浮动盈亏不单独落盘）。
            self._open(round_, self.rb, 1, expect="closed")
            self._rollover()
        elif tag == "day2":
            if idx in (2, 3):
                self._close(round_, self.rb, "CLOSE", 1)  # 结算后的昨仓用 CLOSE 平昨
                self._open(round_, self.rb, 1, twin=True)
                if idx == 2:
                    self._close(round_, self.ic, "CLOSE", 1, fee=IC_RATE)
            elif not self.meta.low:
                self._open(round_, self.rb, 1)
        elif tag == "extra":  # 每个 extra 轮 3 个随机动作，完整版每账户凑够 20 笔以上
            for _ in range(3):
                self._extra(round_)


def _scenario_counts(rounds: list[Round]) -> dict[str, int]:
    counts = {"平今": 0, "平昨": 0, "撤单": 0, "费率成交": 0, "休市拒单": 0, "资金不足拒单": 0}
    for round_ in rounds:
        for cmds in round_.commands.values():
            for cmd in cmds:
                if cmd.kind == "cancel":
                    counts["撤单"] += 1
                elif cmd.expect_error == "closed":
                    counts["休市拒单"] += 1
                elif cmd.expect_error == "insufficient":
                    counts["资金不足拒单"] += 1
                elif cmd.offset == "CLOSETODAY":
                    counts["平今"] += 1
                elif cmd.offset == "CLOSE" and cmd.symbol.startswith("SHFE"):
                    counts["平昨"] += 1
                if (cmd.kind == "submit" and cmd.request.get("feeRate")
                        and cmd.expect_error is None and not cmd.resting):
                    counts["费率成交"] += 1
    return counts


def build_script(account_count: int, full: bool) -> Script:
    specs = SYMBOL_SPECS + ([DCE_SPEC] if full else [])
    plan = sorted(PLAN + (FULL_EXTRA if full else []), key=lambda item: (item[0], item[1]))
    rounds: list[Round] = []
    settle_values: dict[str, float] = {}
    for index, (day, hhmm, tag) in enumerate(plan):
        quotes = []
        for spec in specs:
            settlement = pre = None
            if tag == "close":
                # 结算价取收盘行情最新价：重启路径丢失内存收盘价后按 pre_settlement 补结算，
                # 两条路径结算金额相同，重放才可比。
                settlement = _spec_price(spec, index)
                settle_values[spec["symbol"]] = settlement
            if day == DAY2:
                pre = settle_values.get(spec["symbol"])
            quotes.append(_make_quote(spec, f"{day} {hhmm}:00.000000", index, settlement, pre))
        # 早 1 秒的旧行情：重放里在每笔指令前补发它唤醒 actor，引擎按乱序整笔丢弃。
        earlier = (datetime.fromisoformat(f"{day}T{hhmm}:00+08:00")
                   - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S.%f")
        stale = {**quotes[0], "datetime": earlier}
        rounds.append(Round(moment=f"{day} {hhmm}", tag=tag, quotes=quotes, stale=stale))
    restart_after = next(i for i, item in enumerate(plan) if item[2] == "close")
    accounts = [AccountSpec(i, f"paper:{i:012x}", 100_000.0 if i < 2 else 1_000_000.0)
                for i in range(account_count)]
    traders = [Trader(meta, specs) for meta in accounts]
    for round_ in rounds:
        for trader in traders:
            trader.act(round_)
    return Script(rounds=rounds, accounts=accounts, restart_after=restart_after,
                  counts=_scenario_counts(rounds))


def _http(port: int, path: str, *, method: str = "GET", payload: dict | None = None,
          timeout: float = 10.0) -> dict:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers=headers, method=method, data=data)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


# -- 结果归一化与对照 ---------------------------------------------------------

ACCOUNT_NUM = ("equity", "staticBalance", "todayPnl", "todayNetPnl", "floatProfit",
               "positionProfit", "closeProfit", "commission", "available", "margin",
               "riskRatio", "marginRatio")
ACCOUNT_EXACT = ("tradingDay", "positionCount")
POSITION_NUM = ("openPrice", "margin", "unrealizedPnl")
POSITION_EXACT = ("symbol", "direction", "volume", "todayVolume", "historicalVolume",
                  "frozenToday", "frozenHistorical")
ORDER_NUM = ("limitPrice",)
ORDER_EXACT = ("clientOrderId", "symbol", "direction", "offset", "volume", "volumeLeft",
               "filledVolume", "priceType", "status", "insertDateTime", "lastMsg",
               "isError", "isOnline", "isDead")
TRADE_NUM = ("price",)
TRADE_EXACT = ("tradeId", "clientOrderId", "exchangeTradeId", "symbol", "direction", "offset",
               "volume", "tradeDateTime", "tradingDay")
QUERY_NUM = ("limitPrice",)
QUERY_EXACT = ("clientOrderId", "symbol", "direction", "offset", "volume", "volumeLeft",
               "filledVolume", "priceType", "status", "isError", "lastMsg")


def _pick(row: dict, keys: tuple[str, ...]) -> dict:
    return {key: row.get(key) for key in keys}


def _norm_queries(queries: dict[str, dict]) -> dict[str, dict]:
    result = {}
    for coid, report in queries.items():
        result[coid] = _pick(report["order"], QUERY_EXACT + QUERY_NUM) if report.get("found") else \
            {"found": False, "clientOrderId": coid}
    return result


def _normalize(account: dict, positions: list, activity: dict, queries: dict[str, dict]) -> dict:
    return {
        "account": _pick(account, ACCOUNT_EXACT + ACCOUNT_NUM),
        "positions": sorted((_pick(p, POSITION_EXACT + POSITION_NUM) for p in positions),
                            key=lambda row: (row["symbol"], row["direction"])),
        "orders": sorted((_pick(o, ORDER_EXACT + ORDER_NUM) for o in activity["orders"]),
                         key=lambda row: row["clientOrderId"]),
        "trades": sorted((_pick(t, TRADE_EXACT + TRADE_NUM) for t in activity["trades"]),
                         key=lambda row: (row["clientOrderId"], row["tradeId"])),
        "queries": _norm_queries(queries),
    }


def _cmp_item(label: str, left: dict, right: dict, num_keys: tuple[str, ...],
              exact_keys: tuple[str, ...], problems: list[str]) -> None:
    for key in exact_keys:
        if left.get(key) != right.get(key):
            problems.append(f"{label}.{key}: {left.get(key)!r} != {right.get(key)!r}")
    for key in num_keys:
        a, b = float(left.get(key) or 0.0), float(right.get(key) or 0.0)
        if abs(a - b) > TOL:
            problems.append(f"{label}.{key}: {a} != {b}")


def _cmp_rows(label: str, left: list[dict], right: list[dict], sort_key, num_keys, exact_keys,
              problems: list[str]) -> None:
    left, right = sorted(left, key=sort_key), sorted(right, key=sort_key)
    if len(left) != len(right):
        problems.append(f"{label}: 行数 {len(left)} != {len(right)}")
        return
    for a, b in zip(left, right):
        _cmp_item(f"{label}[{a.get('clientOrderId', a.get('symbol'))}]", a, b, num_keys,
                  exact_keys, problems)


def _cmp_snapshot(label: str, left: dict, right: dict, problems: list[str]) -> None:
    _cmp_item(f"{label}.account", left["account"], right["account"], ACCOUNT_NUM, ACCOUNT_EXACT,
              problems)
    _cmp_rows(f"{label}.positions", left["positions"], right["positions"],
              lambda row: (row["symbol"], row["direction"]), POSITION_NUM, POSITION_EXACT, problems)
    _cmp_rows(f"{label}.orders", left["orders"], right["orders"],
              lambda row: row["clientOrderId"], ORDER_NUM, ORDER_EXACT, problems)
    _cmp_rows(f"{label}.trades", left["trades"], right["trades"],
              lambda row: (row["clientOrderId"], row["tradeId"]), TRADE_NUM, TRADE_EXACT, problems)
    for coid in sorted(set(left["queries"]) | set(right["queries"])):
        _cmp_item(f"{label}.query[{coid}]", left["queries"].get(coid, {}),
                  right["queries"].get(coid, {}), QUERY_NUM, QUERY_EXACT, problems)


def _pct(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))] * 1000.0


# -- 压测主流程 ---------------------------------------------------------------

EXPECT_MESSAGE = {"closed": "不在可交易时间段内", "insufficient": "资金不足"}


def _run_load(path: str, account_count: int, full: bool, tmp_path, fill: str = "ideal") -> None:
    fill_model = FILL_MODELS[fill]
    script = build_script(account_count, full)
    for name, value in script.counts.items():
        assert value > 0, f"生成器未覆盖场景: {name}"
    command_total = sum(len(cmds) for r in script.rounds for cmds in r.commands.values())
    if full:
        per_account = [sum(len(r.commands.get(i, [])) for r in script.rounds) for i in range(account_count)]
        assert min(per_account) >= 20, f"完整版每账户指令不足 20 笔: {min(per_account)}"
    print(f"[paper-load] path={path} fill={fill} accounts={account_count} 脚本指令={command_total} "
          f"场景={script.counts}")

    db_path = str(tmp_path / f"paper-{path}-{account_count}.sqlite3")
    engines = [PaperEngine(db_path=db_path, clock=VirtualClock(START), trading_dates=TRADING_DATES,
                           fill_model=fill_model)]
    engines[0].open()
    engines[0].start()
    servers = [None]
    server_threads = [None]
    ports = [0]
    errors: list[str] = []
    latencies: list[float] = []

    def start_server() -> None:
        servers[0] = PaperServer(("127.0.0.1", 0), TOKEN, engines[0])
        server_threads[0] = threading.Thread(
            target=servers[0].serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        server_threads[0].start()
        ports[0] = int(servers[0].server_address[1])

    def stop_server() -> None:
        if servers[0] is not None:
            servers[0].shutdown()
            servers[0].server_close()
            server_threads[0].join(timeout=5)
            servers[0] = None

    def create_accounts() -> None:
        for meta in script.accounts:
            if path == "direct":
                engines[0].create_account(meta.account_id, meta.init_balance)
            else:
                _http(ports[0], "/accounts", method="POST",
                      payload={"accountId": meta.account_id, "initBalance": meta.init_balance})

    def read_snapshot(index: int, coids: list[str] | None = None) -> dict:
        coids = coids or []
        meta = script.accounts[index]
        if path == "direct":
            engine = engines[0]
            return _normalize(
                engine.account(meta.account_id), engine.positions(meta.account_id),
                engine.activity(meta.account_id),
                {c: engine.query_order(meta.account_id, c) for c in coids})
        base = f"/accounts/{meta.account_id}/v1"
        return _normalize(
            _http(ports[0], f"{base}/account"), _http(ports[0], f"{base}/positions"),
            _http(ports[0], f"{base}/activity"),
            {c: _http(ports[0], f"{base}/simulation/orders/{c}") for c in coids})

    def read_all() -> dict[int, dict]:
        # 重启前后读全部账户：http 路径用 /v1/snapshots 一次批量取 status/account/
        # positions（与单账户端点同形状），每账户再读一次 activity。
        if path == "direct":
            return {i: read_snapshot(i) for i in range(account_count)}
        batch = _http(ports[0], "/v1/snapshots", method="POST",
                      payload={"accountIds": [meta.account_id for meta in script.accounts]})
        result = {}
        for i, meta in enumerate(script.accounts):
            entry = batch["accounts"][meta.account_id]
            assert "error" not in entry, f"snapshots 返回错误: {entry}"
            result[i] = _normalize(entry["account"], entry["positions"],
                                   _http(ports[0], f"/accounts/{meta.account_id}/v1/activity"), {})
        return result

    def dup_fingerprint(index: int):
        # 幂等重发检查用的轻量指纹（一次 activity 读取）：委托数、成交笔数、
        # 委托累计成交量——重复成交三者任一都会变。
        meta = script.accounts[index]
        if path == "direct":
            activity = engines[0].activity(meta.account_id)
        else:
            activity = _http(ports[0], f"/accounts/{meta.account_id}/v1/activity")
        return (len(activity["orders"]), len(activity["trades"]),
                sum(int(order.get("filledVolume") or 0) for order in activity["orders"]))

    def state_summary(index: int):
        # 并发同发检查用的状态：权益、手续费、持仓数、成交笔数、各仓位手数。
        meta = script.accounts[index]
        if path == "direct":
            engine = engines[0]
            account = engine.account(meta.account_id)
            positions = engine.positions(meta.account_id)
            trades = engine.activity(meta.account_id)["trades"]
        else:
            base = f"/accounts/{meta.account_id}/v1"
            account = _http(ports[0], f"{base}/account")
            positions = _http(ports[0], f"{base}/positions")
            trades = _http(ports[0], f"{base}/activity")["trades"]
        return (account["equity"], account["commission"], account["positionCount"], len(trades),
                {(p["symbol"], p["direction"]): p["volume"] for p in positions})

    def wake(round_: Round) -> None:
        # 补发早 1 秒的旧行情唤醒 actor：引擎按乱序整笔丢弃（527-12），不带来任何
        # 新价格信息，只把 actor 从 50ms 空转轮询里拉回在岗状态。生产里行情持续
        # 到达，actor 常态在岗——压测的时延数字应测并发撮合负载，而不是空闲轮询
        # 等待。一轮之内依然没有任何新行情生效。发两条：http 路径的命令要经一次
        # HTTP 往返才进队列，只发一条 actor 可能在命令入队前又睡回去。
        engines[0].feed_quote(round_.stale)
        engines[0].feed_quote(round_.stale)

    def send_submit(index: int, request: dict) -> dict:
        meta = script.accounts[index]
        started = time.perf_counter()
        if path == "direct":
            result = engines[0].submit(meta.account_id, request)
        else:
            result = _http(ports[0], f"/accounts/{meta.account_id}/v1/simulation/orders/submit",
                           method="POST", payload=request)
        latencies.append(time.perf_counter() - started)
        return result

    def send_cancel(index: int, cmd: Cmd) -> dict:
        meta = script.accounts[index]
        started = time.perf_counter()
        if path == "direct":
            result = engines[0].cancel_order(meta.account_id, cmd.coid, cmd.command_id)
        else:
            result = _http(ports[0],
                           f"/accounts/{meta.account_id}/v1/simulation/orders/{cmd.coid}/cancel",
                           method="POST", payload={"commandId": cmd.command_id})
        latencies.append(time.perf_counter() - started)
        return result

    def check_submit(index: int, cmd: Cmd, report: dict) -> None:
        order = report.get("order") if isinstance(report, dict) else None
        if not report.get("found") or not order:
            errors.append(f"a{index} {cmd.coid}: submit 未返回委托: {str(report)[:200]}")
            return
        if cmd.expect_error:
            if not order.get("isError") or EXPECT_MESSAGE[cmd.expect_error] not in str(order.get("lastMsg")):
                errors.append(f"a{index} {cmd.coid}: 预期拒单({cmd.expect_error}) 实际 "
                              f"{order.get('status')}/{order.get('lastMsg')}")
        elif order.get("isError"):
            errors.append(f"a{index} {cmd.coid}: 意外错单: {order.get('lastMsg')}")

    def execute(index: int, cmd: Cmd, round_: Round) -> None:
        try:
            if cmd.kind == "cancel":
                wake(round_)
                report = send_cancel(index, cmd)
                order = report.get("order") or {}
                if order.get("isError", True) or order.get("status") != "FINISHED":
                    errors.append(f"a{index} 撤单 {cmd.coid}: {order.get('status')}/"
                                  f"{order.get('lastMsg')}")
                return
            if cmd.duplicate:
                before = dup_fingerprint(index)
                wake(round_)
                report = send_submit(index, cmd.request)
                order = report.get("order") or {}
                if order.get("clientOrderId") != cmd.coid:
                    errors.append(f"a{index} 幂等重发 {cmd.coid}: 返回 "
                                  f"{order.get('clientOrderId')}")
                if dup_fingerprint(index) != before:
                    errors.append(f"a{index} 幂等重发 {cmd.coid}: 状态变化（重复成交）")
                return
            if cmd.twin:
                # 同一轮里两个线程对同一账户并发发同一条指令：只能成交一次。
                before = state_summary(index)
                gate = threading.Barrier(2, timeout=15)
                outcome: dict[str, dict] = {}

                def twin_send() -> None:
                    try:
                        gate.wait()
                        wake(round_)
                        outcome["helper"] = send_submit(index, cmd.request)
                    except Exception as error:
                        errors.append(f"a{index} 并发同发 {cmd.coid}: {error!r}")

                helper = threading.Thread(target=twin_send, daemon=True)
                helper.start()
                gate.wait()
                wake(round_)
                outcome["main"] = send_submit(index, cmd.request)
                helper.join(timeout=15)
                for who in ("main", "helper"):
                    order = (outcome.get(who) or {}).get("order") or {}
                    if order.get("clientOrderId") != cmd.coid or order.get("isError"):
                        errors.append(f"a{index} 并发同发 {cmd.coid} {who}: {order}")
                after = state_summary(index)
                key = (cmd.symbol, "long" if cmd.request["direction"] == "BUY" else "short")
                if after[4].get(key, 0) - before[4].get(key, 0) != cmd.request["volume"]:
                    errors.append(f"a{index} 并发同发 {cmd.coid}: 持仓变化异常 "
                                  f"{before[4]} -> {after[4]}")
                return
            wake(round_)
            report = send_submit(index, cmd.request)
            check_submit(index, cmd, report)
            if cmd.resting and (report.get("order") or {}).get("status") != "ALIVE":
                errors.append(f"a{index} 挂单 {cmd.coid} 未存活: "
                              f"{(report.get('order') or {}).get('status')}")
        except Exception as error:
            errors.append(f"a{index} {cmd.coid}: {type(error).__name__}: {error}")

    barrier_timeout = 120.0 if full else 30.0
    start_gate = threading.Barrier(account_count + 1, timeout=barrier_timeout)
    end_gate = threading.Barrier(account_count + 1, timeout=barrier_timeout)

    def worker(index: int) -> None:
        try:
            for round_ in script.rounds:
                start_gate.wait()
                for cmd in round_.commands.get(index, []):
                    execute(index, cmd, round_)
                end_gate.wait()
        except Exception as error:  # BrokenBarrier 等致命问题：记录后退出，不让测试挂死
            errors.append(f"worker a{index}: {type(error).__name__}: {error}")

    workers = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(account_count)]
    checkpoint_reads: dict[int, dict] = {}
    final_reads: dict[int, dict] = {}
    try:
        if path == "http":
            start_server()
        create_accounts()
        for thread_ in workers:
            thread_.start()
        started = time.perf_counter()
        for r_index, round_ in enumerate(script.rounds):
            # 每轮：主线程喂各合约一条行情并排空，随后所有账户在 Barrier 后同时发本轮指令，
            # 全部线程到齐（end_gate）才进下一轮；一轮之内不喂行情。
            for quote in round_.quotes:
                engines[0].feed_quote(quote)
            assert engines[0].drain(timeout=60.0 if full else 10.0), f"行情排空超时 {round_.moment}"
            start_gate.wait()
            end_gate.wait()
            if r_index == script.restart_after:
                # 第 1 天收盘结算之后、第 2 天第一轮之前重启引擎，账本必须逐项相等
                # （任务口径：account/positions/activity；跨日委托可见性由最终读取的
                # query_order 全量比对覆盖）。
                pre = read_all()
                stop_server()
                engines[0].close()
                engines[0] = PaperEngine(
                    db_path=db_path,
                    clock=VirtualClock(datetime.fromisoformat("2026-11-03T08:59:00+08:00")),
                    trading_dates=TRADING_DATES, fill_model=fill_model)
                engines[0].open()
                engines[0].start()
                if path == "http":
                    start_server()
                post = read_all()
                problems = []
                for i in range(account_count):
                    _cmp_snapshot(f"重启前后 a{i}", pre[i], post[i], problems)
                assert not problems, "\n".join(problems[:15])
                checkpoint_reads = pre
                # 重启丢掉了内存里的行情缓存与结算价记录，第 2 天的结算依赖第 2 天
                # 行情携带的 pre_settlement（2026-10-11 起不再有收盘价兜底）。生产里
                # 重启后行情源会重发各合约最新快照（TqQuoteSource 首连行为），这里同样
                # 补喂收盘快照（同时刻重复行情：价格相同、不撮合，仅重建缓存），使两条
                # 路径一致。
                for quote in script.rounds[r_index].quotes:
                    engines[0].feed_quote(quote)
                assert engines[0].drain(timeout=60.0 if full else 10.0), "收盘快照补喂排空超时"
        for thread_ in workers:
            thread_.join(timeout=barrier_timeout)
        elapsed = time.perf_counter() - started
        alive = [t for t in workers if t.is_alive()]
        assert not alive, f"{len(alive)} 个 worker 未退出"
        # 最终读取只对第 1 天的委托做 query_order 全量比对：跨日之后内存归档按天
        # 裁剪，这些委托在 activity 里不可见，只能查持久层；第 2 天的委托由
        # activity orders 直接覆盖。
        final_reads = {i: read_snapshot(i, script.coids_upto(i, script.restart_after))
                       for i in range(account_count)}
    finally:
        stop_server()
        engines[0].close()

    assert latencies, "没有任何指令发出"
    assert not errors, "\n".join(errors[:15])
    throughput = len(latencies) / elapsed if elapsed else 0.0
    print(f"[paper-load] path={path} accounts={account_count} 实发={len(latencies)} "
          f"并发阶段={elapsed:.2f}s 吞吐={throughput:.0f}笔/秒 "
          f"p50={_pct(latencies, 0.5):.2f}ms p95={_pct(latencies, 0.95):.2f}ms "
          f"max={_pct(latencies, 1.0):.2f}ms")

    # -- 对照：每个账户在独立引擎里按同样轮次单独串行重放（直接调用、不重启） ---------
    # 引擎 actor 空闲时以 50ms 轮询等行情，命令不唤醒它：重放在每笔指令前补发一条
    # 早 1 秒的旧行情唤醒 actor——引擎按乱序整笔丢弃（527-12）、VirtualClock 只前进，
    # 不改变任何状态，主跑与重放的结果仍必须逐项相等。各账户引擎互相独立，重放
    # 线程之间无共享；每个账户内部仍是串行重放。
    replay_checkpoints: dict[int, dict] = {}
    replay_finals: dict[int, dict] = {}
    replay_errors: list[str] = []

    def replay_one(index: int) -> None:
        meta = script.accounts[index]
        try:
            engine = PaperEngine(db_path=str(tmp_path / f"replay-{path}-{index}.sqlite3"),
                                 clock=VirtualClock(START), trading_dates=TRADING_DATES,
                                 fill_model=fill_model)
            engine.open()
            engine.start()
            engine.create_account(meta.account_id, meta.init_balance)
            try:
                checkpoint = None
                for r_index, round_ in enumerate(script.rounds):
                    for quote in round_.quotes:
                        engine.feed_quote(quote)
                    assert engine.drain(timeout=60.0 if full else 10.0), "重放行情排空超时"
                    for cmd in round_.commands.get(index, []):
                        engine.feed_quote(round_.stale)
                        if cmd.kind == "cancel":
                            engine.cancel_order(meta.account_id, cmd.coid, cmd.command_id)
                        else:
                            engine.submit(meta.account_id, cmd.request)
                            if cmd.duplicate or cmd.twin:  # 幂等重发/并发同发按两次串行发送
                                engine.feed_quote(round_.stale)
                                engine.submit(meta.account_id, cmd.request)
                    if r_index == script.restart_after:
                        checkpoint = _normalize(
                            engine.account(meta.account_id), engine.positions(meta.account_id),
                            engine.activity(meta.account_id), {})
                replay_checkpoints[index] = checkpoint
                replay_finals[index] = _normalize(
                    engine.account(meta.account_id), engine.positions(meta.account_id),
                    engine.activity(meta.account_id),
                    {c: engine.query_order(meta.account_id, c)
                     for c in script.coids_upto(index, script.restart_after)})
            finally:
                engine.close()
        except Exception as error:
            replay_errors.append(f"a{index}: {type(error).__name__}: {error}")

    replay_workers = [threading.Thread(target=replay_one, args=(i,), daemon=True)
                      for i in range(account_count)]
    for thread_ in replay_workers:
        thread_.start()
    for thread_ in replay_workers:
        thread_.join(timeout=120.0 if full else 30.0)
    assert not any(thread_.is_alive() for thread_ in replay_workers), "重放线程未退出"
    assert not replay_errors, "\n".join(replay_errors[:15])

    problems: list[str] = []
    for index in range(account_count):
        _cmp_snapshot(f"对照 a{index}/第1天末", checkpoint_reads[index],
                      replay_checkpoints[index], problems)
        _cmp_snapshot(f"对照 a{index}/最终", final_reads[index], replay_finals[index], problems)

    # 串户检查：本账户读到的委托号必须都属于本账户；query 不得返回别人的委托。
    for index in range(account_count):
        prefix = f"a{index:03d}-"
        for label, snapshot in (("最终", final_reads[index]), ("第1天末", checkpoint_reads[index])):
            for rows, what in ((snapshot["orders"], "orders"), (snapshot["trades"], "trades")):
                for row in rows:
                    if not str(row.get("clientOrderId", "")).startswith(prefix):
                        problems.append(f"a{index} {label}{what} 出现外部委托 "
                                        f"{row.get('clientOrderId')}")
            for coid, order in snapshot["queries"].items():
                if order.get("clientOrderId") not in (None, coid):
                    problems.append(f"a{index} query {coid} 返回 {order.get('clientOrderId')}")

    # 错单集合必须恰好等于预期拒单（休市轮、资金不足账户）：第 1 天的查持久层，
    # 第 2 天的看 activity orders。
    expected_errors = {cmd.coid for r in script.rounds for cmds in r.commands.values()
                       for cmd in cmds if cmd.kind == "submit" and cmd.expect_error}
    observed_errors = {coid for index in range(account_count)
                       for coid, order in final_reads[index]["queries"].items()
                       if order.get("isError")}
    observed_errors |= {str(row.get("clientOrderId")) for index in range(account_count)
                        for row in final_reads[index]["orders"] if row.get("isError")}
    if observed_errors != expected_errors:
        problems.append(f"错单集合不符: 多出 {sorted(observed_errors - expected_errors)} "
                        f"缺少 {sorted(expected_errors - observed_errors)}")
    assert not problems, "\n".join(problems[:15])


@pytest.mark.parametrize("fill", sorted(FILL_MODELS))
@pytest.mark.parametrize("path", ["direct", "http"])
def test_paper_multi_account_concurrent_small(path, fill, tmp_path):
    _run_load(path, 5, full=False, tmp_path=tmp_path, fill=fill)


@pytest.mark.skipif(not FULL, reason="PAPER_LOAD_FULL=1 时才跑完整压测（50/100 账户）")
@pytest.mark.parametrize("fill", sorted(FILL_MODELS))
@pytest.mark.parametrize("path,account_count", [
    ("direct", 50), ("direct", 100), ("direct", 200), ("http", 50), ("http", 100),
])
def test_paper_multi_account_concurrent_full(path, account_count, fill, tmp_path):
    _run_load(path, account_count, full=True, tmp_path=tmp_path, fill=fill)
