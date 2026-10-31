"""Chat sessions, messages, the SSE event stream and the feed focus."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from ..auth import CloudAuth
from ..chat_suggestions import ChatSuggestions, SuggestionsRequest, SuggestionsResult
from ..guest import guest_request
from ..message_context import PENDING_CARD_REPLY, encode_instruction
from ..plans import daily_token_limit, default_allowed_model, plan_for_user
from ..runtime_feed import BoardWant
from ..wiring.context import RuntimeContext
from .requests import (
    FeedFocusRequest,
    FeedViewRequest,
    MessageRequest,
    SessionAccountRequest,
    SessionRequest,
)


def register(application: FastAPI, ctx: RuntimeContext) -> None:
    services = ctx.services
    agent = ctx.agent
    store = services.store
    broker = services.broker
    adapter = agent.adapter
    runs = agent.runs
    lifecycle = ctx.lifecycle
    runtime_feed = ctx.runtime_feed
    auth = ctx.auth
    current_user = ctx.current_user
    require_session = ctx.require_session
    native_endpoints = agent.native_endpoints

    configured_model_slugs = lambda: set(native_endpoints)  # noqa: E731
    @application.post("/api/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session(request: Request, body: SessionRequest, chat: bool = False) -> dict[str, str]:
        user_id = await current_user(request)
        if isinstance(auth, CloudAuth) and body.mode != "cloud":
            raise HTTPException(status_code=422, detail="cloud_runtime_requires_cloud_mode")
        agent_id = body.agentId
        # The session acts on the account the request names (header) or the user's
        # default; every later request of the session resolves back to it.
        account_id = (await ctx.account_context(request, user_id)).account.account_id
        session = broker.create_session(body.mode, agent_id, user_id, trading_account_id=account_id)
        if chat:
            store.retain_chat_session(session.session_id, user_id=user_id)
        await adapter.bootstrap(session.session_id)
        return {"sessionId": session.session_id, "mode": body.mode, "agentId": agent_id}

    @application.get("/api/chat-sessions")
    async def chat_sessions(request: Request, mode: Literal["local", "cloud"] = "local"):
        user_id = await current_user(request)
        return {"items": store.list_chat_sessions(user_id=user_id, mode=mode)}

    @application.post("/api/sessions/restore")
    async def restore_session(request: Request, body: SessionRequest):
        user_id = await current_user(request)
        if isinstance(auth, CloudAuth) and body.mode != "cloud":
            raise HTTPException(422, "cloud_runtime_requires_cloud_mode")
        previous = store.recent_chat_session(user_id=user_id, mode=body.mode)
        if previous is not None:
            session_id = previous["sessionId"]
            store.retain_chat_session(session_id, user_id=user_id)
            refresh = getattr(adapter, "refresh_session", None)
            if refresh is not None:
                await refresh(session_id)
            return store.get_session(session_id, user_id=user_id)
        account_id = (await ctx.account_context(request, user_id)).account.account_id
        session = broker.create_session(body.mode, body.agentId, user_id, trading_account_id=account_id)
        store.retain_chat_session(session.session_id, user_id=user_id)
        await adapter.bootstrap(session.session_id)
        return store.get_session(session.session_id, user_id=user_id)

    @application.get("/api/sessions/{session_id}")
    async def resume_session(request: Request, session_id: str) -> dict[str, object]:
        user_id = await current_user(request)
        try:
            session = store.resume_session(session_id, user_id=user_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="session_not_found") from error
        refresh_adapter = getattr(adapter, "refresh_session", None)
        if refresh_adapter is not None:
            await refresh_adapter(session_id)
        return session

    @application.delete("/api/sessions/{session_id}")
    async def delete_session(request: Request, session_id: str) -> dict[str, object]:
        user_id = await current_user(request)
        require_session(session_id, user_id)
        application.state.app_capabilities.forget_session(session_id)
        runtime_feed.forget_session(session_id)
        deleted = await lifecycle.release(session_id)
        return {"sessionId": session_id, "status": "deleted" if deleted else "missing"}

    @application.put("/api/sessions/{session_id}/feed")
    async def set_feed_focus(request: Request, session_id: str, body: FeedFocusRequest) -> dict[str, object]:
        """Tell the push feed which market this session is looking at."""
        user_id = await current_user(request)
        require_session(session_id, user_id)
        try:
            focus = runtime_feed.set_focus(session_id, symbol=body.symbol, duration=body.durationSeconds,
                                           market_patches=body.marketPatches, feed_touch=body.feedTouch,
                                           since=body.since)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except KeyError as error:
            raise HTTPException(status_code=404, detail="session_not_found") from error
        return {"sessionId": session_id, "symbol": focus.symbol, "durationSeconds": focus.duration}

    @application.put("/api/sessions/{session_id}/feed/view")
    async def set_feed_view(request: Request, session_id: str, body: FeedViewRequest) -> dict[str, object]:
        """What this session's window shows (2026-10-12): page visibility and the 报价总表 rows on
        screen. The push sends the board (feed.board, then feed.board-patch) only to a visible
        window that shows it, and pauses the chart market while the page is hidden; every call
        starts the board over with one full frame, and a page coming back gets a full chart too."""
        user_id = await current_user(request)
        require_session(session_id, user_id)
        want = None
        # 报价总表 is a member surface (GET /api/market/board is 403 for a guest): a guest's view
        # keeps its visibility and simply carries no board.
        if body.board is not None and not guest_request(request):
            want = BoardWant.of(everything=body.board.all, exchanges=body.board.exchanges,
                                products=body.board.products, symbols=body.board.symbols, key=body.board.key)
        try:
            state = runtime_feed.set_view(session_id, visible=body.visible, board=want)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="session_not_found") from error
        return {"sessionId": session_id, "visible": state.visible, "board": state.board is not None}

    @application.put("/api/sessions/{session_id}/account")
    async def set_session_account(
        request: Request, session_id: str, body: SessionAccountRequest,
    ) -> dict[str, str | int]:
        """Bind the session to another account the caller is granted on (4.4a).
        The feed follows on its next tick and every later agent turn of the
        session reads and trades the new account. Either way the session's
        runtime.status is published and `statusAt` is that event's timestamp: a
        window waits for a status at least that new before it trusts the account
        (#932 R3). The answer and runtime.status carry the session's account
        revision (`accountGeneration`, the session row's own counter — shared by
        every Runtime slot on the database). A switch naming `expectedGeneration`
        is one conditional update that applies only while the row still has that
        revision and then advances it (a repeat of the same account too); one
        decided before a newer switch landed — a request left in flight across a
        reconnect or a blue-green switch — is 409 account_switch_superseded and
        changes nothing, never the old account written last (#932 follow-up).
        The revision is checked before the target is resolved, so a superseded
        request never assembles an account. Without the field (an older client)
        the switch applies as before; a real move still advances the revision.
        Grant checks come from the account context, so an unknown or ungranted
        target — including one this boot no longer assembles — is 403, never a
        silent fall-back to the default account."""
        user_id = await current_user(request)
        require_session(session_id, user_id)
        expected = body.expectedGeneration

        def superseded(revision: int, account_id: str) -> HTTPException:
            # The session's revision and account as one read saw them — the precheck's query or the conditional
            # update's own transaction — so a window can show the server's account at once (Codex r4309888894).
            return HTTPException(status_code=409, detail={
                "code": "account_switch_superseded", "accountGeneration": revision, "accountId": account_id})

        # A cheap read first: a superseded switch neither resolves nor assembles its target account.
        if expected is not None:
            try:
                revision_now, account_now = store.session_account_state(session_id, user_id=user_id)
            except KeyError as error:   # deleted meanwhile (Codex r4309822263)
                raise HTTPException(status_code=404, detail="session_not_found") from error
            if revision_now != expected:
                raise superseded(revision_now, account_now)
        # The header would otherwise shadow the session's own account; the named
        # target decides, and it must be one the user holds a grant on.
        context = await ctx.account_context(request, user_id, account_id=body.accountId)
        target = context.account.account_id
        try:
            # "Did the account change" is what this very write replaced (Codex r4309822248), never an earlier read.
            if expected is None:
                revision, previous = store.set_session_trading_account(session_id, target, user_id=user_id)
            else:
                applied, revision, account_now, previous = store.switch_session_account(
                    session_id, target, user_id=user_id, expected=expected)
                if not applied:
                    raise superseded(revision, account_now)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="session_not_found") from error
        if previous != target:
            runtime_feed.session_account_changed(session_id, target)
        event = await broker.publish(session_id, kind="result", name="runtime.status",
                                     payload=ctx.status_for_session(session_id))
        # The revision this switch committed (Codex r4309759904): a later switch landing while the status was published
        # must not hand this window a token for an account state it never saw.
        return {"sessionId": session_id, "accountId": target, "statusAt": str(event["timestamp"]),
                "accountGeneration": revision}

    @application.post("/api/sessions/{session_id}/messages", status_code=status.HTTP_202_ACCEPTED)
    async def send_message(request: Request, session_id: str, body: MessageRequest) -> dict[str, str]:
        user_id = await current_user(request)
        require_session(session_id, user_id)
        application.state.presence.touch(user_id)
        if body.uiWindowId:
            await application.state.verify_ui_window(body.uiWindowId, user_id)
        return await enqueue_message(request, session_id, user_id, body)

    async def enqueue_message(request: Request, session_id: str, user_id: str, body: MessageRequest) -> dict[str, str]:
        """One user message into the session's run queue, with the tier's token cap and model set — the
        typed path, and 我的流程's 运行 (2026-10-06: POST /api/routines/{id}/run sends the routine's
        name through here, so a run is metered and queued exactly like the user typing it; #522: with
        the routine's id, so the run is the routine picked, not whichever name the text matches)."""
        # M2: the tier's token cap (with the global env backstop) and the tier's
        # model set — a stored preference the tier no longer allows falls back
        # to the first allowed model without being rewritten.
        plan = plan_for_user(store, user_id)
        daily_limit = daily_token_limit(
            plan, env_limit=int(os.getenv("AI_TRADER_DAILY_TOKEN_LIMIT", "200000000")))
        if daily_limit is not None and store.token_usage(
            user_id, datetime.now(UTC).date().isoformat(),
        ) >= daily_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="daily_token_quota_exceeded",
            )
        configured_slugs = configured_model_slugs()
        model_slug = default_allowed_model(plan, configured_slugs, store.get_model_pref(user_id))
        if model_slug is None and configured_slugs:
            # Models exist but none is in this tier: refuse rather than run the
            # turn on a model the user is not entitled to.
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="model_not_in_plan")
        application.state.app_capabilities.bind_session(session_id, user_id, request.headers)
        # The session's account is assembled before its turn, as any read of that account would (2026-10-09:
        # after a Runtime restart the first message on an idle paper account stopped with "当前未装配"). A
        # refusal here is left to the turn, which states the account's condition itself.
        session = broker.get_session(session_id, user_id)
        if session is not None and session.trading_account_id:
            try:
                await ctx.account_context(request, user_id, account_id=session.trading_account_id)
            except HTTPException:
                pass
        application.state.strategy_explanations.preempt(user_id)
        card_reply = PENDING_CARD_REPLY.get() == (user_id, session_id) and body.origin == "voice"
        token = PENDING_CARD_REPLY.set(None)  # no ambient handoff in long-lived queue workers
        try:
            run = runs.enqueue(
                session_id=session_id,
                user_id=user_id,
                instruction=encode_instruction(body.text, body.workspaceContext, body.uiWindowId, origin=body.origin, follow_up=bool(body.followUp),
                                               routine_id=body.routineId, pending_card_reply=card_reply),
                model_slug=model_slug,
            )
        except ValueError as error:
            # #515 F-d: the session is being cleaned up under the request — say so instead of
            # accepting a run whose rows the release deletes next.
            if str(error) == "session_releasing":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="session_releasing") from error
            raise
        finally:
            PENDING_CARD_REPLY.reset(token)
        return {"status": "accepted", "sessionId": session_id, "runId": str(run["runId"])}

    application.state.enqueue_chat_message = enqueue_message
    suggestions = ChatSuggestions()
    application.state.chat_suggestions = suggestions

    @application.post("/api/agent/suggestions", response_model=SuggestionsResult)
    async def agent_suggestions(request: Request, body: SuggestionsRequest) -> SuggestionsResult:
        """输入框上方的 AI 建议问题（owner 2026-10-05）：0–2 句，按当前行情、持仓、
        时间和对话生成；游客、无凭据、额度不足或模型失败都安静地返回空数组，
        不进能力目录（界面辅助，不是业务能力）。"""
        if guest_request(request):
            return SuggestionsResult(items=[])
        user_id = await current_user(request)
        return SuggestionsResult(items=await suggestions.read(application, ctx, request, user_id, body))

    @application.get("/api/events")
    async def events(request: Request, session_id: str = Query(alias="sessionId"),
                     last_event_id: str | None = Header(default=None, alias="Last-Event-ID")) -> StreamingResponse:
        user_id = await current_user(request)
        require_session(session_id, user_id)
        # M1a: a live event stream is a durable "user is online" signal (the
        # client keeps the SSE open the whole time the app is).
        application.state.presence.touch(user_id)
        identity = application.state.app_capabilities.capture_identity(user_id, request.headers, session_id)
        try:
            sequence = int(last_event_id or "0")
        except ValueError:
            sequence = 0
        return StreamingResponse(
            broker.stream(
                session_id, request, sequence, authorize=identity.verify,
                # A resumed session never gets another bootstrap status; every
                # reader starts with a transient one so runtime.account is never
                # empty. Transient: not staged, outside the Last-Event-ID replay.
                on_subscribed=lambda: broker.publish_transient(
                    session_id, kind="result", name="runtime.status",
                    payload=ctx.status_for_session(session_id))),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )
