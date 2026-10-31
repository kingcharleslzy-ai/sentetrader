"""Packaged CLI for exactly the same authenticated product capabilities."""
import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

import httpx

from .app_connection import DEFAULT_CONNECTION_FILE, account_headers, connection_options


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ai-trader-runtime cli")
    parser.add_argument("--connection-file", type=Path, default=DEFAULT_CONNECTION_FILE)
    parser.add_argument("--account", help="作用的账户 id（GET /api/accounts 列出）；不填按会话或默认账户")
    commands = parser.add_subparsers(dest="command", required=True)
    caps = commands.add_parser("capabilities", help="查询能力目录或接口定义")
    caps.add_argument("--operation")
    call = commands.add_parser("call", help="从文件或标准输入读取AppCall JSON")
    call.add_argument("--file", type=Path)
    skills = commands.add_parser("skills", help="查看内置技能目录（与 MCP 的技能工具同源）")
    skills_commands = skills.add_subparsers(dest="skills_command", required=True)
    skills_commands.add_parser("list", help="列出技能及当前账号的可用状态")
    show = skills_commands.add_parser("show", help="读取一个技能的完整手册")
    show.add_argument("skill_id", help="技能 id（skills list 里的 id）")
    show.add_argument("--resource", help="改读技能目录内的附加资源相对路径")
    commands.add_parser("mcp-config", help="输出不含凭据的 MCP 启动配置")
    commands.add_parser("doctor", help="检查打包 MCP 的初始化、能力发现和账号连接")
    migrate = commands.add_parser("migrate-strategies", help="预览或迁移本机旧策略，不迁移执行状态")
    migrate.add_argument("--database", type=Path)
    migrate.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        account = account_headers(args.account)
    except ValueError as error:
        parser.error(str(error))
    mcp_args = (["mcp"] if getattr(sys, "frozen", False) else ["-m", "trader_runtime.sidecar_entry", "mcp"])
    if args.command == "mcp-config":
        print(json.dumps({"mcpServers": {"ai-trader": {"command": sys.executable, "args": mcp_args}}}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "doctor":
        async def check():
            from mcp import Client
            from mcp.client.stdio import StdioServerParameters
            async with asyncio.timeout(10), Client(StdioServerParameters(command=sys.executable,
                args=[*mcp_args, "--connection-file", str(args.connection_file)])) as client:
                tools = await client.list_tools()
                result = await client.call_tool("app_capabilities", {})
                return {"connected": not result.is_error, "tools": [tool.name for tool in tools.tools],
                        "message": "MCP 与账号连接正常" if not result.is_error else "请先在客户端登录并连接服务"}
        try:
            result = asyncio.run(check())
        except Exception:
            result = {"connected": False, "message": "MCP 检查未完成，请确认客户端登录和网络连接"}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["connected"] else 1
    try:
        with httpx.Client(**connection_options(args.connection_file)) as client:
            client.headers.update(account)
            if args.command == "migrate-strategies":
                from .strategy_migration import migrate_local_plans
                database = args.database or args.connection_file.parent.parent / "ai-trader.sqlite3"
                result = migrate_local_plans(client, database, apply=args.apply)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.command == "capabilities":
                response = client.get("/api/agent/capabilities", params={"operation": args.operation} if args.operation else {})
            elif args.command == "skills":
                if args.skills_command == "list":
                    response = client.get("/api/agent/skills")
                else:
                    skill_path = f"/api/agent/skills/{quote(args.skill_id, safe='')}"
                    response = (client.get(f"{skill_path}/resource", params={"path": args.resource})
                                if args.resource else client.get(skill_path))
            else:
                body = json.loads(args.file.read_text() if args.file else sys.stdin.read())
                response = client.post("/api/agent/call", json=body)
        data = response.json()
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if response.is_success and data.get("ok", True) else 1
    except (OSError, ValueError, KeyError, sqlite3.Error, httpx.HTTPError):
        # Never echo connection contents or request headers.
        print("操作未完成：请确认客户端已启动、连接文件权限正确且请求符合能力定义。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
