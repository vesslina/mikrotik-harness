import json

from mth.agent import (
    ModelCapabilities,
    PresetPaths,
    ProviderKind,
    ProviderPreset,
    ProviderPresetStore,
    ReasoningControl,
    ToolCallFormat,
)


def test_preset_store_never_persists_api_key_value(tmp_path) -> None:
    path = tmp_path / "providers.json"
    store = ProviderPresetStore(PresetPaths(file=path))
    preset = ProviderPreset(
        name="openrouter",
        provider=ProviderKind.OPENROUTER,
        base_url="https://openrouter.ai/api/v1",
        model="vendor/model",
        api_key_env="OPENROUTER_API_KEY",
        capabilities=ModelCapabilities(
            supports_tools=True,
            supports_streaming=True,
            supports_reasoning=False,
            supports_json_schema=True,
            max_context_tokens=64_000,
            reasoning_control=ReasoningControl.NONE,
            tool_call_format=ToolCallFormat.OPENAI,
        ),
    )

    store.save(preset)

    raw = path.read_text(encoding="utf-8")
    document = json.loads(raw)
    assert document["selected"] == "openrouter"
    assert "memory-only-secret" not in raw
    assert "api_key_env" in raw
    assert store.selected() == preset


def test_old_preset_defaults_sensitive_tool_data_to_protected(tmp_path) -> None:
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "selected": "old-local",
                "presets": {
                    "old-local": {
                        "provider": "lm_studio",
                        "base_url": "http://127.0.0.1:1234/v1",
                        "model": "qwen",
                        "api_key_env": None,
                        "capabilities": {
                            "supports_tools": True,
                            "supports_streaming": False,
                            "supports_reasoning": False,
                            "supports_json_schema": False,
                            "max_context_tokens": 32768,
                            "reasoning_control": "none",
                            "tool_call_format": "openai",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    selected = ProviderPresetStore(PresetPaths(file=path)).selected()

    assert selected is not None
    assert selected.allow_sensitive_tool_data is False
