from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from mth.core.mcp_client import McpToolResult


class ToolSession(Protocol):
    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> McpToolResult: ...


class RunbookBackend(ToolSession, Protocol):
    def open_session(self) -> AbstractAsyncContextManager[ToolSession]: ...


class PppoeRunbookError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PppoeRequest:
    name: str
    interface: str
    username: str
    service_name: str | None = None
    add_default_route: bool = True
    dial_on_demand: bool = False

    def __post_init__(self) -> None:
        for label, value in (
            ("PPPoE name", self.name),
            ("parent interface", self.interface),
            ("PPPoE username", self.username),
        ):
            if not value.strip():
                raise ValueError(f"{label} must not be empty")


@dataclass(frozen=True, slots=True)
class PppoeSecret:
    password: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.password:
            raise ValueError("PPPoE password must not be empty")


@dataclass(frozen=True, slots=True)
class PppoePlan:
    plan_id: str
    request: PppoeRequest
    preview: str

    @property
    def summary(self) -> str:
        route = "yes" if self.request.add_default_route else "no"
        demand = "yes" if self.request.dial_on_demand else "no"
        service = self.request.service_name or "any"
        return (
            f'Create PPPoE client "{self.request.name}" on {self.request.interface}\n'
            f"User: {self.request.username} · service: {service}\n"
            f"Default route: {route} · dial on demand: {demand}\n"
            "Password: supplied through the masked field (not shown)"
        )


@dataclass(frozen=True, slots=True)
class PppoeApplyResult:
    journal_ids: tuple[str, ...]
    verified: bool
    verification_details: str
    backend_summary: str
    operational: bool | None = None


@dataclass(frozen=True, slots=True)
class PppoeRollbackPreview:
    journal_id: str
    preview: str


@dataclass(frozen=True, slots=True)
class PppoeRollbackResult:
    verified: bool
    verification_details: str
    backend_summary: str


