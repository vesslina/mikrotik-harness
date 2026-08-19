from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mth.agent.capabilities import (
    ModelCapabilities,
    ProviderKind,
    ProviderPreset,
    ReasoningControl,
    ToolCallFormat,
)
from mth.core.mcp_client.runtime import project_root


@dataclass(frozen=True, slots=True)
class PresetPaths:
    file: Path = field(default_factory=lambda: project_root() / ".mth" / "providers.json")


class ProviderPresetStore:
    def __init__(self, paths: PresetPaths | None = None) -> None:
        self.paths = paths or PresetPaths()

    def list(self) -> tuple[ProviderPreset, ...]:
        document = self._load()
        raw_presets = document.get("presets", {})
        if not isinstance(raw_presets, dict):
            raise ValueError("Invalid provider preset mapping")
        return tuple(
            self._decode(name, value)
            for name, value in sorted(raw_presets.items())
            if isinstance(name, str) and isinstance(value, dict)
        )

    def selected(self) -> ProviderPreset | None:
        document = self._load()
        selected = document.get("selected")
        if not isinstance(selected, str):
            return None
        return next((preset for preset in self.list() if preset.name == selected), None)

    def save(self, preset: ProviderPreset, *, select: bool = True) -> None:
        document = self._load()
        raw_presets = document.setdefault("presets", {})
        if not isinstance(raw_presets, dict):
            raise ValueError("Invalid provider preset mapping")
        raw_presets[preset.name] = self._encode(preset)
        if select:
            document["selected"] = preset.name
        self._atomic_write(document)

    def api_key(self, preset: ProviderPreset) -> str | None:
        return os.environ.get(preset.api_key_env) if preset.api_key_env else None

    def _load(self) -> dict[str, Any]:
        if not self.paths.file.exists():
            return {"version": 1, "selected": None, "presets": {}}
        loaded = json.loads(self.paths.file.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("Provider preset file must contain an object")
        return loaded

    def _atomic_write(self, document: dict[str, Any]) -> None:
        path = self.paths.file
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(document, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    @staticmethod
    def _encode(preset: ProviderPreset) -> dict[str, Any]:
        capabilities = preset.capabilities
        return {
            "provider": preset.provider,
            "base_url": preset.base_url,
            "model": preset.model,
            "api_key_env": preset.api_key_env,
            "allow_sensitive_tool_data": preset.allow_sensitive_tool_data,
            "capabilities": {
                "supports_tools": capabilities.supports_tools,
                "supports_streaming": capabilities.supports_streaming,
                "supports_reasoning": capabilities.supports_reasoning,
                "supports_json_schema": capabilities.supports_json_schema,
                "max_context_tokens": capabilities.max_context_tokens,
                "reasoning_control": capabilities.reasoning_control,
                "tool_call_format": capabilities.tool_call_format,
            },
        }

    @staticmethod
    def _decode(name: str, raw: dict[str, Any]) -> ProviderPreset:
        capabilities = raw.get("capabilities")
        if not isinstance(capabilities, dict):
            raise ValueError(f"Preset {name!r} has no capability matrix")
        allow_sensitive = raw.get("allow_sensitive_tool_data", False)
        if not isinstance(allow_sensitive, bool):
            raise ValueError(
                f"Preset {name!r} has an invalid sensitive-tool-data policy"
            )
        return ProviderPreset(
            name=name,
            provider=ProviderKind(str(raw["provider"])),
            base_url=str(raw["base_url"]),
            model=str(raw["model"]),
            api_key_env=(
                str(raw["api_key_env"]) if raw.get("api_key_env") is not None else None
            ),
            allow_sensitive_tool_data=allow_sensitive,
            capabilities=ModelCapabilities(
                supports_tools=bool(capabilities["supports_tools"]),
                supports_streaming=bool(capabilities["supports_streaming"]),
                supports_reasoning=bool(capabilities["supports_reasoning"]),
                supports_json_schema=bool(capabilities["supports_json_schema"]),
                max_context_tokens=int(capabilities["max_context_tokens"]),
                reasoning_control=ReasoningControl(str(capabilities["reasoning_control"])),
                tool_call_format=ToolCallFormat(str(capabilities["tool_call_format"])),
            ),
        )
