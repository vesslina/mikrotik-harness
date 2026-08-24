import asyncio
import json
from dataclasses import replace
from unittest.mock import patch

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
    ProviderStreamChunk,
    ProviderToolCall,
    ProviderWarmup,
    ReadOnlyAgentLoop,
    ReasoningControl,
    ReasoningDelta,
    ReasoningStatus,
    RunbookProposal,
    ToolCallFormat,
    ToolResult,
)
from mth.core.mcp_client.models import McpTool, McpToolResult
from mth.core.runbooks import (
    RunbookApplyResult,
    RunbookPlan,
    RunbookStep,
    RunbookVerification,
)
from mth.rag import FieldPack, RagHit, load_or_build


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
        write = {"readOnlyHint": False, "destructiveHint": True}
        return (
            McpTool("list_interfaces", "Read interfaces", schema, read_only),
            McpTool("manage_bridge", "Write bridge", schema, write),
            McpTool("manage_bridge_port", "Write bridge port", schema, write),
            McpTool("manage_pppoe_client", "Write PPPoE client", schema, write),
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
        progress = []
        loop.set_progress_sink(progress.append)

        events = await loop.run("Show interfaces", AgentMode.READY)

        assert len(provider.tool_names) == 2
        assert "list_interfaces" in provider.tool_names[0]
        assert "propose_lan_bridge" in provider.tool_names[0]
        assert provider.tool_names[0] == provider.tool_names[1]
        assert backend.arguments == {"routerId": "mikrotik-afe23e"}
        assert backend.catalog_calls == 1
        assert any(isinstance(event, PlannedAction) for event in events)
        assert any(isinstance(event, AgentMessage) for event in events)
        assert isinstance(events[-1], FinalSummary)
        assert any(isinstance(event, PlannedAction) for event in progress)
        assert any(isinstance(event, ToolResult) for event in progress)
        assert not any(isinstance(event, AgentMessage) for event in progress)

    asyncio.run(scenario())


def test_plan_mode_exposes_all_live_read_tools() -> None:
    async def scenario() -> None:
        backend = _Backend()

        class Provider:
            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, messages, tools=()) -> ProviderReply:
                self.calls += 1
                assert tuple(tool.name for tool in tools) == ("list_interfaces",)
                if self.calls == 1:
                    return ProviderReply(
                        "Inspect interfaces.",
                        (ProviderToolCall("read-1", "list_interfaces", {}),),
                    )
                return ProviderReply("Here is a read-only diagnostic plan.", ())

        provider = Provider()
        loop = ReadOnlyAgentLoop(
            preset=_preset(),
            provider=provider,
            backend=backend,
            router_id="mikrotik-afe23e",
        )

        events = await loop.run("Plan diagnostics", AgentMode.PLAN)

        assert any(isinstance(event, AgentMessage) for event in events)
        assert backend.arguments == {"routerId": "mikrotik-afe23e"}
        assert backend.catalog_calls == 1

    asyncio.run(scenario())


def test_agent_keeps_bounded_conversation_history_and_can_clear_it() -> None:
    async def scenario() -> None:
        class Provider:
            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, messages, tools=()) -> ProviderReply:
                self.calls += 1
                contents = [message.get("content") for message in messages]
                if self.calls == 1:
                    assert "Remember bridge-lan" in contents
                    return ProviderReply("I will remember bridge-lan.", ())
                if self.calls == 2:
                    assert "Remember bridge-lan" in contents
                    assert "I will remember bridge-lan." in contents
                    return ProviderReply("The remembered name is bridge-lan.", ())
                assert "Remember bridge-lan" not in contents
                return ProviderReply("No prior context remains.", ())

        provider = Provider()
        loop = ReadOnlyAgentLoop(
            preset=_preset(),
            provider=provider,
            backend=_Backend(),
            router_id="mikrotik-afe23e",
        )

        await loop.run("Remember bridge-lan", AgentMode.PLAN)
        await loop.run("What name did I give you?", AgentMode.PLAN)
        loop.clear_history()
        await loop.run("What do you remember?", AgentMode.PLAN)

    asyncio.run(scenario())


