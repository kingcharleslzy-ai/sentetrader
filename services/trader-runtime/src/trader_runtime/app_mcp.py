"""Standard stdio MCP transport for the existing authenticated capability API."""
import argparse
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from . import __version__
from .agent_api import AppCall
from .app_connection import DEFAULT_CONNECTION_FILE, HostHttpPool, account_headers
from .ui_captures import separate_capture_image


def create_server(connection_file: Path) -> MCPServer:
    pool = HostHttpPool(connection_file)

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield {}
        finally:
            await pool.aclose()

    server = MCPServer("Sente", version=__version__, lifespan=lifespan, instructions=(
        "这是 Sente（原 AI Trader）的业务接口。先查询 app_capabilities，按 operation 读取参数定义，再调用 app_call 并回读结果。"
        "界面命令 queued/dispatched 不表示完成；按原 commandId 查询到 applied 后才能报告成功。"
        "交易、撤单或启动策略需要用户授权；超时或取消等待不代表业务撤销，禁止自动重发。"
        "当前仅模拟盘和历史回放，实盘永久关闭。"))

    async def request(method, path, **kwargs) -> dict[str, Any]:
        try:
            response = await pool.request(method, path, **kwargs)
            result = response.json()
        except (OSError, ValueError, KeyError, httpx.HTTPError):
            raise ToolError("无法确认操作结果：检查客户端连接；写操作请查询原编号，不要自动重发。") from None
        if not isinstance(result, dict):
            raise ToolError("服务未返回有效结果，请回读原操作状态。")
        if not response.is_success or result.get("ok") is False:
            raise ToolError(json.dumps(result, ensure_ascii=False))
        return result

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False))
    async def app_capabilities(operation: str | None = None) -> dict[str, Any]:
        """读取当前 Runtime 的能力目录；提供 operation 可读取完整参数定义。"""
        return await request("GET", "/api/agent/capabilities", params={"operation": operation} if operation else {})

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                           idempotent_hint=False, open_world_hint=True))
    async def app_call(operation: str, path: dict[str, str] | None = None,
                       query: dict[str, str | int | float | bool] | None = None,
                       body: dict | None = None, replayId: str | None = None,
                       account: str | None = None) -> dict[str, Any]:
        """调用已发现的业务接口。沿用软件权限、风险检查与回放调度，不自动重试。

        account：作用的账户 id（GET /api/accounts 列出）；不填按会话绑定账户或默认账户。
        """
        try:
            headers = account_headers(account)
        except ValueError as error:
            raise ToolError(str(error)) from None
        call = AppCall(operation=operation, path=path or {}, query=query or {}, body=body, replayId=replayId)
        if operation == "GET /api/app/ui/windows" and not call.query:
            device_file = connection_file.with_name("agent-device.json")
            if not device_file.exists():
                raise ToolError("当前设备尚未登记，请打开客户端；跨设备查询必须明确指定 deviceId。")
            if device_file.exists():
                try:
                    device = json.loads(device_file.read_text())["deviceId"]
                    if isinstance(device, str) and 8 <= len(device) <= 100:
                        call.query = {"deviceId": device}
                    else:
                        raise ValueError("invalid_device")
                except (OSError, ValueError, KeyError):
                    raise ToolError("当前设备标识不可用，请重新打开客户端") from None
        result = await request("POST", "/api/agent/call", json=call.model_dump(exclude_none=True), headers=headers)
        metadata, captured = separate_capture_image(result)
        if captured is not None:
            return CallToolResult(structured_content=metadata, content=[
                TextContent(text=json.dumps(metadata, ensure_ascii=False)),
                ImageContent(data=captured.data, mime_type=captured.mimeType),
            ])
        return result

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False))
    async def list_skills() -> dict[str, Any]:
        """列出当前账号可用的内置技能（id、名称、分类、说明、典型说法）。

        停用或档位未开通的技能不出现（服务端过滤）；需要某个技能的详细操作
        手册时用 activate_skill 加载，资源细节用 read_skill_resource 读取。
        """
        result = await request("GET", "/api/agent/skills")
        return {"skills": [
            {"id": item["id"], "label": item["label"], "category": item["category"],
             "description": item["description"], "phrases": item["phrases"]}
            for item in result.get("items", []) if item.get("available")
        ]}

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False))
    async def activate_skill(skillId: str) -> dict[str, Any]:
        """加载一个技能的完整操作手册（何时用/不用、步骤、安全边界与附加资源列表）。

        与内置 AI 的渐进披露同一接口：skillId 用 list_skills 返回的 id；
        技能不可用（停用/档位未开通）时返回业务错误码。
        """
        return await request("GET", f"/api/agent/skills/{quote(skillId, safe='')}")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False))
    async def read_skill_resource(skillId: str, path: str) -> dict[str, Any]:
        """读取一个已加载技能目录内的附加资源（references/ 下的相对路径）。

        path 用 activate_skill 返回的手册里 <skill_resources> 列出的路径；
        目录外路径（..、绝对路径）与超限文件都会被拒绝。
        """
        return await request("GET", f"/api/agent/skills/{quote(skillId, safe='')}/resource",
                             params={"path": path})

    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ai-trader-runtime mcp")
    parser.add_argument("--connection-file", type=Path, default=DEFAULT_CONNECTION_FILE)
    args = parser.parse_args(argv)
    create_server(args.connection_file).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
