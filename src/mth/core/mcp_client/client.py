from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mth.core.mcp_client.models import BackendInspection, McpTool, McpToolResult
from mth.core.mcp_client.rest_reader import RouterOsRestReader, RouterOsRestReadError
from mth.core.mcp_client.runtime import MikroMcpRuntime

_LIST_IP_ADDRESSES = McpTool(
    name="list_ip_addresses",
    description=(
        "List IPv4 addresses assigned to RouterOS interfaces. This harness-owned read-only "
        "extension fills a missing inspection tool in pinned MikroMCP; it never changes the router."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "routerId": {"type": "string", "description": "Connected RouterOS router ID"},
            "interface": {"type": "string", "description": "Optional exact interface filter"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 500},
        },
        "required": ["routerId"],
        "additionalProperties": False,
    },
    annotations={"readOnlyHint": True, "destructiveHint": False},
)


def _exception_leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for nested in error.exceptions:
            leaves.extend(_exception_leaves(nested))
        return leaves
    return [error]


def unwrap_exception_group(error: BaseExceptionGroup) -> BaseException:
    """Return the useful cause hidden by an AnyIO/MCP task group."""

    leaves = _exception_leaves(error)
    non_cancelled = [leaf for leaf in leaves if not isinstance(leaf, asyncio.CancelledError)]
    candidates = non_cancelled or leaves
    if len(candidates) == 1:
        return candidates[0]
    details = "; ".join(dict.fromkeys(str(candidate) for candidate in candidates))
    return RuntimeError(details or str(error))


class MikroMcpSession:
    """A live MCP session used when safety state must survive several tool calls."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def list_tools(self) -> tuple[McpTool, ...]:
        result = await self._session.list_tools()
        return MikroMcpClient._normalize_tools(result.tools)

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> McpToolResult:
        result = await self._session.call_tool(name, dict(arguments or {}))
        return MikroMcpClient._normalize_result(result)


class MikroMcpClient:
    """Stdio client for short calls or an explicit multi-call live session."""

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
        try:
            async with stdio_client(parameters) as (
                read_stream,
                write_stream,
            ), ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=self._read_timeout),
            ) as session:
                await session.initialize()
                yield session
        except BaseExceptionGroup as error:
            useful_cause = unwrap_exception_group(error)
            raise useful_cause from error

    async def list_tools(self) -> tuple[McpTool, ...]:
        async with self._session() as session:
            result = await session.list_tools()
        tools = self._augment_read_catalog(self._normalize_tools(result.tools))
        return tools

    @staticmethod
    def _augment_read_catalog(tools: tuple[McpTool, ...]) -> tuple[McpTool, ...]:
        if any(tool.name == _LIST_IP_ADDRESSES.name for tool in tools):
            return tools
        return (*tools, _LIST_IP_ADDRESSES)

    @asynccontextmanager
    async def open_session(self) -> AsyncIterator[MikroMcpSession]:
        """Keep one backend process alive across an approval-bound workflow."""

        async with self._session() as session:
            yield MikroMcpSession(session)

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
        raw_arguments = dict(arguments or {})
        if name == _LIST_IP_ADDRESSES.name:
            return await self._list_ip_addresses(raw_arguments)
        async with self._session() as session:
            result = await session.call_tool(name, raw_arguments)

        return self._normalize_result(result)

    async def _list_ip_addresses(self, arguments: Mapping[str, Any]) -> McpToolResult:
        router_id = arguments.get("routerId")
        interface = arguments.get("interface")
        limit = arguments.get("limit", 500)
        if not isinstance(router_id, str) or not router_id:
            return McpToolResult(("routerId is required for list_ip_addresses.",), None, True)
        if interface is not None and not isinstance(interface, str):
            return McpToolResult(("interface must be a string when provided.",), None, True)
        if not isinstance(limit, int) or isinstance(limit, bool):
            return McpToolResult(("limit must be an integer.",), None, True)
        try:
            records = await asyncio.to_thread(
                RouterOsRestReader(self._environment).list_ip_addresses,
                router_id,
                interface=interface,
                limit=limit,
            )
        except RouterOsRestReadError as error:
            return McpToolResult((str(error),), None, True)
        return McpToolResult(
            (f"Listed {len(records)} RouterOS IP address record(s).",),
            {"addresses": records},
            False,
        )

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