def test_provider_warmup_uses_hidden_tool_free_probe() -> None:
    async def scenario() -> None:
        class Provider:
            async def complete(self, messages, tools=()) -> ProviderReply:
                assert not tools
                assert messages[-1]["content"] == "Are you there? Reply only OK."
                return ProviderReply("OK", ())

        loop = ReadOnlyAgentLoop(
            preset=_preset(),
            provider=Provider(),
            backend=_Backend(),
            router_id="mikrotik-afe23e",
        )

        result = await loop.warm_up()

        assert isinstance(result, ProviderWarmup)
        assert result.response == "OK"
        assert result.latency_ms >= 0

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


def test_openai_compatible_client_streams_sse_reasoning_and_content() -> None:
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> bool:
            return False

        def __iter__(self):
            return iter(
                (
                    'data: {"choices":[{"delta":{"reasoning_content":"дум"}}]}\n'.encode(),
                    'data: {"choices":[{"delta":{"content":"Привет"}}]}\n'.encode(),
                    b"data: [DONE]\n",
                )
            )

    def open_url(request, timeout):
        captured["stream"] = json.loads(request.data.decode("utf-8"))["stream"]
        captured["accept"] = request.headers["Accept"]
        captured["timeout"] = timeout
        return Response()

    async def scenario() -> list[ProviderStreamChunk]:
        client = OpenAICompatibleClient(
            base_url="http://localhost:1234/v1",
            model="test-model",
        )
        return [
            chunk
            async for chunk in client.stream([{"role": "user", "content": "Привет"}])
        ]

    with patch("mth.agent.providers.urllib.request.urlopen", open_url):
        chunks = asyncio.run(scenario())

    assert captured == {"stream": True, "accept": "text/event-stream", "timeout": 60.0}
    assert [chunk.reasoning for chunk in chunks if chunk.reasoning] == ["дум"]
    assert [chunk.content for chunk in chunks if chunk.content] == ["Привет"]
    assert chunks[-1].reply is not None
    assert chunks[-1].reply.content == "Привет"
    assert chunks[-1].reply.reasoning == "дум"


def test_openai_compatible_client_parses_lm_studio_reasoning_content() -> None:
    def transport(url, headers, body, timeout) -> bytes:
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning_content": "Thinking\n\nFinal Answer:\nПривет!",
                            "tool_calls": [],
                        }
                    }
                ],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 18}},
            }
        ).encode()

    client = OpenAICompatibleClient(
        base_url="http://localhost:1234/v1",
        model="qwen3.5-4b",
        transport=transport,
    )

    reply = asyncio.run(client.complete([{"role": "user", "content": "Привет"}]))

    assert reply.content == ""
    assert reply.reasoning.endswith("Привет!")
    assert reply.reasoning_tokens == 18


def test_loop_recovers_labelled_final_answer_from_reasoning_only_reply() -> None:
    async def scenario() -> None:
        class Provider:
            async def complete(self, messages, tools=()) -> ProviderReply:
                return ProviderReply(
                    content="",
                    tool_calls=(),
                    reasoning=(
                        "Thinking Process:\ninternal notes\n\n"
                        "6. **Output Generation** (in Russian):\n"
                        "    Привет! Да, я здесь."
                    ),
                    reasoning_tokens=42,
                )

        loop = ReadOnlyAgentLoop(
            preset=_preset(),
            provider=Provider(),
            backend=_Backend(),
            router_id="mikrotik-afe23e",
        )

        events = await loop.run("Привет", AgentMode.PLAN)

        assert isinstance(events[0], ReasoningStatus)
        assert events[0].token_count == 42
        assert events[0].recovered_final_answer is True
        assert isinstance(events[1], AgentMessage)
        assert events[1].text == "Привет! Да, я здесь."

    asyncio.run(scenario())


