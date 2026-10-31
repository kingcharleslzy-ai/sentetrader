"""输入框上方的 AI 建议问题（owner 2026-10-05）：POST /api/agent/suggestions。

0–2 句、由模型按行情/持仓/时间/对话生成；模型失败、超时、无凭据、游客一律安静
返回空数组；同键 10 分钟缓存、同一用户 60 秒最小调用间隔。全部走真实路由与
本地 token 应用，只有模型适配器是假的（挂在 app.state.adapter 上的记录桩）。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI

from trader_runtime.chat_suggestions import SuggestedQuestions, postprocess, session_phase
from trader_runtime.main import create_app

TOKEN = "chat-suggestions-token"
ORIGIN = "https://chat.example.test"


class FakeModel:
    """complete_structured 记录桩：按脚本回放，计数模型调用。"""

    agent_authenticated = True

    def __init__(self, items: list[str] | None = None, error: Exception | None = None) -> None:
        self.items = items if items is not None else ["给螺纹空单挂个止损", "螺纹为什么突然拉升"]
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def complete_structured(self, *, instructions: str, input_text: str, schema: type,
                                  timeout_seconds: float, usage_user: str | None = None,
                                  **kwargs: Any) -> Any:
        self.calls.append({"input": json.loads(input_text), "timeout": timeout_seconds,
                           "usage_user": usage_user})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(value=SuggestedQuestions(items=list(self.items)))


def suggestions_app(tmp_path: Path, database: str, model: FakeModel | None = None) -> FastAPI:
    application = create_app(tmp_path / database, local_token=TOKEN, agent_mode="fake",
                             data_mode="fake", cors_origins={ORIGIN})
    if model is not None:
        application.state.adapter.complete_structured = model.complete_structured  # type: ignore[method-assign]
        application.state.adapter.agent_authenticated = True
    return application


def client_for(application: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                             base_url="https://runtime.example.test",
                             headers={"Authorization": f"Bearer {TOKEN}", "Origin": ORIGIN})


BODY = {"view": "trade", "symbol": "SHFE.rb2610", "trigger": "open"}


async def ask(client: httpx.AsyncClient, body: dict[str, Any] | None = None) -> dict[str, Any]:
    response = await client.post("/api/agent/suggestions", json=body or BODY)
    assert response.status_code == 200
    return response.json()


async def test_returns_trimmed_unique_items_within_two(tmp_path: Path) -> None:
    model = FakeModel(items=["  给螺纹空单挂个止损  ", "给螺纹空单挂个止损",
                             "这句话明显超过二十四个汉字的限制所以应当被丢弃掉才对",
                             "复盘一下今天这三笔"])
    application = suggestions_app(tmp_path, "normal.sqlite3", model)
    try:
        async with client_for(application) as client:
            result = await ask(client)
        assert result == {"items": ["给螺纹空单挂个止损", "复盘一下今天这三笔"]}
        assert len(model.calls) == 1
        call = model.calls[0]
        # usage 归到调用者、超时约 12 秒、上下文带合约与页面。
        assert call["usage_user"] == "local-user"
        assert call["timeout"] == 12
        assert call["input"]["symbol"] == "SHFE.rb2610"
        assert call["input"]["view"] == "交易台"
        assert "now" in call["input"]
    finally:
        application.state.store.close()


async def test_model_error_and_timeout_answer_empty(tmp_path: Path) -> None:
    for index, error in enumerate((RuntimeError("model_request_failed"), TimeoutError())):
        model = FakeModel(error=error)
        application = suggestions_app(tmp_path, f"error-{index}.sqlite3", model)
        try:
            async with client_for(application) as client:
                assert await ask(client) == {"items": []}
            assert len(model.calls) == 1
        finally:
            application.state.store.close()


async def test_without_model_credentials_or_guest_no_model_call(tmp_path: Path) -> None:
    # fake 适配器本身没有 complete_structured：无凭据 → 空，且不进任何模型路径。
    application = suggestions_app(tmp_path, "nocreds.sqlite3")
    try:
        async with client_for(application) as client:
            assert await ask(client) == {"items": []}
            # 游客（Bearer guest.）同样安静为空，不需要登录。
            guest = await client.post("/api/agent/suggestions", json=BODY,
                                      headers={"Authorization": "Bearer guest.anything"})
            assert guest.status_code == 200 and guest.json() == {"items": []}
    finally:
        application.state.store.close()


async def test_same_key_cache_hit_skips_the_model(tmp_path: Path) -> None:
    model = FakeModel()
    application = suggestions_app(tmp_path, "cache.sqlite3", model)
    try:
        async with client_for(application) as client:
            first = await ask(client)
            second = await ask(client)
        assert first == second == {"items": model.items}
        assert len(model.calls) == 1  # 同键 10 分钟内直接复用
    finally:
        application.state.store.close()


async def test_within_sixty_seconds_reuses_only_the_same_scope(tmp_path: Path) -> None:
    model = FakeModel()
    application = suggestions_app(tmp_path, "interval.sqlite3", model)
    try:
        async with client_for(application) as client:
            first = await ask(client)
            # 60 秒间隔内换合约：不复用别的品种的建议，安静为空。
            other_symbol = await ask(client, {**BODY, "symbol": "DCE.i2609"})
            assert other_symbol == {"items": []}
            # 原键还在缓存里：同键照常命中，不调模型。
            again = await ask(client)
            assert again == first
            assert len(model.calls) == 1
            # 间隔过后换合约才重算。
            service = application.state.chat_suggestions
            service._last_call["local-user"] -= 61.0
            refreshed = await ask(client, {**BODY, "symbol": "DCE.i2609"})
        assert refreshed == {"items": model.items}
        assert len(model.calls) == 2
    finally:
        application.state.store.close()


async def test_a_closed_position_is_not_suggested_from_the_cache_and_cards_are_not_confirmed_by_text(
        tmp_path: Path, monkeypatch: Any) -> None:
    """#932 P2: 沪银平掉后建议条仍给它挂止盈（10 分钟缓存不看持仓）；建议“确认这张沪银卡”打字确认走不通。"""
    from trader_runtime.chat_suggestions import INSTRUCTIONS
    from trader_runtime.data_providers import FakeReadOnlyDataProvider as FakeDataProvider
    model = FakeModel()
    application = suggestions_app(tmp_path, "held.sqlite3", model)
    try:
        async with client_for(application) as client:
            await ask(client)
            application.state.chat_suggestions._last_call["local-user"] -= 61.0
            held = FakeDataProvider.positions
            monkeypatch.setattr(FakeDataProvider, "positions", lambda self: [row for row in held(self) if "ag" not in row["symbol"]])
            await ask(client)
        assert len(model.calls) == 2  # the account changed: a fresh answer, never the cached one
        assert all("ag2612" not in json.dumps(row) for row in model.calls[1]["input"]["positions"])
        assert "打字确认不生效" in INSTRUCTIONS and "最近对话里提到的持仓可能已经平掉" in INSTRUCTIONS
    finally:
        application.state.store.close()


