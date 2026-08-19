import asyncio
import json

from mth.agent import (
    AgentMessage,
    AgentMode,
    FinalSummary,
    ModelCapabilities,
    OpenAICompatibleClient,
    PlannedAction,
    ProviderKind,
    ProviderPreset,
    ProviderReply,
    ProviderToolCall,
    ReadOnlyAgentLoop,
    ReasoningControl,
    ToolCallFormat,
)
from mth.core.mcp_client.models import McpTool, McpToolResult


def _preset() -> ProviderPreset:
    return ProviderPreset(
        name="test",
        provider=ProviderKind.OPENAI_COMPATIBLE,
        base_url="http://127.0.0.1:1234/v1",
        model="test-model",
        capabilities=ModelCapabilities(
            supports_tools=True,
            supports_streaming=False,
            supports_reasoning=False,
            supports_json_schema=True,
            max_context_tokens=32_768,
            reasoning_control=ReasoningControl.NONE,
            tool_call_format=ToolCallFormat.OPENAI,
        ),
    )


class _Backend:
    def __init__(self) -> None:
        self.arguments = None
        self.catalog_calls = 0

    async def list_tools(self) -> tuple[McpTool, ...]:
        self.catalog_calls += 1
        schema = {"type": "object", "properties": {"routerId": {"type": "string"}}}
        read_only = {"readOnlyHint": True, "destructiveHint": False}
        return (
            McpTool("list_interfaces", "Read interfaces", schema, read_only),
            McpTool("manage_bridge", "Write bridge", schema, read_only),
            McpTool("run_command", "Raw escape hatch", schema, read_only),
        )

    async def call_tool(self, name, arguments=None) -> McpToolResult:
        assert name == "list_interfaces"
        self.arguments = arguments
        return McpToolResult(("ether1",), {"interfaces": ["ether1"]}, False)


class _Provider:
    def __init__(self) -> None:
        self.calls = 0
        self.tool_names: list[tuple[str, ...]] = []

    async def complete(self, messages, tools=()) -> ProviderReply:
        self.tool_names.append(tuple(tool.name for tool in tools))
        self.calls += 1
        if self.calls == 1:
            return ProviderReply(
                "I will inspect interfaces.",
                (
                    ProviderToolCall(
                        "call-1",
                        "list_interfaces",
                        {"routerId": "attacker-selected-router"},
                    ),
                ),
            )
        return ProviderReply("The router has one interface: ether1.", ())


def test_ready_loop_filters_catalog_and_binds_connected_router() -> None:
    async def scenario() -> None:
        backend = _Backend()
        provider = _Provider()
        loop = ReadOnlyAgentLoop(
            preset=_preset(),
            provider=provider,
            backend=backend,
            router_id="mikrotik-afe23e",
        )

        events = await loop.run("Show interfaces", AgentMode.READY)

        assert provider.tool_names == [("list_interfaces",), ("list_interfaces",)]
        assert backend.arguments == {"routerId": "mikrotik-afe23e"}
        assert backend.catalog_calls == 1
        assert any(isinstance(event, PlannedAction) for event in events)
        assert any(isinstance(event, AgentMessage) for event in events)
        assert isinstance(events[-1], FinalSummary)

    asyncio.run(scenario())


def test_plan_mode_exposes_no_tools() -> None:
    async def scenario() -> None:
        backend = _Backend()

        class Provider:
            async def complete(self, messages, tools=()) -> ProviderReply:
                assert not tools
                return ProviderReply("Here is a read-only diagnostic plan.", ())

        loop = ReadOnlyAgentLoop(
            preset=_preset(),
            provider=Provider(),
            backend=backend,
            router_id="mikrotik-afe23e",
        )

        events = await loop.run("Plan diagnostics", AgentMode.PLAN)

        assert isinstance(events[0], AgentMessage)
        assert backend.arguments is None

    asyncio.run(scenario())


def test_openai_compatible_client_sends_tools_and_parses_tool_call() -> None:
    captured = {}

    def transport(url, headers, body, timeout) -> bytes:
        captured.update(
            url=url,
            headers=dict(headers),
            body=json.loads(body.decode("utf-8")),
            timeout=timeout,
        )
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-7",
                                    "function": {
                                        "name": "get_system_status",
                                        "arguments": '{"routerId":"router"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ).encode()

    client = OpenAICompatibleClient(
        base_url="http://localhost:1234/v1/",
        model="test-model",
        api_key="memory-only-secret",
        transport=transport,
    )
    reply = asyncio.run(
        client.complete(
            [{"role": "user", "content": "status"}],
            [McpTool("get_system_status", "Status", {"type": "object"})],
        )
    )

    assert captured["url"] == "http://localhost:1234/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer memory-only-secret"
    assert captured["body"]["tools"][0]["function"]["name"] == "get_system_status"
    assert reply.tool_calls[0].name == "get_system_status"
    assert reply.tool_calls[0].arguments == {"routerId": "router"}


def test_read_only_filter_never_exposes_write_or_raw_command() -> None:
    schema = {"type": "object", "properties": {"routerId": {"type": "string"}}}
    global_schema = {"type": "object", "properties": {"tags": {"type": "array"}}}
    read_only = {"readOnlyHint": True, "destructiveHint": False}
    tools = (
        McpTool("get_log", None, schema, read_only),
        McpTool("list_bridges", None, schema, read_only),
        McpTool("list_routers", None, global_schema, read_only),
        McpTool("manage_bridge", None, schema, read_only),
        McpTool("apply_plan", None, schema, read_only),
        McpTool("run_command", None, schema, read_only),
    )

    filtered = ReadOnlyAgentLoop.filter_read_only_tools(tools)

    assert tuple(tool.name for tool in filtered) == ("get_log", "list_bridges")
