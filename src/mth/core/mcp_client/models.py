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

    @property
    def confirmation_token(self) -> str | None:
        structured = self.structured_content or {}
        details = structured.get("details")
        token = details.get("confirmationToken") if isinstance(details, dict) else None
        return (
            token
            if self.is_error
            and structured.get("code")
            in {"CONFIRMATION_REQUIRED", "FLEET_CONFIRMATION_REQUIRED"}
            and isinstance(token, str)
            and token
            else None
        )


@dataclass(frozen=True, slots=True)
class BackendInspection:
    tools: tuple[McpTool, ...]
    health: McpToolResult
    system_status: McpToolResult