async def test_within_sixty_seconds_another_session_or_an_expired_answer_is_not_lent_out(tmp_path: Path) -> None:
    model = FakeModel()
    application = suggestions_app(tmp_path, "sessions.sqlite3", model)
    try:
        async with client_for(application) as client:
            s1, s2 = [(await client.post("/api/sessions", json={"mode": "local"})).json()["sessionId"]
                      for _ in range(2)]
            first = await ask(client, {**BODY, "sessionId": s1})
            assert first == {"items": model.items}
            # 同账户/合约/页面的另一会话：没有它自己的新鲜结果就安静为空，不借 s-1 的。
            assert await ask(client, {**BODY, "sessionId": s2}) == {"items": []}
            assert len(model.calls) == 1
            # s-1 的结果过了 10 分钟：限频期内也不把过期结果翻出来。
            service = application.state.chat_suggestions
            for key, (at, items) in list(service._cache.items()):
                service._cache[key] = (at - 601.0, items)
            assert await ask(client, {**BODY, "sessionId": s1}) == {"items": []}
        assert len(model.calls) == 1
    finally:
        application.state.store.close()


async def test_the_session_must_be_the_callers_and_on_the_account_answered_for(tmp_path: Path) -> None:
    """#932 X1：sessionId 与账户头绑定——会话不是调用者的、或会话绑在别的账户上（对话与待答卡
    属于另一账户），安静为空、不调模型；会话自己的账户照常出建议。"""
    model = FakeModel()
    application = suggestions_app(tmp_path, "bound.sqlite3", model)
    try:
        async with client_for(application) as client:
            session_id = (await client.post("/api/sessions", json={"mode": "local"})).json()["sessionId"]
            default = application.state.accounts.default.account_id
            header = {"X-AI-Trader-Account": default}
            assert await ask(client, {**BODY, "sessionId": "not-a-session"}) == {"items": []}
            application.state.store.set_session_trading_account(session_id, "another-account", user_id="local-user")
            response = await client.post("/api/agent/suggestions", json={**BODY, "sessionId": session_id},
                                         headers=header)
            assert response.status_code == 200 and response.json() == {"items": []}
            assert model.calls == []
            application.state.store.set_session_trading_account(session_id, default, user_id="local-user")
            response = await client.post("/api/agent/suggestions", json={**BODY, "sessionId": session_id},
                                         headers=header)
            assert response.json() == {"items": model.items}
        assert len(model.calls) == 1
    finally:
        application.state.store.close()


