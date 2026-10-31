import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from trader_runtime.strategy_explanations import (
    ExplanationRequest,
    ExplanationResult,
    StrategyExplanations,
    collect_explanation,
    resolve_evidence,
)


def _request():
    return Request({"type": "http", "method": "POST",
                    "path": "/api/app/strategy-explanations", "headers": []})


def snapshot():
    return {"environment":"replay","replayId":"historical", "orders":[], "strategies":[], "protections":[],
            "fills":[{"tradeId":"fill-1","clientOrderId":"order-1","strategyId":"strategy-1",
                      "symbol":"SHFE.rb2610","volume":1,"price":3100,"tradeDateTime":100,
                      "futureQuote":9999,"direction":"BUY","offset":"OPEN"}]}


def test_evidence_rejects_wrong_strategy_and_excludes_future_information():
    request = ExplanationRequest(symbol="SHFE.rb2610",eventType="fill",eventId="fill-1",strategyId="strategy-1")
    evidence = resolve_evidence(snapshot(), request)
    assert evidence["environment"] == "replay"
    assert "futureQuote" not in evidence["event"]
    assert evidence["eventTime"] == 100
    with pytest.raises(HTTPException) as wrong:
        resolve_evidence(snapshot(), request.model_copy(update={"strategyId":"other"}))
    assert wrong.value.status_code == 409
    with pytest.raises(HTTPException) as missing:
        resolve_evidence(snapshot(), request.model_copy(update={"eventId":"other-user-fill"}))
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_model_receives_only_evidence_without_tools():
    calls=[]
    async def complete_structured(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(value=SimpleNamespace(summary="已成交一手；未提供当时指标，无法判断信号依据。"), tool_calls=[])
    adapter=SimpleNamespace(complete_structured=complete_structured)
    assert "已成交" in await collect_explanation(adapter,{"event":{"volume":1}})
    assert calls[0].get("tools") is None
    assert calls[0]["timeout_seconds"] == 30
    assert json.loads(calls[0]["input_text"]) == {"event":{"volume":1}}
    assert "不输出思考过程或工具调用" in calls[0]["instructions"]


@pytest.mark.asyncio
async def test_foreground_work_preempts_analysis_and_keeps_event(monkeypatch):
    entered=asyncio.Event()
    async def collect(*args): entered.set(); await asyncio.Event().wait()
    monkeypatch.setattr("trader_runtime.strategy_explanations.collect_explanation",collect)
    service=StrategyExplanations(None)
    adapter=SimpleNamespace(_gated_runs=set(),_design_lock=asyncio.Lock())
    result=ExplanationResult(id="e",status="waiting",evidence={"event":{"price":3100}},environment="replay")
    service.tasks["owner"]=asyncio.create_task(service._run(adapter,result))
    await entered.wait()
    service.preempt("owner")
    await service.tasks["owner"]
    assert result.status=="unavailable"
    assert result.evidence["event"]["price"]==3100
    assert "让位" in result.detail

@pytest.mark.asyncio
@pytest.mark.parametrize('signal_time,expected', [('90',True),('110',False)])
async def test_persisted_signal_is_scoped_and_never_uses_later_data(tmp_path,monkeypatch,signal_time,expected):
    from trader_runtime.main import create_app
    app=create_app(database_path=tmp_path/'state.sqlite3',runtime_state_root=tmp_path,engineering_root=tmp_path/'workspace',agent_mode='fake',data_mode='fake',local_token='test')
    store=app.state.store
    store.save_strategy_draft(strategy_id='strategy-1',user_id='owner',source_session_id='s',name='test',symbol='SHFE.rb2610',version=1,status='paused',definition={},definition_digest='d')
    store.save_strategy_intent_snapshot(intent_id='intent',strategy_id='strategy-1',user_id='owner',strategy_digest='d',signal_bar_time=signal_time,context_digest='c',payload={'signalSnapshot':{'action':'open_long'},'laterQuote':9999})
    store.reserve_strategy_execution_attempt(attempt_id='attempt',strategy_id='strategy-1',user_id='owner',intent_id='intent',signal_bar_time=signal_time,trading_day='2026-09-09',action='open_long',volume=1)
    store.finish_strategy_execution_attempt(attempt_id='attempt',status='SUBMITTED',client_order_id='order-1')
    monkeypatch.setattr('trader_runtime.strategy_explanations.activity_snapshot',lambda *args,**kwargs:snapshot())
    service=StrategyExplanations(app,app.state.ctx)
    body=ExplanationRequest(symbol='SHFE.rb2610',eventType='fill',eventId='fill-1')
    result=await service.request(_request(),'owner',body)
    assert ('recordedSignal' in result.evidence) is expected
    assert 'laterQuote' not in str(result.evidence)
    assert await service.request(_request(),'owner',body) is result
    other=await service.request(_request(),'other',body)
    assert 'recordedSignal' not in other.evidence
    assert other.id != result.id
    await service.close()
