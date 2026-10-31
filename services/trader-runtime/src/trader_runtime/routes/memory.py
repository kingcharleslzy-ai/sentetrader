"""The private memory surface (GET/POST/PUT/DELETE /api/memory): every route is
authenticated and scoped to the caller's user_id — another user's id is simply
'没有这条记忆'. Failing a memory is a status flip, never a delete."""
from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .. import user_memory
from ..memory_consolidation import MemoryConsolidator
from ..skill_registry import human_request
from ..store.memory import MemoryConflict
from ..wiring.context import RuntimeContext


class MemoryCreate(BaseModel):
    layer: Literal["profile", "rule", "lesson"]
    text: str = Field(min_length=1, max_length=user_memory.MAX_TEXT * 4)
    quote: str | None = Field(default=None, max_length=200)
    products: list[str] | None = Field(default=None, max_length=5)
    via: Literal["settings", "chat"] = "settings"


class MemoryUpdate(BaseModel):
    text: str = Field(min_length=1, max_length=user_memory.MAX_TEXT * 4)
    # AI（非人工调用）改记忆必须带用户原话，核对规则同新增；设置页人工修改不需要。
    quote: str | None = Field(default=None, max_length=200)


def _item(memory: dict) -> dict:
    return {"id": memory["id"], "layer": memory["layer"], "layerLabel": user_memory.LAYER_LABELS[memory["layer"]],
            "text": memory["text"], "source": memory["source"], "evidence": memory["evidence"],
            "tags": memory["tags"], "status": memory["status"], "createdAt": memory["createdAt"],
            "updatedAt": memory["updatedAt"], "invalidAt": memory["invalidAt"],
            "invalidReason": memory["invalidReason"]}


def _change(change: dict) -> dict:
    return {"id": change["id"], "memoryId": change["memoryId"], "op": change["op"],
            "actor": change["actor"], "at": change["at"], "before": change["before"],
            "after": change["after"]}


def register(application: FastAPI, ctx: RuntimeContext) -> None:
    store = ctx.services.store
    current_user = ctx.current_user
    # 3b 每日整理（lifespan 里 start/close）：统计走复盘同一条链路，原话提炼用默认便宜模型。
    application.state.memory_consolidator = MemoryConsolidator(application, store, ctx.services.research_desk)

    def _memory_enabled(user_id: str) -> bool:
        try:
            return store.user_preferences(user_id).chat.memory
        except Exception:
            return False

    @application.get("/api/memory")
    async def list_memory(request: Request, includeInvalid: bool = False) -> dict[str, object]:
        user_id = await current_user(request)
        return {"enabled": _memory_enabled(user_id),
                "items": [_item(memory) for memory in store.list_memories(user_id, include_invalid=includeInvalid)],
                "changes": [_change(change) for change in store.list_memory_changes(user_id)]}

    @application.post("/api/memory", status_code=201)
    async def create_memory(body: MemoryCreate, request: Request) -> dict[str, object]:
        user_id = await current_user(request)
        # 非人工请求（AI 经能力目录 /api/agent/call 转发）一律按对话来源处理 (#524-1)：同样要
        # 原话、同样受“记忆已关闭”约束，审计记 ai；body 里的 via 只对用户自己的请求有效。
        via = body.via if human_request(request) else "chat"
        try:
            memory = user_memory.add(store, user_id, layer=body.layer, text=body.text,
                                     quote=body.quote, products=body.products, via=via)
        except user_memory.MemoryDisabled as error:
            raise HTTPException(status_code=409, detail="记忆已关闭，可在设置 → 记忆打开") from error
        except user_memory.MemoryRefused as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return _item(memory)

    @application.put("/api/memory/{memory_id}")
    async def update_memory(memory_id: str, body: MemoryUpdate, request: Request) -> dict[str, object]:
        user_id = await current_user(request)
        try:
            return _item(user_memory.update(store, user_id, memory_id, body.text,
                                            actor="user" if human_request(request) else "ai", quote=body.quote))
        except KeyError as error:
            raise HTTPException(status_code=404, detail="没有这条记忆") from error
        except user_memory.MemoryDisabled as error:
            raise HTTPException(status_code=409, detail="记忆已关闭，可在设置 → 记忆打开") from error
        except user_memory.MemoryRefused as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @application.delete("/api/memory/{memory_id}")
    async def delete_memory(memory_id: str, request: Request, reason: str | None = Query(None, max_length=120),
                            via: Literal["settings", "chat"] = Query("settings")) -> dict[str, object]:
        user_id = await current_user(request)
        actor = ("ai" if via == "chat" else "user") if human_request(request) else "ai"
        try:
            user_memory.invalidate(store, user_id, memory_id, reason or "用户删除", actor=actor)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="没有这条记忆") from error
        return {"id": memory_id, "status": "invalid"}

    @application.post("/api/memory/changes/{change_id}/undo")
    async def undo_memory_change(change_id: str, request: Request) -> dict[str, object]:
        """逐步撤销原变动；重复且状态未变复用结果，后续编辑/历史依据不足409，请重读后由用户决定。"""
        user_id = await current_user(request)
        try:
            return _item(user_memory.undo(store, user_id, change_id,
                                          actor="user" if human_request(request) else "ai"))
        except KeyError as error:
            raise HTTPException(status_code=404, detail="没有这条记忆") from error
        except MemoryConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except user_memory.MemoryDisabled as error:
            raise HTTPException(status_code=409, detail="记忆已关闭，可在设置 → 记忆打开") from error

    @application.post("/api/memory/consolidate")
    async def consolidate_memory(request: Request) -> dict[str, object]:
        """“现在整理一次”：只整理当前用户，窗口与每日 06:00 相同，覆盖当天认领行。"""
        user_id = await current_user(request)
        if not _memory_enabled(user_id):
            raise HTTPException(status_code=409, detail="记忆已关闭，可在设置 → 记忆打开")
        consolidator: MemoryConsolidator = application.state.memory_consolidator
        if not consolidator.allow_manual(user_id):
            raise HTTPException(status_code=429, detail="刚整理过，10 分钟后再试")
        return await consolidator.run_manual(user_id)