async def test_concurrent_requests_for_one_key_share_one_model_call(tmp_path: Path) -> None:
    """#932 X1：同参数的请求在模型调用返回前到达（打开面板与一轮回答结束几乎同时），
    等同一次调用的结果，不另起一次、也不被 60 秒限频挡成空。"""
    model = FakeModel()
    release = asyncio.Event()
    answer = model.complete_structured

    async def slow(**kwargs: Any) -> Any:
        await release.wait()
        return await answer(**kwargs)

    application = suggestions_app(tmp_path, "inflight.sqlite3", model)
    application.state.adapter.complete_structured = slow  # type: ignore[method-assign]
    try:
        async with client_for(application) as client:
            pending = [asyncio.create_task(ask(client)) for _ in range(3)]
            await asyncio.sleep(0.05)
            release.set()
            results = await asyncio.gather(*pending)
        assert results == [{"items": model.items}] * 3
        assert len(model.calls) == 1
        assert application.state.chat_suggestions._inflight == {}
    finally:
        application.state.store.close()


def test_session_phase_covers_the_trading_day() -> None:
    beijing = timezone(timedelta(hours=8))
    schedule = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]],
                "night": [["21:00:00", "23:00:00"]]}
    assert session_phase(schedule, datetime(2026, 10, 5, 10, 0, tzinfo=beijing)) == "盘中"
    assert session_phase(schedule, datetime(2026, 10, 5, 10, 45, tzinfo=beijing)) == "盘中"  # 第二节内
    assert session_phase(schedule, datetime(2026, 10, 5, 12, 0, tzinfo=beijing)) == "午休"
    assert session_phase(schedule, datetime(2026, 10, 5, 8, 30, tzinfo=beijing)) == "盘前"
    assert session_phase(schedule, datetime(2026, 10, 5, 16, 0, tzinfo=beijing)) == "收盘后"
    assert session_phase(schedule, datetime(2026, 10, 5, 22, 0, tzinfo=beijing)) == "夜盘"
    assert session_phase(schedule, datetime(2026, 10, 3, 10, 0, tzinfo=beijing)) == "休市"  # 周六
    assert session_phase(None, datetime(2026, 10, 5, 10, 0, tzinfo=beijing)) == "未知"


def test_postprocess_trims_dedupes_and_caps() -> None:
    assert postprocess([" a ", "a", "x" * 25, "", "b", "c"]) == ["a", "b"]
    assert postprocess([]) == []
