"""本站模拟撮合引擎的行情账号接力（2026-10-12）。

撮合行情原来只连引擎自己挂载的一个快期账号（secrets 里的 tqkq_username /
tqkq_password，与主 sidecar 同一组）。这个账号会话或口令出问题，所有本站模拟
账户 60 秒后就没有行情、不能下单；新浪备用数据不能用于撮合。

现在引擎按一份有序账号清单接力，同一时刻只用一个账号：

- 清单与顺序的唯一来源是 Runtime 的公共行情源清单 `market_sources`。Runtime
  （租约持有者）经引擎已有的 Bearer 鉴权回环接口 `POST /v1/market-sources`
  下发：账户池账号带它在 Runtime 账户库里加密保存的快期用户名/口令（与 Runtime
  向账户池起子进程同一条通道）；主 sidecar 账号只标 `local`，用引擎自己挂载的
  secrets。口令只在内存，不落盘、不进日志、不进健康输出。
- 下发之前（引擎刚启动、Runtime 未配置）清单只有 `local` 一个，即原来的行为；
  下发了空清单（管理员停掉全部行情源）就没有撮合行情源，不退回挂载账号。
- 认证失败或无权限：立即换下一个，该账号冷却 AUTH_COOLDOWN_SECONDS。
- 连接失败或运行中断线：同一账号连续失败 MAX_FAILURES 次才换（中间每次等
  `TqQuoteSource.RETRY_SECONDS`），冷却 FAILURE_COOLDOWN_SECONDS；一次会话稳定
  跑满 STABLE_SECONDS 再断，计数从头算。
- 换的时候按清单顺序取第一个不在冷却中的账号；当前账号正常时不因为排在前面的
  账号恢复而切回（与公共行情“never reclaims”一致）。当前账号被移出清单或凭据
  变了，立即重连到清单里的下一个。
- 全部账号都在冷却：没有行情连接，引擎沿用原来的“重连 60 秒后 unavailable”。

切换后的首包行情与平时一样过引擎的防倒退水位与合约资料规则（`_apply_quote`），
交易日历、结算价查询、合约资料表刷新都走当前这一个连接，跟着一起换。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

LOCAL_SOURCE_ID = "local"
AUTH_COOLDOWN_SECONDS = 600.0  # 与账户池缓存认证失败的时长一致
FAILURE_COOLDOWN_SECONDS = 60.0
MAX_FAILURES = 3
STABLE_SECONDS = 60.0
MAX_SOURCES = 16
AUTH_CODES = {"tqkq_authentication_failed", "tqkq_permission_denied", "tqkq_credentials_missing"}
_ACCOUNT_ID = re.compile(r"[A-Za-z0-9:_-]{1,120}")


@dataclass
class SourceAccount:
    account_id: str
    local: bool
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    failures: int = 0
    cooling_until: float = 0.0
    last_error: str | None = None
    last_error_at: str | None = None

    def key(self) -> tuple[str, bool, str, str]:
        return (self.account_id, self.local, self.username, self.password)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_sources(payload: Any) -> list[SourceAccount]:
    """`{"sources": [{"accountId", "local": true} | {"accountId", "username", "password"}]}`。
    格式不对一律 ValueError（HTTP 422），错误信息不带任何字段值。"""
    if not isinstance(payload, dict) or set(payload) != {"sources"}:
        raise ValueError("invalid_market_sources")
    items = payload["sources"]
    if not isinstance(items, list) or len(items) > MAX_SOURCES:
        raise ValueError("invalid_market_sources")
    result: list[SourceAccount] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid_market_source")
        account_id = item.get("accountId")
        if not isinstance(account_id, str) or _ACCOUNT_ID.fullmatch(account_id) is None or account_id in seen:
            raise ValueError("invalid_market_source")
        seen.add(account_id)
        if set(item) == {"accountId", "local"} and item["local"] is True:
            result.append(SourceAccount(account_id, True))
            continue
        username, password = item.get("username"), item.get("password")
        if (set(item) != {"accountId", "username", "password"}
                or not isinstance(username, str) or not username.strip() or len(username) > 200
                or not isinstance(password, str) or not password or len(password) > 200):
            raise ValueError("invalid_market_source")
        result.append(SourceAccount(account_id, False, username.strip(), password))
    return result


class MarketSourceRelay:
    """有序账号清单、每个账号的失败计数与冷却、当前账号和切换原因。

    行情线程（TqQuoteSource._run）调 pick / connected / failed；HTTP 线程调
    configure / status。全部状态在一把锁里。"""

    def __init__(self, local_credentials: Callable[[], tuple[str, str]], *,
                 monotonic: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._local_credentials = local_credentials
        self._monotonic = monotonic
        self._now = now
        self._lock = threading.Lock()
        self._sources: list[SourceAccount] = [SourceAccount(LOCAL_SOURCE_ID, True)]
        self._configured = False
        self._current: SourceAccount | None = None
        self._connected = False
        self._switched_at: str | None = None
        self._switch_reason: str | None = None
        self._pending_reason: str | None = None
        self._key = secrets.token_bytes(32)
        self._revision = self._digest(self._sources)
        self._changed = False
        # 行情线程等待时用它被提前唤醒（清单更新、停止）。
        self.wake = threading.Event()

    # -- 清单 -------------------------------------------------------------------

    def _digest(self, sources: list[SourceAccount]) -> str:
        """清单指纹（含口令，用进程内随机密钥做 HMAC）：内容不变就不变，引擎重启
        就变——Runtime 据此判断要不要重发，指纹本身推不出任何口令。"""
        canonical = json.dumps([list(item.key()) for item in sources], ensure_ascii=False)
        return hmac.new(self._key, canonical.encode(), hashlib.sha256).hexdigest()[:16]

    def configure(self, sources: list[SourceAccount]) -> str:
        """整份替换有序清单。同一账号凭据不变时保留它的失败计数与冷却；凭据变了
        重新算。下发的空清单就是空：管理员停掉全部行情源时撮合也没有行情源，
        不退回挂载账号（只有从未下发时才用挂载账号）。"""
        sources = list(sources)
        with self._lock:
            old = {item.account_id: item for item in self._sources}
            merged: list[SourceAccount] = []
            for item in sources:
                previous = old.get(item.account_id)
                if previous is not None and previous.key() == item.key():
                    merged.append(previous)
                else:
                    merged.append(item)
            current = self._current
            # 引擎启动时还不知道 Runtime 给主账号起的编号：本机挂载账号在新清单里
            # 的那一项接替它（同一个账号，不算切换）。
            if current is not None and current.local and current not in merged:
                index = next((i for i, item in enumerate(merged) if item.local), None)
                if index is not None:
                    current.account_id = merged[index].account_id  # 同一个连接，换个名字
                    merged[index] = current
            if current is not None and current not in merged:
                # 同一账号换了凭据：仍是它，只是要用新凭据重连（行情线程看到
                # still_current 为假就重连）。
                renewed = next((item for item in merged if item.account_id == current.account_id), None)
                if renewed is not None:
                    self._current = renewed
                    self._connected = False
                    self._changed = True
                    current = None
            if current is not None and current not in merged:
                self._pending_reason = "removed"
                self._changed = True
            self._sources = merged
            self._configured = True
            revision = self._digest(merged)
            if revision != self._revision:
                self._changed = True
            self._revision = revision
        self.wake.set()
        return revision

    def take_change(self) -> bool:
        """清单自上次询问后是否变过（行情线程据此检查当前账号还在不在）。"""
        with self._lock:
            changed, self._changed = self._changed, False
            return changed

    def still_current(self, account: SourceAccount | None) -> bool:
        with self._lock:
            return account is not None and account is self._current and account in self._sources

    # -- 选账号与结果 -------------------------------------------------------------

    def pick(self) -> SourceAccount | None:
        """接下来用哪个账号：当前账号还在清单里且未冷却就继续用它，否则按清单
        顺序取第一个不在冷却中的；全部冷却返回 None。"""
        with self._lock:
            now = self._monotonic()
            current = self._current
            if current is not None and current in self._sources and now >= current.cooling_until:
                return current
            chosen = next((item for item in self._sources if now >= item.cooling_until), None)
            if chosen is not None and chosen is not current:
                if current is not None:
                    self._switched_at = _iso(self._now())
                    self._switch_reason = self._pending_reason or "unavailable"
                self._current = chosen
                self._connected = False
            if chosen is not None:
                self._pending_reason = None  # 全部冷却时留着，等真正换上账号时再记
            return chosen

    def credentials(self, account: SourceAccount) -> tuple[str, str]:
        if account.local:
            username, password = self._local_credentials()
            return username or "", password or ""
        return account.username, account.password

    def connected(self, account: SourceAccount) -> None:
        with self._lock:
            if account is self._current:
                self._connected = True

    def failed(self, account: SourceAccount, code: str, *, connecting: bool,
               session_seconds: float | None = None) -> bool:
        """记一次失败，返回这个账号是否因此进入冷却（下一次 pick 会换账号）。"""
        with self._lock:
            account.last_error = code
            account.last_error_at = _iso(self._now())
            if account is self._current:
                self._connected = False
            if code in AUTH_CODES:
                account.failures = 0
                account.cooling_until = self._monotonic() + AUTH_COOLDOWN_SECONDS
                self._pending_reason = "permission_denied" if code == "tqkq_permission_denied" else "auth_failed"
                return True
            if session_seconds is not None and session_seconds >= STABLE_SECONDS:
                account.failures = 0
            account.failures += 1
            if account.failures < MAX_FAILURES:
                return False
            account.failures = 0
            account.cooling_until = self._monotonic() + FAILURE_COOLDOWN_SECONDS
            self._pending_reason = "connect_failed" if connecting else "disconnected"
            return True

    def next_ready_in(self) -> float:
        """全部冷却时，最早一个账号还要等多久。"""
        with self._lock:
            now = self._monotonic()
            waits = [item.cooling_until - now for item in self._sources]
            return max(0.0, min(waits)) if waits else float("inf")  # 空清单：等下一次下发

    # -- 健康输出（只有账号编号与序号，没有用户名和口令） ---------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            now_mono, now = self._monotonic(), self._now()
            current = self._current if self._current in self._sources else None
            accounts = []
            for position, item in enumerate(self._sources, start=1):
                cooling = item.cooling_until > now_mono
                state = ("cooling" if cooling else "active" if item is current and self._connected
                         else "connecting" if item is current else "standby")
                accounts.append({
                    "accountId": item.account_id,
                    "position": position,
                    "local": item.local,
                    "state": state,
                    "failures": item.failures,
                    "lastError": item.last_error,
                    "lastErrorAt": item.last_error_at,
                    "retryAt": _iso(now + timedelta(seconds=item.cooling_until - now_mono)) if cooling else None,
                })
            return {
                "current": current.account_id if current is not None else None,
                "position": self._sources.index(current) + 1 if current is not None else None,
                "total": len(self._sources),
                "connected": bool(current is not None and self._connected),
                "switchedAt": self._switched_at,
                "switchReason": self._switch_reason,
                "configured": self._configured,
                "revision": self._revision,
                "accounts": accounts,
            }

    def redact(self, text: str, account: SourceAccount | None = None) -> str:
        """日志里的报错文本去掉清单里任何账号、以及出错那个账号（可能已被移出
        清单或换了凭据）的用户名和口令。"""
        with self._lock:
            sources = list(self._sources) + ([account] if account is not None else [])
        values: set[str] = set()
        for item in sources:
            try:
                username, password = self.credentials(item)
            except Exception:
                continue
            values |= {value for value in (username, password) if value and len(value) >= 3}
        for value in sorted(values, key=len, reverse=True):
            text = text.replace(value, "***")
        return text
