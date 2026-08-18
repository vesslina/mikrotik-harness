from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mth.core.mcp_client.models import BackendInspection, McpTool, McpToolResult
from mth.core.mcp_client.runtime import MikroMcpRuntime


class MikroMcpClient:
    """Short-lived stdio client; every session starts the pinned child process."""

    def __init__(
        self,
        *,
        runtime: MikroMcpRuntime | None = None,
        environment: Mapping[str, str] | None = None,
        read_timeout: float = 30.0,
    ) -> None:
        self._runtime = runtime or MikroMcpRuntime()
        self._environment = dict(environment or {})
        self._read_timeout = read_timeout

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        self._runtime.validate()
        parameters = StdioServerParameters(
            command=self._runtime.node_command,
            args=[str(self._runtime.entrypoint), "serve"],
            cwd=str(self._runtime.backend_dir),
            env=self._runtime.process_environment(self._environment),
        )
        async with stdio_client(parameters) as (read_stream, write_stream), ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=self._read_timeout),
        ) as session:
            await session.initialize()
            yield session

    async def list_tools(self) -> tuple[McpTool, ...]:
        async with self._session() as session:
            result = await session.list_tools()
        return self._normalize_tools(result.tools)

    async def inspect_router(self, router_id: str) -> BackendInspection:
        """Fetch the live catalog and the two read-only Block A/B probes in one session."""

        async with self._session() as session:
            listed = await session.list_tools()
            tools = self._normalize_tools(listed.tools)
            names = {tool.name for tool in tools}
            required = {"check_router_health", "get_system_status"}
            missing = sorted(required - names)
            if missing:
                raise RuntimeError(
                    f"MikroMCP catalog is missing required tool(s): {', '.join(missing)}"
                )
            health_raw = await session.call_tool("check_router_health", {"routerId": router_id})
            status_raw = await session.call_tool("get_system_status", {"routerId": router_id})

        return BackendInspection(
            tools=tools,
            health=self._normalize_result(health_raw),
            system_status=self._normalize_result(status_raw),
        )

    @staticmethod
    def _normalize_tools(tools: list[Any]) -> tuple[McpTool, ...]:
        return tuple(
            McpTool(
                name=tool.name,
                description=tool.description,
                input_schema=dict(tool.inputSchema),
                annotations=(
                    tool.annotations.model_dump(exclude_none=True)
                    if tool.annotations is not None
                    else {}
                ),
            )
            for tool in tools
        )

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> McpToolResult:
        async with self._session() as session:
            result = await session.call_tool(name, dict(arguments or {}))

        return self._normalize_result(result)

    @staticmethod
    def _normalize_result(result: Any) -> McpToolResult:
        text_blocks = tuple(
            block.text for block in result.content if getattr(block, "type", None) == "text"
        )
        structured = (
            dict(result.structuredContent) if result.structuredContent is not None else None
        )
        return McpToolResult(
            content=text_blocks,
            structured_content=structured,
            is_error=bool(result.isError),
        )
