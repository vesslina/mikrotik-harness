import pytest

from mth.agent import (
    AgentMessage,
    CapabilityMismatchError,
    ModelCapabilities,
    ProviderKind,
    ProviderPreset,
    ReasoningControl,
    RiskLevel,
    ToolCall,
    ToolCallFormat,
)


def _capabilities(**overrides) -> ModelCapabilities:
    values = {
        "supports_tools": True,
        "supports_streaming": True,
        "supports_reasoning": False,
        "supports_json_schema": True,
        "max_context_tokens": 32_768,
        "reasoning_control": ReasoningControl.NONE,
        "tool_call_format": ToolCallFormat.OPENAI,
    }
    values.update(overrides)
    return ModelCapabilities(**values)


def test_provider_preset_keeps_only_api_key_environment_reference() -> None:
    preset = ProviderPreset(
        name="lab",
        provider=ProviderKind.OPENAI_COMPATIBLE,
        base_url="http://192.168.56.1:1234/v1",
        model="lab-model",
        api_key_env="MTH_LAB_API_KEY",
        capabilities=_capabilities(),
    )

    preset.require_agent_loop_support()
    assert preset.api_key_env == "MTH_LAB_API_KEY"
    assert not hasattr(preset, "api_key")


def test_model_without_tools_is_rejected_before_agent_loop() -> None:
    capabilities = _capabilities(supports_tools=False)

    with pytest.raises(CapabilityMismatchError, match="typed tool calls"):
        capabilities.require_agent_loop_support()


def test_reasoning_control_cannot_claim_unsupported_reasoning() -> None:
    with pytest.raises(ValueError, match="reasoning_control"):
        _capabilities(reasoning_control=ReasoningControl.BOOLEAN)


def test_normalized_events_have_stable_kind_and_risk() -> None:
    message = AgentMessage("Inspecting interfaces.")
    tool_call = ToolCall(
        call_id="call-1",
        tool_name="list_interfaces",
        arguments={"routerId": "mikrotik-afe23e"},
        risk=RiskLevel.READ_ONLY,
    )

    assert message.kind == "agent_message"
    assert tool_call.kind == "tool_call"
    assert tool_call.risk is RiskLevel.READ_ONLY


def test_sensitive_tool_opt_in_is_rejected_for_non_loopback_endpoint() -> None:
    with pytest.raises(ValueError, match="loopback"):
        ProviderPreset(
            name="remote",
            provider=ProviderKind.OPENAI_COMPATIBLE,
            base_url="http://192.168.56.1:1234/v1",
            model="remote-model",
            allow_sensitive_tool_data=True,
            capabilities=_capabilities(),
        )
