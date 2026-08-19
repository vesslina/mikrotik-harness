from __future__ import annotations

from dataclasses import dataclass, field

from mth.core.runbooks.base import (
    RunbookApplyResult,
    RunbookBackend,
    RunbookError,
    RunbookExecutor,
    RunbookPlan,
    RunbookRollbackPreview,
    RunbookRollbackResult,
    RunbookSubmission,
)
from mth.core.runbooks.catalog import WanPppoeDefinition


class PppoeRunbookError(RunbookError):
    pass


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
    generic: RunbookPlan | None = field(default=None, repr=False)

    @property
    def summary(self) -> str:
        return WanPppoeDefinition().summary(_values(self.request))


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


def _values(request: PppoeRequest) -> dict[str, object]:
    return {
        "name": request.name,
        "interface": request.interface,
        "username": request.username,
        "serviceName": request.service_name or "",
        "addDefaultRoute": request.add_default_route,
        "dialOnDemand": request.dial_on_demand,
    }


def _compatibility_code(code: str) -> str:
    """Keep the public PPPoE facade's historical error namespace stable."""

    if code == "RUNBOOK_PLAN_WOULD_FAIL":
        return "PPPOE_PLAN_WOULD_FAIL"
    return code


class PppoeRunbookExecutor:
    """Compatibility facade backed by the shared runbook engine."""

    def __init__(self, backend: RunbookBackend, router_id: str) -> None:
        self._definition = WanPppoeDefinition()
        self._executor = RunbookExecutor(backend, router_id, self._definition)
        self._plans_by_journal: dict[str, RunbookPlan] = {}

    async def plan(self, request: PppoeRequest) -> PppoePlan:
        submission = RunbookSubmission(self._definition.id, _values(request))
        try:
            plan = await self._executor.plan(submission)
        except RunbookError as error:
            raise PppoeRunbookError(
                _compatibility_code(error.code),
                str(error),
                journal_ids=error.journal_ids,
            ) from error
        return PppoePlan(plan.plan_id, request, plan.preview, plan)

    async def apply_approved(
        self,
        plan: PppoePlan,
        secret: PppoeSecret,
    ) -> PppoeApplyResult:
        generic = plan.generic
        if generic is None:
            raise PppoeRunbookError(
                "PPPOE_PLAN_INVALID", "The PPPoE plan was not created by this executor."
            )
        try:
            result: RunbookApplyResult = await self._executor.apply_approved(
                generic, {"password": secret.password}
            )
        except RunbookError as error:
            raise PppoeRunbookError(
                error.code, str(error), journal_ids=error.journal_ids
            ) from error
        for journal_id in result.journal_ids:
            self._plans_by_journal[journal_id] = generic
        return PppoeApplyResult(
            journal_ids=result.journal_ids,
            verified=result.verified,
            verification_details=result.verification.details,
            backend_summary=result.backend_summary,
            operational=result.operational,
        )

    async def preview_rollback(self, journal_id: str) -> PppoeRollbackPreview:
        try:
            result: RunbookRollbackPreview = await self._executor.preview_rollback(
                (journal_id,)
            )
        except RunbookError as error:
            raise PppoeRunbookError(error.code, str(error)) from error
        prefix = f"{journal_id}: "
        preview = result.preview.removeprefix(prefix)
        return PppoeRollbackPreview(journal_id, preview)

    async def rollback_approved(
        self,
        journal_id: str,
        request: PppoeRequest,
    ) -> PppoeRollbackResult:
        plan = self._plans_by_journal.get(journal_id)
        if plan is None:
            values = _values(request)
            plan = RunbookPlan(
                plan_id="legacy-rollback-context",
                runbook_id=self._definition.id,
                title=self._definition.title,
                values=values,
                baseline={"record": None},
                steps=self._definition.build_steps(values),
                preview="",
                summary=self._definition.summary(values),
            )
        try:
            result: RunbookRollbackResult = await self._executor.rollback_approved(
                plan, (journal_id,)
            )
        except RunbookError as error:
            raise PppoeRunbookError(
                error.code, str(error), journal_ids=error.journal_ids
            ) from error
        return PppoeRollbackResult(
            verified=result.verified,
            verification_details=result.verification_details,
            backend_summary=result.backend_summary,
        )
