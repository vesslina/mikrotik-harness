from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from mth.core.mcp_client import McpToolResult


class RunbookFieldKind(StrEnum):
    TEXT = "text"
    SECRET = "secret"
    BOOLEAN = "boolean"
    CSV = "csv"


@dataclass(frozen=True, slots=True)
class RunbookField:
    name: str
    label: str
    kind: RunbookFieldKind = RunbookFieldKind.TEXT
    required: bool = False
    default: str | bool | tuple[str, ...] | None = None
    placeholder: str = ""
    description: str = ""
    max_length: int = 256
    allowed_values: tuple[str, ...] = ()
    human_only: bool = False

    @property
    def secret(self) -> bool:
        return self.kind is RunbookFieldKind.SECRET


@dataclass(frozen=True, slots=True)
class RunbookSubmission:
    runbook_id: str
    values: dict[str, Any]
    secrets: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class RunbookStep:
    tool: str
    params: dict[str, Any]

    def as_mcp(self) -> dict[str, Any]:
        return {"tool": self.tool, "params": dict(self.params)}


@dataclass(frozen=True, slots=True)
class RunbookVerification:
    passed: bool
    details: str
    operational: bool | None = None


@dataclass(frozen=True, slots=True)
class RunbookPlan:
    plan_id: str
    runbook_id: str
    title: str
    values: dict[str, Any]
    baseline: dict[str, Any]
    steps: tuple[RunbookStep, ...]
    preview: str
    summary: str


@dataclass(frozen=True, slots=True)
class RunbookApplyResult:
    journal_ids: tuple[str, ...]
    verification: RunbookVerification
    backend_summary: str

    @property
    def verified(self) -> bool:
        return self.verification.passed

    @property
    def operational(self) -> bool | None:
        return self.verification.operational


@dataclass(frozen=True, slots=True)
class RunbookRollbackPreview:
    journal_ids: tuple[str, ...]
    preview: str


@dataclass(frozen=True, slots=True)
class RunbookRollbackResult:
    verified: bool
    verification_details: str
    backend_summary: str


class RunbookError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        journal_ids: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.journal_ids = tuple(journal_ids)


class ToolSession(Protocol):
    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> McpToolResult: ...


class RunbookBackend(ToolSession, Protocol):
    def open_session(self) -> AbstractAsyncContextManager[ToolSession]: ...