def test_loop_emits_streamed_reasoning_to_progress_sink_without_polluting_history() -> None:
    async def scenario() -> None:
        class Provider:
            async def complete(self, messages, tools=()) -> ProviderReply:
                raise AssertionError("streaming provider should not use complete")

            async def stream(self, messages, tools=()):
                yield ProviderStreamChunk(reasoning="проверяю ")
                yield ProviderStreamChunk(reasoning="состояние")
                yield ProviderStreamChunk(reply=ProviderReply("Готово.", ()))

        provider = Provider()
        loop = ReadOnlyAgentLoop(
            preset=replace(
                _preset(),
                capabilities=replace(_preset().capabilities, supports_streaming=True),
            ),
            provider=provider,
            backend=_Backend(),
            router_id="mikrotik-afe23e",
        )
        progress = []
        loop.set_progress_sink(progress.append)
        events = await loop.run("Проверь состояние", AgentMode.PLAN)

        assert [event.text for event in progress if isinstance(event, ReasoningDelta)] == [
            "проверяю ",
            "состояние",
        ]
        assert not any(isinstance(event, ReasoningDelta) for event in events)
        assert any(isinstance(event, AgentMessage) and event.text == "Готово." for event in events)

    asyncio.run(scenario())


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


def test_tool_secrets_are_redacted_before_events_and_model_context() -> None:
    async def scenario() -> None:
        class Backend(_Backend):
            async def call_tool(self, name, arguments=None) -> McpToolResult:
                self.arguments = arguments
                return McpToolResult(
                    ("password: 12345",),
                    {
                        "clients": [
                            {
                                "name": "pppoe-wan",
                                "password": "12345",
                                "nested": {"private-key": "secret-key"},
                            }
                        ]
                    },
                    False,
                )

        class Provider(_Provider):
            def __init__(self) -> None:
                super().__init__()
                self.seen_messages = ()

            async def complete(self, messages, tools=()) -> ProviderReply:
                self.seen_messages = messages
                return await super().complete(messages, tools)

        provider = Provider()
        loop = ReadOnlyAgentLoop(
            preset=_preset(),
            provider=provider,
            backend=Backend(),
            router_id="mikrotik-afe23e",
        )

        events = await loop.run("Show PPPoE", AgentMode.READY)

        serialized = json.dumps(provider.seen_messages, ensure_ascii=False)
        assert "12345" not in serialized
        assert "secret-key" not in serialized
        assert "[REDACTED]" in serialized
        result = next(
            event
            for event in events
            if isinstance(event, ToolResult) and event.tool_name == "list_interfaces"
        )
        assert result.structured_content is not None
        assert result.structured_content["clients"][0]["password"] == "[REDACTED]"

    asyncio.run(scenario())


def test_loopback_opt_in_can_expose_sensitive_tool_data() -> None:
    async def scenario() -> None:
        class Backend(_Backend):
            async def call_tool(self, name, arguments=None) -> McpToolResult:
                return McpToolResult((), {"password": "local-secret"}, False)

        class Provider(_Provider):
            def __init__(self) -> None:
                super().__init__()
                self.seen_messages = ()

            async def complete(self, messages, tools=()) -> ProviderReply:
                self.seen_messages = messages
                return await super().complete(messages, tools)

        provider = Provider()
        loop = ReadOnlyAgentLoop(
            preset=replace(_preset(), allow_sensitive_tool_data=True),
            provider=provider,
            backend=Backend(),
            router_id="mikrotik-afe23e",
        )

        await loop.run("Show PPPoE", AgentMode.READY)

        assert "local-secret" in json.dumps(provider.seen_messages)

    asyncio.run(scenario())


