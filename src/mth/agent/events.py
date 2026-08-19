from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)


class RiskLevel(StrEnum):
    READ_ONLY = "read_only"
    CHANGE = "change"
    DESTRUCTIVE = "destructive"


class FinalOutcome(StrEnum):
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AgentMessage:
    text: str
    kind: str = field(default="agent_message", init=False)


@dataclass(frozen=True, slots=True)
class ReasoningStatus:
    token_count: int | None
    recovered_final_answer: bool = False
    kind: str = field(default="reasoning_status", init=False)


@dataclass(frozen=True, slots=True)
class PlannedAction:
    summary: str
    tool_names: tuple[str, ...]
    risk: RiskLevel
    kind: str = field(default="planned_action", init=False)


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    tool_name: str
    arguments: dict[str, JsonValue]
    risk: RiskLevel
    kind: str = field(default="tool_call", init=False)


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    tool_name: str
    content: tuple[str, ...]
    structured_content: dict[str, JsonValue] | None
    is_error: bool
    kind: str = field(default="tool_result", init=False)


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    plan_id: str
    summary: str
    risk: RiskLevel
    kind: str = field(default="approval_request", init=False)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    check: str
    passed: bool
    details: str
    kind: str = field(default="verification_result", init=False)


@dataclass(frozen=True, slots=True)
class FinalSummary:
    text: str
    outcome: FinalOutcome
    kind: str = field(default="final_summary", init=False)


AgentEvent: TypeAlias = (
    AgentMessage
    | ReasoningStatus
    | PlannedAction
    | ToolCall
    | ToolResult
    | ApprovalRequest
    | VerificationResult
    | FinalSummary
)
