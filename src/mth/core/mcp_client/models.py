from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class McpTool:
    name: str
    description: str | None
    input_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class McpToolResult:
    content: tuple[str, ...]
    structured_content: dict[str, Any] | None
    is_error: bool

    @property
    def text(self) -> str:
        return "\n".join(self.content)


@dataclass(frozen=True, slots=True)
class BackendInspection:
    tools: tuple[McpTool, ...]
    health: McpToolResult
    system_status: McpToolResult
