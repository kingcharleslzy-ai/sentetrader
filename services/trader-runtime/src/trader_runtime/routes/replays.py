"""Historical replay child runtimes: creation, status, control and the mounted child apps."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request

from ..instruments import foreign_product_of
from ..replay_requests import FOREIGN_REPLAY_REFUSAL, ReplayControl, ReplayCreate
from ..skill_registry import human_request, require_skill
from ..wiring.context import RuntimeContext


def register(application: FastAPI, ctx: RuntimeContext) -> None:
    services = ctx.services
    settings = ctx.settings
    store = services.store
    # Replay children always run the single default account (the accounts file
    # is ignored for them), so the replay router shares the default provider.
    provider = services.accounts.default.provider
    auth = ctx.auth
    current_user = ctx.current_user
    state_root = settings.state_root
    _replay_provider = ctx.replay_provider

    if _replay_provider is None and settings.auth_mode != "cloud-jwt":
        from ..replay import ReplayRuntimeRouter, adoptable_public

        assert ctx.create_replay_child is not None
        create_replay_child = ctx.create_replay_child
        replay_router = ReplayRuntimeRouter(auth, provider, state_root, create_replay_child, parent=application)
        application.state.replay_router = replay_router
        ctx.replay_router = replay_router
        ctx.lifecycle.replay_router = replay_router

        @application.post("/api/replays", status_code=201)
        async def create_replay(request: Request, body: ReplayCreate):
            # The market-replay skill gates this POST at the app-level write
            # gate; the read endpoints stay open so a finished replay's
            # history stays visible.
            user_id = await current_user(request)
            if foreign_product_of(body.symbol) is not None:
                raise HTTPException(422, FOREIGN_REPLAY_REFUSAL)
            return await replay_router.create(user_id, body.model_dump(mode="json", exclude_none=True))

        @application.get("/api/replays/{identifier}")
        async def replay_status(identifier: str, request: Request):
            return (await replay_router.resolve(identifier, await current_user(request))).public()

        @application.get("/api/replays")
        async def active_replays(request: Request):
            user_id = await current_user(request)
            return {"items": [item.public() for item in replay_router.contexts.values() if item.user_id == user_id]
                    + [adoptable_public(metadata) for metadata in replay_router.adoptable(user_id)]}

        @application.post("/api/replays/{identifier}/control")
        async def replay_control(identifier: str, request: Request, control: ReplayControl):
            user_id = await current_user(request)
            context = await replay_router.resolve(identifier, user_id)
            identity = request.scope.get("ai_trader.call_identity")
            if identity is None:
                identity = application.state.app_capabilities.capture_identity(user_id, request.headers)
            elif identity.user != user_id:
                raise HTTPException(403, "session_owner_mismatch")
            body = control.model_dump(exclude_none=True)
            action = body.get("action")
            # The app-level write gate skips this route: closing stays open
            # (the stop-like action); the rest follow the skill, gated here.
            if action != "close":
                require_skill(store, user_id, "replay-control", human=human_request(request))
            if action == "close":
                from ..operations import owned_operation
                async def close_replay():
                    context.require_open(user_id)
                    await context.stop()
                    replay_router.contexts.pop(identifier, None)
                    replay_router.forget(identifier, confirmed_closed=context.sidecar_closed)
                    return {"status": "closed"}
                return await owned_operation(close_replay, authorize=identity.verify)

            async def apply_control():
                if action == "pause":
                    context.playing = False
                elif action == "pause_entries":
                    context.provider.entries_paused = True
                elif action == "resume_entries" and not context.ending and not context.ended:
                    context.provider.entries_paused = False
                elif action == "finish" and not context.ended:
                    try:
                        await context.finish()
                    except RuntimeError as error:
                        # finish reads the snapshot before it marks the replay ending: an unusable
                        # one (incomplete / unconfirmed / stale, #932 B3) refuses it unchanged.
                        if str(error) == "replay_snapshot_unavailable":
                            raise HTTPException(409, "replay_snapshot_unavailable") from error
                        raise
                elif action == "speed" and body.get("speed") in {1, 2, 5, 10} and not isinstance(body.get("speed"), bool):
                    context.speed = body["speed"]
                # incomplete (#932 B3): the last step stopped part-way through a minute; play/step
                # finish that same minute first, then run the order query, protection and strategy.
                elif (action in {"play", "step"} and context.provider.snapshot.get("status") in {"ready", "incomplete"}
                      and not context.ended):
                    context.error = None
                    if action == "play":
                        context.playing = True
                    else:
                        context.playing = False
                        try:
                            await context.advance()
                        except Exception as error:
                            context.error = "单步结果未确认，已暂停；请查看订单状态，不要重复下单。"
                            raise HTTPException(503, "replay_step_unconfirmed") from error
                else:
                    raise HTTPException(409, "replay_control_not_ready")
                return context.public()
            return await context.call_tool(user_id, apply_control, authorize=identity.verify)

        application.mount("/api/replays", replay_router)