class RunbookDefinition(ABC):
    id: str
    title: str
    command: str
    description: str
    proposal_tool_name: str
    proposal_description: str
    fields: tuple[RunbookField, ...]
    write_tools: frozenset[str]
    secret_backend_parameters: frozenset[str] = frozenset()
    capability_domains: frozenset[str]
    rollback_note = "Rollback restores the saved RouterOS configuration snapshot."

    def parse_submission(self, raw: Mapping[str, Any]) -> RunbookSubmission:
        values: dict[str, Any] = {}
        secrets: dict[str, str] = {}
        for spec in self.fields:
            parsed = self._parse_field(spec, raw.get(spec.name, spec.default))
            if spec.secret:
                if parsed:
                    secrets[spec.name] = str(parsed)
            else:
                values[spec.name] = parsed
        self.validate(values)
        return RunbookSubmission(self.id, values, secrets)

    def sanitize_proposal(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        sanitized: dict[str, Any] = {}
        for spec in self.fields:
            if spec.secret or spec.human_only or spec.name not in raw:
                continue
            try:
                sanitized[spec.name] = self._parse_field(spec, raw[spec.name])
            except ValueError:
                continue
        return sanitized

    def proposal_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        for spec in self.fields:
            if spec.secret or spec.human_only:
                continue
            if spec.kind is RunbookFieldKind.BOOLEAN:
                schema: dict[str, Any] = {"type": "boolean"}
            elif spec.kind is RunbookFieldKind.CSV:
                schema = {"type": "array", "items": {"type": "string"}}
            else:
                schema = {"type": "string", "maxLength": spec.max_length}
            if spec.description:
                schema["description"] = spec.description
            if spec.default is not None:
                schema["default"] = (
                    list(spec.default) if isinstance(spec.default, tuple) else spec.default
                )
            if spec.allowed_values:
                if spec.kind is RunbookFieldKind.CSV:
                    schema["items"]["enum"] = list(spec.allowed_values)
                else:
                    schema["enum"] = list(spec.allowed_values)
            properties[spec.name] = schema
        return {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }

    def validate(self, values: Mapping[str, Any]) -> None:
        del values

    @abstractmethod
    def build_steps(
        self,
        values: Mapping[str, Any],
        secrets: Mapping[str, str] | None = None,
    ) -> tuple[RunbookStep, ...]: ...

    @abstractmethod
    def summary(self, values: Mapping[str, Any]) -> str: ...

    @abstractmethod
    async def capture_baseline(
        self,
        session: ToolSession,
        router_id: str,
        values: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def verify_apply(
        self,
        session: ToolSession,
        router_id: str,
        values: Mapping[str, Any],
    ) -> RunbookVerification: ...

    @abstractmethod
    async def verify_rollback(
        self,
        session: ToolSession,
        router_id: str,
        values: Mapping[str, Any],
        baseline: Mapping[str, Any],
    ) -> RunbookVerification: ...

    @staticmethod
    def _parse_field(spec: RunbookField, raw: Any) -> Any:
        if spec.kind is RunbookFieldKind.BOOLEAN:
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                normalized = raw.strip().casefold()
                if normalized in {"true", "yes", "1", "on"}:
                    return True
                if normalized in {"false", "no", "0", "off", ""}:
                    return False
            return bool(spec.default)

        if spec.kind is RunbookFieldKind.CSV:
            candidates: Sequence[Any]
            if isinstance(raw, str):
                candidates = raw.split(",")
            elif isinstance(raw, (list, tuple)):
                candidates = raw
            elif raw is None:
                candidates = ()
            else:
                raise ValueError(f"{spec.label} must be a comma-separated list")
            values: list[str] = []
            for item in candidates:
                if not isinstance(item, str):
                    continue
                value = item.strip()
                if value and value not in values:
                    values.append(value)
            if spec.required and not values:
                raise ValueError(f"{spec.label} must not be empty")
            if spec.allowed_values:
                unknown = [value for value in values if value not in spec.allowed_values]
                if unknown:
                    raise ValueError(
                        f"{spec.label} contains unsupported value(s): {', '.join(unknown)}"
                    )
            return values

        value = raw.strip() if isinstance(raw, str) else "" if raw is None else str(raw)
        if spec.required and not value:
            raise ValueError(f"{spec.label} must not be empty")
        if len(value) > spec.max_length:
            raise ValueError(f"{spec.label} must be at most {spec.max_length} characters")
        if spec.allowed_values and value and value not in spec.allowed_values:
            raise ValueError(f"{spec.label} has an unsupported value: {value}")
        return value


class RunbookExecutor:
    """Shared approval-bound lifecycle for deterministic RouterOS runbooks."""

    MAX_STEPS = 10

    def __init__(
        self,
        backend: RunbookBackend,
        router_id: str,
        definition: RunbookDefinition,
    ) -> None:
        self._backend = backend
        self._router_id = router_id
        self.definition = definition

    async def plan(self, submission: RunbookSubmission) -> RunbookPlan:
        self._require_submission(submission)
        steps = self.definition.build_steps(submission.values)
        self._validate_steps(steps)
        arguments = {
            "routerId": self._router_id,
            "steps": [step.as_mcp() for step in steps],
        }
        async with self._backend.open_session() as session:
            try:
                baseline = await self.definition.capture_baseline(
                    session, self._router_id, submission.values
                )
            except RunbookError:
                raise
            except Exception as error:
                raise RunbookError(
                    "RUNBOOK_BASELINE_FAILED",
                    f"Could not capture the pre-change baseline: {error}",
                ) from error
            result = await session.call_tool("plan_changes", arguments)
        self._require_success(result, "RUNBOOK_PLAN_FAILED")
        self._require_viable_plan(result)
        return RunbookPlan(
            plan_id=f"{self.definition.id}-{uuid4().hex[:12]}",
            runbook_id=self.definition.id,
            title=self.definition.title,
            values=dict(submission.values),
            baseline=baseline,
            steps=steps,
            preview=result.text,
            summary=self.definition.summary(submission.values),
        )

    async def apply_approved(
        self,
        plan: RunbookPlan,
        secrets: Mapping[str, str] | None = None,
    ) -> RunbookApplyResult:
        self._require_plan(plan)
        applied_steps = self.definition.build_steps(plan.values, secrets or {})
        self._validate_apply_matches_plan(plan.steps, applied_steps)
        arguments = {
            "routerId": self._router_id,
            "steps": [step.as_mcp() for step in applied_steps],
        }
        async with self._backend.open_session() as session:
            challenge = await session.call_tool("apply_plan", arguments)
            token = self._confirmation_token(challenge)
            applied = await session.call_tool(
                "apply_plan", {**arguments, "confirmationToken": token}
            )
            if applied.is_error:
                journals = self._journal_ids(applied)
                self._raise_result_error(applied, "RUNBOOK_APPLY_FAILED", journals)
            journals = self._journal_ids(applied)
            if len(journals) != len(applied_steps):
                raise RunbookError(
                    "RUNBOOK_JOURNAL_MISSING",
                    "MikroMCP applied the plan but did not return one rollback journal "
                    "for every step.",
                    journal_ids=journals,
                )
            try:
                verification = await self.definition.verify_apply(
                    session, self._router_id, plan.values
                )
            except Exception as error:
                verification = RunbookVerification(
                    False,
                    f"{self.definition.title} post-check failed: {error}",
                )
        return RunbookApplyResult(
            journal_ids=journals,
            verification=verification,
            backend_summary=applied.text,
        )

    async def preview_rollback(
        self,
        journal_ids: Sequence[str],
    ) -> RunbookRollbackPreview:
        normalized = self._normalized_journals(journal_ids)
        previews: list[str] = []
        async with self._backend.open_session() as session:
            for journal_id in reversed(normalized):
                result = await self._call_confirmed(
                    session,
                    "rollback_change",
                    {
                        "routerId": self._router_id,
                        "journalId": journal_id,
                        "dryRun": True,
                    },
                )
                previews.append(f"{journal_id}: {result.text}")
        return RunbookRollbackPreview(normalized, "\n".join(previews))

    async def rollback_approved(
        self,
        plan: RunbookPlan,
        journal_ids: Sequence[str],
    ) -> RunbookRollbackResult:
        self._require_plan(plan)
        normalized = self._normalized_journals(journal_ids)
        summaries: list[str] = []
        async with self._backend.open_session() as session:
            for journal_id in reversed(normalized):
                result = await self._call_confirmed(
                    session,
                    "rollback_change",
                    {
                        "routerId": self._router_id,
                        "journalId": journal_id,
                        "dryRun": False,
                    },
                )
                summaries.append(result.text)
            try:
                verification = await self.definition.verify_rollback(
                    session,
                    self._router_id,
                    plan.values,
                    plan.baseline,
                )
            except Exception as error:
                raise RunbookError(
                    "RUNBOOK_ROLLBACK_POST_CHECK_FAILED",
                    f"{self.definition.title} rollback post-check failed: {error}",
                    journal_ids=normalized,
                ) from error
        if not verification.passed:
            raise RunbookError(
                "RUNBOOK_ROLLBACK_POST_CHECK_FAILED",
                verification.details,
                journal_ids=normalized,
            )
        return RunbookRollbackResult(
            verified=True,
            verification_details=verification.details,
            backend_summary="\n".join(summaries),
        )

    async def _call_confirmed(
        self,
        session: ToolSession,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> McpToolResult:
        challenge = await session.call_tool(tool_name, arguments)
        token = self._confirmation_token(challenge)
        result = await session.call_tool(
            tool_name,
            {**arguments, "confirmationToken": token},
        )
        self._require_success(result, f"{tool_name.upper()}_FAILED")
        return result

    def _require_submission(self, submission: RunbookSubmission) -> None:
        if submission.runbook_id != self.definition.id:
            raise RunbookError(
                "RUNBOOK_MISMATCH",
                f"Submission is for {submission.runbook_id}, not {self.definition.id}.",
            )

    def _require_plan(self, plan: RunbookPlan) -> None:
        if plan.runbook_id != self.definition.id:
            raise RunbookError(
                "RUNBOOK_MISMATCH",
                f"Plan is for {plan.runbook_id}, not {self.definition.id}.",
            )

    def _validate_steps(self, steps: Sequence[RunbookStep]) -> None:
        if not steps:
            raise RunbookError("RUNBOOK_EMPTY", "Runbook produced no change steps.")
        if len(steps) > self.MAX_STEPS:
            raise RunbookError(
                "RUNBOOK_TOO_LARGE",
                f"Runbook produced {len(steps)} steps; MikroMCP supports at most 10 per plan.",
            )
        for step in steps:
            if step.tool not in self.definition.write_tools:
                raise RunbookError(
                    "RUNBOOK_TOOL_NOT_ALLOWED",
                    f"Runbook attempted undeclared tool {step.tool}.",
                )
            forbidden = {"routerId", "dryRun", "confirmationToken"} & step.params.keys()
            if forbidden:
                raise RunbookError(
                    "RUNBOOK_ARGUMENT_NOT_ALLOWED",
                    "Runbook step contains harness-owned argument(s): "
                    + ", ".join(sorted(forbidden))
                    + ".",
                )

    def _validate_apply_matches_plan(
        self,
        planned: Sequence[RunbookStep],
        applied: Sequence[RunbookStep],
    ) -> None:
        self._validate_steps(applied)
        if len(planned) != len(applied):
            raise RunbookError("RUNBOOK_PLAN_DIVERGED", "Approved plan step count changed.")
        for planned_step, applied_step in zip(planned, applied, strict=True):
            if planned_step.tool != applied_step.tool:
                raise RunbookError("RUNBOOK_PLAN_DIVERGED", "Approved plan tool changed.")
            for key, value in applied_step.params.items():
                if key in self.definition.secret_backend_parameters:
                    continue
                if planned_step.params.get(key) != value:
                    raise RunbookError(
                        "RUNBOOK_PLAN_DIVERGED",
                        f"Approved parameter {key} changed before apply.",
                    )
            missing = set(planned_step.params) - set(applied_step.params)
            if missing:
                raise RunbookError(
                    "RUNBOOK_PLAN_DIVERGED",
                    f"Approved parameter(s) disappeared: {', '.join(sorted(missing))}.",
                )

    @staticmethod
    def _confirmation_token(result: McpToolResult) -> str:
        token = result.confirmation_token
        if (
            (result.structured_content or {}).get("code") != "CONFIRMATION_REQUIRED"
            or token is None
        ):
            raise RunbookError(
                "CONFIRMATION_GATE_BYPASSED",
                "MikroMCP did not issue the required operator confirmation token; apply stopped.",
            )
        return token

    @classmethod
    def _require_success(cls, result: McpToolResult, fallback_code: str) -> None:
        if not result.is_error:
            return
        cls._raise_result_error(result, fallback_code)

    @staticmethod
    def _raise_result_error(
        result: McpToolResult,
        fallback_code: str,
        journal_ids: Sequence[str] = (),
    ) -> None:
        structured = result.structured_content or {}
        code = structured.get("code")
        message = structured.get("message")
        error = structured.get("error")
        if not isinstance(code, str) and isinstance(error, dict):
            code = error.get("code")
        if not isinstance(message, str) and isinstance(error, dict):
            message = error.get("message")
        raise RunbookError(
            str(code) if isinstance(code, str) else fallback_code,
            str(message) if isinstance(message, str) else result.text,
            journal_ids=journal_ids,
        )

    @staticmethod
    def _require_viable_plan(result: McpToolResult) -> None:
        structured = result.structured_content or {}
        steps = structured.get("steps")
        if not isinstance(steps, list) or not steps:
            raise RunbookError(
                "RUNBOOK_PLAN_INVALID",
                "MikroMCP returned a plan without structured step results.",
            )
        failures: list[str] = []
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                failures.append(f"step {index}: malformed result")
                continue
            dry_run = step.get("structuredDryRun")
            if isinstance(dry_run, dict) and dry_run.get("action") == "would_fail":
                detail = step.get("dryRunResult")
                failures.append(str(detail) if detail else f"step {index}: would fail")
        if failures:
            raise RunbookError(
                "RUNBOOK_PLAN_WOULD_FAIL",
                "Dry-run plan is not safe to approve: " + "; ".join(failures),
            )

    @staticmethod
    def _journal_ids(result: McpToolResult) -> tuple[str, ...]:
        structured = result.structured_content or {}
        candidates = structured.get("steps")
        if not isinstance(candidates, list) or not candidates:
            candidates = structured.get("completedSteps")
        if not isinstance(candidates, list):
            return ()
        return tuple(
            str(step["journalId"])
            for step in candidates
            if isinstance(step, dict) and isinstance(step.get("journalId"), str)
        )

    @staticmethod
    def _normalized_journals(journal_ids: Sequence[str]) -> tuple[str, ...]:
        normalized = tuple(journal.strip() for journal in journal_ids if journal.strip())
        if not normalized:
            raise ValueError("at least one rollback journal ID is required")
        return normalized
