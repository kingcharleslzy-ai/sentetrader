"""Ephemeral, identity-bound native window captures; only the UI supplies pixels."""
import asyncio
import base64
from dataclasses import dataclass
from time import monotonic
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

# The idle heartbeat contract (one value for ui_control too): an idle window pulses every 30 s
# and at once when a command, capture or receipt waits (feed.ui nudge); a closing page says so
# immediately. 75 s tolerates one lost pulse. Was 10 s / 20 s until 2026-09-27: the pulse was the
# largest share of an idle page's requests.
ONLINE_SECONDS = 75.0


class CaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requestId: UUID
    expectedRevision: int = Field(ge=0, strict=True)


class CapturedImage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mimeType: Literal["image/png", "image/jpeg"]
    data: str = Field(min_length=1, max_length=2_800_000)

    @model_validator(mode="after")
    def valid_image(self):
        try:
            decoded = base64.b64decode(self.data, validate=True)
        except ValueError as error:
            raise ValueError("截图编码无效") from error
        signature = b"\x89PNG\r\n\x1a\n" if self.mimeType == "image/png" else b"\xff\xd8\xff"
        if len(decoded) > 2_000_000 or not decoded.startswith(signature):
            raise ValueError("截图格式或大小无效")
        return self


class CaptureReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requestId: UUID
    observedRevision: int = Field(ge=0, strict=True)
    capturedAt: str = Field(max_length=40)
    image: CapturedImage | None = None
    error: Literal["private_window", "page_changed", "capture_unavailable"] | None = None
    # `page_changed` only: which visible parts differed (text, controls, regions, activeDialog, viewport, revision).
    detail: str | None = Field(default=None, max_length=200)


class CaptureResult(CaptureRequest):
    windowId: UUID
    status: Literal["queued", "dispatched", "ready", "rejected", "expired"]
    capturedAt: str | None = None
    image: CapturedImage | None = None
    error: str | None = None


def separate_capture_image(result):
    """Keep native image content out of JSON/text tool output."""
    if not isinstance(result, dict) or "/captures" not in result.get("operation", ""):
        return result, None
    data = result.get("data")
    if not isinstance(data, dict) or data.get("status") != "ready" or not data.get("image"):
        return result, None
    image = CapturedImage.model_validate(data["image"])
    return {**result, "data": {key: value for key, value in data.items() if key != "image"}}, image


@dataclass
class Capture:
    user: str
    identity: object
    result: CaptureResult
    created: float


def register_ui_captures(app, identity, owned_window, lock, commands):
    captures: dict[tuple[str, UUID], Capture] = {}

    def prune():
        for key, capture in list(captures.items()):
            age = monotonic() - capture.created
            if age > 60:
                del captures[key]
            elif age > 10 and capture.result.status in {"queued", "dispatched"}:
                capture.result.status = "expired"

    async def result_for(user, identifier):
        prune()
        capture = captures.get((user, identifier))
        if capture is None:
            raise HTTPException(404, "截图不存在或已过期，请重新读取窗口")
        await capture.identity.verify()
        await owned_window(capture.result.windowId, user)
        return capture.result

    @app.post("/api/app/ui/windows/{window_id}/captures", response_model=CaptureResult, response_model_exclude_none=True)
    async def request_capture(request: Request, window_id: UUID, body: CaptureRequest,
                              waitMs: int = Query(default=0, ge=0, le=5000)):
        async with lock:
            user, caller = await identity(request)
            prune()
            window = await owned_window(window_id, user)
            if monotonic() - window.seen > ONLINE_SECONDS:
                raise HTTPException(409, "目标窗口离线")
            if not window.pulse.device or not window.pulse.device.nativeCapture:
                raise HTTPException(409, "当前客户端不支持原生截图")
            key = (user, body.requestId)
            if key in captures:
                saved = captures[key].result
                if saved.windowId != window_id or saved.expectedRevision != body.expectedRevision:
                    raise HTTPException(409, "截图编号已用于其他参数")
            else:
                if body.expectedRevision != window.pulse.revision:
                    raise HTTPException(409, "页面已变化，请重新读取窗口")
                if any(c.user == user and c.request.windowId == window_id and c.status in {"queued", "dispatched", "unknown"}
                       for c in commands.values()):
                    raise HTTPException(409, "请先确认已有界面操作结果")
                if sum(c.user == user for c in captures.values()) >= 8:
                    raise HTTPException(429, "截图请求过多，请稍后重试")
                captures[key] = Capture(user, caller, CaptureResult(**body.model_dump(), windowId=window_id, status="queued"), monotonic())
        feed = getattr(app.state, "runtime_feed", None)
        if feed is not None:
            await feed.publish_ui_nudge(user, "capture")
        deadline = asyncio.get_running_loop().time() + waitMs / 1000
        while True:
            async with lock:
                result = await result_for(user, body.requestId)
                remaining = deadline - asyncio.get_running_loop().time()
                if result.status not in {"queued", "dispatched"} or remaining <= 0:
                    return result
            await asyncio.sleep(min(.05, remaining))

    @app.get("/api/app/ui/captures/{request_id}", response_model=CaptureResult, response_model_exclude_none=True)
    async def read_capture(request: Request, request_id: UUID):
        async with lock:
            user, _ = await identity(request)
            return await result_for(user, request_id)

    async def accept(body, user):
        prune()
        receipt = body.captureReceipt
        if receipt is None:
            return
        capture = captures.get((user, receipt.requestId))
        if capture is None or capture.result.windowId != body.windowId or capture.result.status != "dispatched":
            return
        result = capture.result
        try:
            await capture.identity.verify()
        except HTTPException:
            result.status, result.error = "rejected", "登录身份已失效"
            return
        if receipt.error or receipt.observedRevision != result.expectedRevision or body.revision != result.expectedRevision or receipt.image is None:
            result.status = "rejected"
            result.error = {"private_window": "凭据窗口不提供截图", "page_changed": "截图期间页面已变化",
                            "capture_unavailable": "客户端截图暂不可用"}.get(receipt.error, "页面版本不一致")
            if receipt.error == "page_changed" and receipt.detail:
                result.error = f"截图期间页面已变化（{receipt.detail}）"
            return
        result.status, result.image, result.capturedAt = "ready", receipt.image, receipt.capturedAt

    async def dispatch(body, user):
        prune()
        for capture in captures.values():
            result = capture.result
            if capture.user != user or result.windowId != body.windowId or result.status != "queued":
                continue
            try:
                await capture.identity.verify()
            except HTTPException:
                result.status, result.error = "rejected", "登录身份已失效"
                continue
            if body.revision != result.expectedRevision:
                result.status, result.error = "rejected", "页面已变化"
                continue
            result.status = "dispatched"
            return CaptureRequest(requestId=result.requestId, expectedRevision=result.expectedRevision)
        return None

    return accept, dispatch