def test_pppoe_intent_becomes_harness_proposal_without_backend_write() -> None:
    async def scenario() -> None:
        class Provider:
            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, messages, tools=()) -> ProviderReply:
                self.calls += 1
                assert "propose_wan_pppoe" in {tool.name for tool in tools}
                return ProviderReply(
                    "",
                    (
                        ProviderToolCall(
                            "proposal-1",
                            "propose_wan_pppoe",
                            {
                                "interface": "ether2",
                                "username": "isp-user",
                                "password": "must-be-ignored",
                            },
                        ),
                    ),
                )

        backend = _Backend()
        loop = ReadOnlyAgentLoop(
            preset=_preset(),
            provider=Provider(),
            backend=backend,
            router_id="mikrotik-afe23e",
        )

        events = await loop.run("Configure PPPoE on ether2", AgentMode.READY)

        proposal = next(event for event in events if isinstance(event, RunbookProposal))
        assert proposal.runbook == "wan_pppoe"
        assert proposal.parameters["interface"] == "ether2"
        assert proposal.parameters["username"] == "isp-user"
        assert "password" not in proposal.parameters
        assert backend.arguments is None

    asyncio.run(scenario())


def test_ready_loop_turns_live_write_schema_into_typed_proposal() -> None:
    async def scenario() -> None:
        class Backend:
            async def list_tools(self):
                return (McpTool(
                    "manage_route",
                    "Manage a static route.",
                    {
                        "type": "object",
                        "properties": {
                            "routerId": {"type": "string"},
                            "action": {"type": "string"},
                            "dstAddress": {"type": "string"},
                            "gateway": {"type": "string"},
                            "dryRun": {"type": "boolean"},
                        },
                        "required": ["routerId", "action", "dstAddress", "gateway"],
                    },
                    {"readOnlyHint": False, "destructiveHint": True},
                ),)

            async def call_tool(self, name, arguments=None):
                raise AssertionError("A proposal must not call the backend write tool")

        class Provider:
            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, messages, tools=()):
                self.calls += 1
                names = {tool.name for tool in tools}
                assert "manage_route" not in names
                assert "propose_typed_manage_route" in names
                proposal = next(
                    tool for tool in tools if tool.name == "propose_typed_manage_route"
                )
                assert "routerId" not in proposal.input_schema["properties"]
                return ProviderReply(
                    "",
                    (
                        ProviderToolCall(
                            "proposal-1",
                            "propose_typed_manage_route",
                            {
                                "action": "add",
                                "dstAddress": "10.20.0.0/16",
                                "gateway": "192.0.2.1",
                            },
                        ),
                    ),
                )

        events = await ReadOnlyAgentLoop(
            preset=_preset(),
            provider=Provider(),
            backend=Backend(),
            router_id="mikrotik-afe23e",
        ).run("Add a route", AgentMode.READY)

        proposal = next(event for event in events if isinstance(event, RunbookProposal))
        assert proposal.runbook == "typed:manage_route"
        assert proposal.parameters["gateway"] == "192.0.2.1"

    asyncio.run(scenario())


def test_agent_writes_short_report_after_approved_change() -> None:
    async def scenario() -> None:
        class Provider:
            async def complete(self, messages, tools=()):
                assert not tools
                evidence = json.loads(messages[-1]["content"])
                assert evidence["status"] == "verified"
                assert evidence["rollbackAvailable"] is True
                return ProviderReply(
                    "Маршрут добавлен и проверен. При необходимости доступен rollback.", ()
                )

        loop = ReadOnlyAgentLoop(
            preset=_preset(),
            provider=Provider(),
            backend=_Backend(),
            router_id="mikrotik-afe23e",
        )
        plan = RunbookPlan(
            plan_id="route-1",
            runbook_id="typed:manage_route",
            title="Manage route",
            values={},
            baseline={},
            steps=(RunbookStep("manage_route", {"action": "add"}),),
            preview="dry run",
            summary="Добавить маршрут 10.20.0.0/16.",
        )
        result = RunbookApplyResult(
            journal_ids=("journal-1",),
            verification=RunbookVerification(True, "Route is present."),
            backend_summary="Applied.",
        )

        events = await loop.report_change(plan, result)

        assert isinstance(events[0], AgentMessage)
        assert "добавлен" in events[0].text

    asyncio.run(scenario())


