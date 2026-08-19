from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse


class ProviderKind(StrEnum):
    LM_STUDIO = "lm_studio"
    OPENROUTER = "openrouter"
    OPENAI_COMPATIBLE = "openai_compatible"


class ReasoningControl(StrEnum):
    NONE = "none"
    BOOLEAN = "boolean"
    LEVELS = "levels"


class ToolCallFormat(StrEnum):
    OPENAI = "openai"
    CUSTOM = "custom"


class CapabilityMismatchError(ValueError):
    """The selected model cannot satisfy the current agent loop contract."""


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    supports_tools: bool
    supports_streaming: bool
    supports_reasoning: bool
    supports_json_schema: bool
    max_context_tokens: int
    reasoning_control: ReasoningControl
    tool_call_format: ToolCallFormat

    def __post_init__(self) -> None:
        if self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive")
        if not self.supports_reasoning and self.reasoning_control is not ReasoningControl.NONE:
            raise ValueError("reasoning_control requires supports_reasoning")

    def require_agent_loop_support(self) -> None:
        missing: list[str] = []
        if not self.supports_tools:
            missing.append("typed tool calls")
        if self.tool_call_format is not ToolCallFormat.OPENAI:
            missing.append("OpenAI-format tool calls")
        if missing:
            raise CapabilityMismatchError(
                "Selected model is incompatible with the current agent loop: "
                + ", ".join(missing)
            )


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    """Serializable provider metadata; the API key value is deliberately absent."""

    name: str
    provider: ProviderKind
    base_url: str
    model: str
    capabilities: ModelCapabilities
    api_key_env: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("preset name must not be empty")
        if not self.model.strip():
            raise ValueError("model name must not be empty")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if self.api_key_env is not None and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env
        ):
            raise ValueError("api_key_env must be an environment variable name")

    def require_agent_loop_support(self) -> None:
        self.capabilities.require_agent_loop_support()