class PppoeRunbookExecutor:
    """WAN PPPoE add workflow with deterministic steps and a human-bound apply gate."""

    def __init__(self, backend: RunbookBackend, router_id: str) -> None:
        self._backend = backend
        self._router_id = router_id

    async def plan(self, request: PppoeRequest) -> PppoePlan:
        arguments = {
            "routerId": self._router_id,
            "steps": [self._step(request)],
        }
        result = await self._backend.call_tool("plan_changes", arguments)
        self._require_success(result, "PPPOE_PLAN_FAILED")
        self._require_viable_plan(result)
        return PppoePlan(
            plan_id=f"pppoe-{uuid4().hex[:12]}",
            request=request,
            preview=result.text,
        )

    async def apply_approved(
        self,
        plan: PppoePlan,
        secret: PppoeSecret,
    ) -> PppoeApplyResult:
        steps = [self._step(plan.request, secret)]
        base_arguments: dict[str, Any] = {
            "routerId": self._router_id,
            "steps": steps,
        }

        # Token issuance, consumption, and verification share one child process. This
        # preserves MikroMCP's process-local single-use replay protection.
        async with self._backend.open_session() as session:
            challenge = await session.call_tool("apply_plan", base_arguments)
            token = self._confirmation_token(challenge)
            confirmed_arguments = {**base_arguments, "confirmationToken": token}
            applied = await session.call_tool("apply_plan", confirmed_arguments)
            self._require_success(applied, "PPPOE_APPLY_FAILED")
            verification = await session.call_tool(
                "list_pppoe_clients",
                {"routerId": self._router_id, "status": "all"},
            )

        journal_ids = self._journal_ids(applied)
        if verification.is_error:
            verified = False
            operational = None
            details = (
                "The PPPoE change was applied, but the mandatory post-check failed: "
                f"{verification.text}"
            )
        else:
            verified, operational, details = self._verify(plan.request, verification)
        return PppoeApplyResult(
            journal_ids=journal_ids,
            verified=verified,
            verification_details=details,
            backend_summary=applied.text,
            operational=operational,
        )

    async def preview_rollback(self, journal_id: str) -> PppoeRollbackPreview:
        if not journal_id.strip():
            raise ValueError("rollback journal ID must not be empty")
        arguments = {
            "routerId": self._router_id,
            "journalId": journal_id,
            "dryRun": True,
        }
        async with self._backend.open_session() as session:
            preview = await self._call_confirmed(session, "rollback_change", arguments)
        return PppoeRollbackPreview(journal_id=journal_id, preview=preview.text)

    async def rollback_approved(
        self,
        journal_id: str,
        request: PppoeRequest,
    ) -> PppoeRollbackResult:
        arguments = {
            "routerId": self._router_id,
            "journalId": journal_id,
            "dryRun": False,
        }
        async with self._backend.open_session() as session:
            rolled_back = await self._call_confirmed(session, "rollback_change", arguments)
            verification = await session.call_tool(
                "list_pppoe_clients",
                {"routerId": self._router_id, "status": "all"},
            )
            self._require_success(verification, "PPPOE_ROLLBACK_VERIFY_FAILED")
        absent, details = self._verify_absent(request, verification)
        if not absent:
            raise PppoeRunbookError("PPPOE_ROLLBACK_POST_CHECK_FAILED", details)
        return PppoeRollbackResult(
            verified=True,
            verification_details=details,
            backend_summary=rolled_back.text,
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

    def _step(
        self,
        request: PppoeRequest,
        secret: PppoeSecret | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "action": "add",
            "name": request.name,
            "interface": request.interface,
            "user": request.username,
            "addDefaultRoute": request.add_default_route,
            "dialOnDemand": request.dial_on_demand,
        }
        if request.service_name is not None:
            params["serviceName"] = request.service_name
        if secret is not None:
            params["password"] = secret.password
        return {"tool": "manage_pppoe_client", "params": params}

    @staticmethod
    def _confirmation_token(result: McpToolResult) -> str:
        structured = result.structured_content or {}
        details = structured.get("details")
        token = details.get("confirmationToken") if isinstance(details, dict) else None
        if (
            not result.is_error
            or structured.get("code") != "CONFIRMATION_REQUIRED"
            or not isinstance(token, str)
            or not token
        ):
            raise PppoeRunbookError(
                "CONFIRMATION_GATE_BYPASSED",
                "MikroMCP did not issue the required operator confirmation token; apply stopped.",
            )
        return token

    @staticmethod
    def _require_success(result: McpToolResult, fallback_code: str) -> None:
        if not result.is_error:
            return
        structured = result.structured_content or {}
        code = structured.get("code")
        message = structured.get("message")
        raise PppoeRunbookError(
            str(code) if isinstance(code, str) else fallback_code,
            str(message) if isinstance(message, str) else result.text,
        )

    @staticmethod
    def _require_viable_plan(result: McpToolResult) -> None:
        structured = result.structured_content or {}
        steps = structured.get("steps")
        if not isinstance(steps, list) or not steps:
            raise PppoeRunbookError(
                "PPPOE_PLAN_INVALID",
                "MikroMCP returned a plan without structured step results.",
            )
        failures = []
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                failures.append(f"step {index}: malformed result")
                continue
            dry_run = step.get("structuredDryRun")
            if isinstance(dry_run, dict) and dry_run.get("action") == "would_fail":
                detail = step.get("dryRunResult")
                failures.append(str(detail) if detail else f"step {index}: would fail")
        if failures:
            raise PppoeRunbookError(
                "PPPOE_PLAN_WOULD_FAIL",
                "Dry-run plan is not safe to approve: " + "; ".join(failures),
            )

    @staticmethod
    def _journal_ids(result: McpToolResult) -> tuple[str, ...]:
        structured = result.structured_content or {}
        raw_steps = structured.get("steps")
        if not isinstance(raw_steps, list):
            return ()
        return tuple(
            str(step["journalId"])
            for step in raw_steps
            if isinstance(step, dict) and isinstance(step.get("journalId"), str)
        )

    @staticmethod
    def _verify(
        request: PppoeRequest,
        result: McpToolResult,
    ) -> tuple[bool, bool | None, str]:
        structured = result.structured_content or {}
        clients = structured.get("clients")
        if not isinstance(clients, list):
            return False, None, "PPPoE verification returned no client list."
        match = next(
            (
                client
                for client in clients
                if isinstance(client, dict) and client.get("name") == request.name
            ),
            None,
        )
        if not isinstance(match, dict):
            return False, None, f'PPPoE client "{request.name}" was not found after apply.'
        if match.get("interface") != request.interface or match.get("user") != request.username:
            return (
                False,
                None,
                f'PPPoE client "{request.name}" exists but differs from the approved plan.',
            )
        disabled = match.get("disabled")
        running = match.get("running")
        if disabled is True:
            return (
                True,
                False,
                f'PPPoE client "{request.name}" matches the approved configuration; '
                "the interface is disabled and operationally inactive.",
            )
        if running is True:
            return (
                True,
                True,
                f'PPPoE client "{request.name}" matches the approved configuration and '
                "the session is active.",
            )
        if running is False:
            return (
                True,
                False,
                f'PPPoE client "{request.name}" matches the approved configuration; '
                "the session is not currently running.",
            )
        return (
            True,
            None,
            f'PPPoE client "{request.name}" matches the approved configuration; '
            "RouterOS did not report an operational state.",
        )

    @staticmethod
    def _verify_absent(request: PppoeRequest, result: McpToolResult) -> tuple[bool, str]:
        structured = result.structured_content or {}
        clients = structured.get("clients")
        if not isinstance(clients, list):
            return False, "PPPoE rollback verification returned no client list."
        exists = any(
            isinstance(client, dict) and client.get("name") == request.name
            for client in clients
        )
        if exists:
            return False, f'PPPoE client "{request.name}" still exists after rollback.'
        return True, f'PPPoE client "{request.name}" is absent; rollback verified.'