def test_high_risk_executes_raw_mcp_writes_and_persistent_ssh_without_proposal() -> None:
    async def scenario() -> None:
        class Backend:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object] | None]] = []

            async def list_tools(self) -> tuple[McpTool, ...]:
                schema = {"type": "object", "properties": {"routerId": {"type": "string"}}}
                return (
                    McpTool("manage_ip_address", None, schema, {"readOnlyHint": False}),
                    McpTool("list_ip_addresses", None, schema, {"readOnlyHint": True}),
                )

            async def call_tool(self, name, arguments=None) -> McpToolResult:
                self.calls.append((name, dict(arguments) if arguments else None))
                return McpToolResult(("address added",), {"changed": True}, False)

        class Executor:
            def __init__(self) -> None:
                self.commands: list[tuple[str, int, int]] = []

            async def execute(self, command, timeout_seconds=20, max_output_bytes=65_536):
                self.commands.append((command, timeout_seconds, max_output_bytes))
                return McpToolResult(("ether1",), {"status": "ok"}, False)

        class Provider:
            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, messages, tools=()):
                self.calls += 1
                names = {tool.name for tool in tools}
                assert {"manage_ip_address", "ssh_exec", "propose_typed_manage_ip_address"} <= names
                if self.calls == 1:
                    return ProviderReply(
                        "I will make and verify the requested change.",
                        (
                            ProviderToolCall(
                                "write-1",
                                "manage_ip_address",
                                {"action": "add", "address": "10.0.0.1/24"},
                            ),
                            ProviderToolCall(
                                "ssh-1",
                                "ssh_exec",
                                {"command": "/interface print"},
                            ),
                        ),
                    )
                return ProviderReply("Изменение выполнено и проверено.", ())

        backend = Backend()
        executor = Executor()
        loop = ReadOnlyAgentLoop(
            preset=_preset(),
            provider=Provider(),
            backend=backend,
            router_id="mikrotik-afe23e",
        )
        loop.set_high_risk_executor(executor)

        events = await loop.run("Add an address", AgentMode.HIGH_RISK)

        assert backend.calls == [
            (
                "manage_ip_address",
                {
                    "action": "add",
                    "address": "10.0.0.1/24",
                    "routerId": "mikrotik-afe23e",
                },
            )
        ]
        assert executor.commands == [("/interface print", 20, 65_536)]
        assert not any(isinstance(event, RunbookProposal) for event in events)
        assert any(isinstance(event, ToolResult) for event in events)

    asyncio.run(scenario())


def test_high_risk_routes_harness_proposals_locally() -> None:
    async def scenario() -> None:
        schema = {
            "type": "object",
            "properties": {
                "routerId": {"type": "string"},
                "action": {"type": "string", "enum": ["add", "update", "remove"]},
                "address": {"type": "string"},
                "interface": {"type": "string"},
                "dryRun": {"type": "boolean"},
            },
            "required": ["routerId", "action", "address", "interface"],
        }

        class Backend:
            async def list_tools(self) -> tuple[McpTool, ...]:
                return (McpTool("manage_ip_address", "Manage IP", schema, {}),)

            async def call_tool(self, name, arguments=None) -> McpToolResult:
                raise AssertionError(f"harness proposal leaked to MikroMCP: {name}")

        class Provider:
            async def complete(self, messages, tools=()):
                names = {tool.name for tool in tools}
                assert {"propose_ip_address", "propose_typed_manage_ip_address"} <= names
                return ProviderReply(
                    "I will open the requested preview.",
                    (
                        ProviderToolCall(
                            "proposal-1",
                            "propose_typed_manage_ip_address",
                            {
                                "action": "add",
                                "address": "145.145.3.3/24",
                                "interface": "ether3",
                            },
                        ),
                    ),
                )

        class Executor:
            async def execute(self, *_args, **_kwargs):
                raise AssertionError("proposal must not use SSH")

        loop = ReadOnlyAgentLoop(
            preset=_preset(),
            provider=Provider(),
            backend=Backend(),
            router_id="mikrotik-afe23e",
        )
        loop.set_high_risk_executor(Executor())

        events = await loop.run("Preview an address change", AgentMode.HIGH_RISK)

        proposals = [event for event in events if isinstance(event, RunbookProposal)]
        assert len(proposals) == 1
        assert proposals[0].runbook == "typed:manage_ip_address"

    asyncio.run(scenario())


def test_high_risk_owns_router_and_confirmation_arguments() -> None:
    async def scenario() -> None:
        schema = {
            "type": "object",
            "properties": {
                "routerId": {"type": "string"},
                "confirmationToken": {"type": "string"},
                "action": {"type": "string", "enum": ["add"]},
                "address": {"type": "string"},
                "interface": {"type": "string"},
            },
            "required": [
                "routerId",
                "confirmationToken",
                "action",
                "address",
                "interface",
            ],
        }

        class Backend:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def list_tools(self) -> tuple[McpTool, ...]:
                return (
                    McpTool(
                        "manage_ip_address",
                        "Manage IP",
                        schema,
                        {"readOnlyHint": False, "destructiveHint": False},
                    ),
                )

            async def call_tool(self, name, arguments=None) -> McpToolResult:
                assert name == "manage_ip_address"
                call = dict(arguments or {})
                self.calls.append(call)
                if len(self.calls) == 1:
                    assert "confirmationToken" not in call
                    return McpToolResult(
                        ("Confirmation required",),
                        {
                            "code": "FLEET_CONFIRMATION_REQUIRED",
                            "details": {"confirmationToken": "token-1"},
                        },
                        True,
                    )
                assert call["confirmationToken"] == "token-1"
                return McpToolResult(("address added",), {"changed": True}, False)

        class Provider:
            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, messages, tools=()):
                self.calls += 1
                if self.calls == 1:
                    tool = next(tool for tool in tools if tool.name == "manage_ip_address")
                    properties = tool.input_schema["properties"]
                    required = tool.input_schema["required"]
                    assert "routerId" not in properties
                    assert "confirmationToken" not in properties
                    assert "routerId" not in required
                    assert "confirmationToken" not in required
                    return ProviderReply(
                        "I will apply the requested address.",
                        (
                            ProviderToolCall(
                                "write-1",
                                "manage_ip_address",
                                {
                                    "action": "add",
                                    "address": "145.145.3.3/24",
                                    "interface": "ether3",
                                    "confirmationToken": "model-controlled-token",
                                },
                            ),
                        ),
                    )
                return ProviderReply("Адрес добавлен и результат проверен.", ())

        class Executor:
            async def execute(self, *_args, **_kwargs):
                raise AssertionError("structured write must not use SSH")

        backend = Backend()
        loop = ReadOnlyAgentLoop(
            preset=_preset(),
            provider=Provider(),
            backend=backend,
            router_id="mikrotik-afe23e",
        )
        loop.set_high_risk_executor(Executor())

        events = await loop.run("Add the address", AgentMode.HIGH_RISK)

        expected = {
            "action": "add",
            "address": "145.145.3.3/24",
            "interface": "ether3",
            "routerId": "mikrotik-afe23e",
        }
        assert backend.calls == [expected, {**expected, "confirmationToken": "token-1"}]
        assert any(isinstance(event, ToolResult) and not event.is_error for event in events)

    asyncio.run(scenario())


def test_high_risk_can_search_the_local_routeros_pack(tmp_path) -> None:
    index_url = "https://manual.example/llms.txt"
    pages = {
        index_url: "- [Safe Mode](safe-mode.md)\n",
        "https://manual.example/safe-mode.md": (
            "# Safe Mode\n\nSafe Mode rolls configuration changes back when the session drops."
        ),
    }
    pack = load_or_build(
        tmp_path / "rag",
        index_url=index_url,
        fetcher=pages.__getitem__,
        max_chunk_chars=256,
    )

    async def scenario() -> None:
        class Provider:
            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, messages, tools=()):
                self.calls += 1
                names = {tool.name for tool in tools}
                assert "search_routeros_docs" in names
                if self.calls == 1:
                    return ProviderReply(
                        "I will verify the behavior in local documentation.",
                        (
                            ProviderToolCall(
                                "rag-1",
                                "search_routeros_docs",
                                {"query": "safe mode rollback", "limit": 2},
                            ),
                        ),
                    )
                result = json.loads(messages[-1]["content"])
                assert result["trust"] == "untrusted_reference"
                assert "session drops" in result["hits"][0]["text"]
                return ProviderReply("Документация подтверждает поведение Safe Mode.", ())

        class Executor:
            async def execute(self, *_args, **_kwargs):
                raise AssertionError("documentation search must not use SSH")

        loop = ReadOnlyAgentLoop(
            preset=_preset(),
            provider=Provider(),
            backend=_Backend(),
            router_id="mikrotik-afe23e",
            rag_pack=pack,
            routeros_version="7.21.5",
        )
        loop.set_high_risk_executor(Executor())

        events = await loop.run("Проверь поведение Safe Mode", AgentMode.HIGH_RISK)

        rag_results = [
            event
            for event in events
            if isinstance(event, ToolResult) and event.tool_name == "search_routeros_docs"
        ]
        assert len(rag_results) == 1
        assert rag_results[0].is_error is False

    asyncio.run(scenario())


def test_routeros_documentation_results_are_context_bounded() -> None:
    class Pack:
        manifest = {"created_at": "2026-08-24T00:00:00Z"}

        def search(self, _query: str, *, limit: int = 5):
            return tuple(
                RagHit("x" * 5_000, f"Heading {index}", "https://example.test", "page.md", 1.0)
                for index in range(limit)
            )

    loop = ReadOnlyAgentLoop(
        preset=_preset(),
        provider=_Provider(),
        backend=_Backend(),
        router_id="mikrotik-afe23e",
        rag_pack=Pack(),  # type: ignore[arg-type]
    )

    result = loop._search_routeros_docs({"query": "routeros", "limit": 5})
    hits = result.structured_content["hits"]

    assert sum(len(hit["text"]) for hit in hits) <= loop.MAX_RAG_CONTEXT_CHARS


def test_high_risk_exposes_local_field_recipe_collection(tmp_path) -> None:
    recipe_dir = tmp_path / "field-recipes"
    recipe_dir.mkdir()
    (recipe_dir / "recipe.md").write_text(
        """---
kind: field_recipe
collection: rag2b_field
id: cpe
device_models: [SXTsq Lite5]
---
# CPE
Use the reviewed CPE path.
""",
        encoding="utf-8",
    )

    class Backend:
        async def list_tools(self) -> tuple[McpTool, ...]:
            return (
                McpTool(
                    "list_interfaces",
                    "Read interfaces",
                    {"type": "object", "properties": {}},
                    {"readOnlyHint": True, "destructiveHint": False},
                ),
            )

    class Executor:
        async def execute(self, *_args, **_kwargs) -> McpToolResult:
            return McpToolResult(("ok",), {"status": "ok"}, False)

    class Provider:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, _messages, tools=()) -> ProviderReply:
            self.calls += 1
            assert "search_field_recipes" in {tool.name for tool in tools}
            if self.calls == 1:
                return ProviderReply(
                    "Сначала загружу профиль.",
                    (
                        ProviderToolCall(
                            "select-1",
                            "select_router_capabilities",
                            {"domains": ["interfaces"]},
                        ),
                    ),
                )
            assert any(
                "SXTsq Lite5" in str(message.get("content"))
                for message in _messages
                if message.get("role") == "tool"
            )
            return ProviderReply("Карточка доступна локально.", ())

    loop = ReadOnlyAgentLoop(
        preset=_preset(),
        provider=Provider(),
        backend=Backend(),
        router_id="mikrotik-afe23e",
        field_pack=FieldPack.load(recipe_dir),
        device_model="SXTsq Lite5",
    )
    loop.set_high_risk_executor(Executor())

    events = asyncio.run(loop.run("Настрой SXTsq Lite5 CPE", AgentMode.HIGH_RISK))

    assert any(isinstance(event, FinalSummary) for event in events)
