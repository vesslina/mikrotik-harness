import asyncio
from contextlib import asynccontextmanager

import pytest

from mth.core.mcp_client import McpToolResult
from mth.core.runbooks import (
    AdminServicesDefinition,
    LanBridgeDefinition,
    RunbookDefinition,
    RunbookError,
    RunbookExecutor,
    RunbookHistoryPaths,
    RunbookHistoryStore,
    RunbookStep,
    RunbookSubmission,
    RunbookVerification,
    WanNatDefinition,
    WanPppoeDefinition,
)


class _Definition(RunbookDefinition):
    id = "test"
    title = "Test runbook"
    command = "/test"
    description = "test"
    proposal_tool_name = "propose_test"
    proposal_description = "test"
    fields = ()
    write_tools = frozenset({"manage_test"})
    secret_backend_parameters = frozenset({"password"})
    capability_domains = frozenset({"system"})

    def build_steps(self, values, secrets=None):
        params = {"action": "add", "name": values["name"]}
        if secrets is not None:
            params["password"] = secrets["password"]
        return (RunbookStep("manage_test", params),)

    def summary(self, values):
        return f"Add {values['name']}"

    async def capture_baseline(self, session, router_id, values):
        result = await session.call_tool("get_test", {"routerId": router_id})
        return dict(result.structured_content or {})

    async def verify_apply(self, session, router_id, values):
        await session.call_tool("get_test", {"routerId": router_id})
        return RunbookVerification(True, "applied")

    async def verify_rollback(self, session, router_id, values, baseline):
        await session.call_tool("get_test", {"routerId": router_id})
        return RunbookVerification(True, "restored")


class _Backend:
    def __init__(
        self,
        *,
        partial_failure=False,
        baseline_error=False,
        missing_journal=False,
    ):
        self.calls = []
        self.partial_failure = partial_failure
        self.baseline_error = baseline_error
        self.missing_journal = missing_journal

    @asynccontextmanager
    async def open_session(self):
        yield self

    async def call_tool(self, name, arguments=None):
        arguments = dict(arguments or {})
        self.calls.append((name, arguments))
        if name == "get_test":
            if self.baseline_error:
                raise RuntimeError("router read failed")
            return McpToolResult((), {"present": False}, False)
        if name == "plan_changes":
            assert "password" not in arguments["steps"][0]["params"]
            return McpToolResult(
                ("dry run",),
                {"steps": [{"structuredDryRun": {"action": "dry_run"}}]},
                False,
            )
        if name == "apply_plan" and "confirmationToken" not in arguments:
            return McpToolResult(
                ("confirm",),
                {
                    "code": "CONFIRMATION_REQUIRED",
                    "details": {"confirmationToken": "token"},
                },
                True,
            )
        if name == "apply_plan":
            assert arguments["steps"][0]["params"]["password"] == "secret"
            if self.partial_failure:
                return McpToolResult(
                    ("partial",),
                    {
                        "status": "failed",
                        "completedSteps": [{"journalId": "journal-partial"}],
                        "error": {"code": "STEP_FAILED", "message": "boom"},
                    },
                    True,
                )
            if self.missing_journal:
                return McpToolResult(("applied",), {"steps": []}, False)
            return McpToolResult(
                ("applied",), {"steps": [{"journalId": "journal-1"}]}, False
            )
        if name == "rollback_change" and "confirmationToken" not in arguments:
            return McpToolResult(
                ("confirm",),
                {
                    "code": "CONFIRMATION_REQUIRED",
                    "details": {"confirmationToken": "token"},
                },
                True,
            )
        if name == "rollback_change":
            return McpToolResult(("rolled back",), {"action": "rolled_back"}, False)
        raise AssertionError(name)


def test_shared_engine_keeps_secrets_out_of_plan_and_verifies_rollback() -> None:
    async def scenario() -> None:
        backend = _Backend()
        executor = RunbookExecutor(backend, "router-1", _Definition())
        submission = RunbookSubmission("test", {"name": "sample"}, {"password": "secret"})

        plan = await executor.plan(submission)
        applied = await executor.apply_approved(plan, submission.secrets)
        preview = await executor.preview_rollback(applied.journal_ids)
        rolled_back = await executor.rollback_approved(plan, applied.journal_ids)

        assert "secret" not in plan.preview
        assert applied.journal_ids == ("journal-1",)
        assert preview.journal_ids == ("journal-1",)
        assert rolled_back.verified is True

    asyncio.run(scenario())


def test_shared_engine_preserves_partial_apply_journals() -> None:
    async def scenario() -> None:
        executor = RunbookExecutor(_Backend(partial_failure=True), "router-1", _Definition())
        submission = RunbookSubmission("test", {"name": "sample"}, {"password": "secret"})
        plan = await executor.plan(submission)

        with pytest.raises(RunbookError) as captured:
            await executor.apply_approved(plan, submission.secrets)

        assert captured.value.code == "STEP_FAILED"
        assert captured.value.journal_ids == ("journal-partial",)

    asyncio.run(scenario())


def test_shared_engine_fails_closed_without_baseline_or_rollback_journal() -> None:
    async def scenario() -> None:
        submission = RunbookSubmission("test", {"name": "sample"}, {"password": "secret"})
        baseline_executor = RunbookExecutor(
            _Backend(baseline_error=True), "router-1", _Definition()
        )
        with pytest.raises(RunbookError) as baseline_error:
            await baseline_executor.plan(submission)
        assert baseline_error.value.code == "RUNBOOK_BASELINE_FAILED"

        journal_executor = RunbookExecutor(
            _Backend(missing_journal=True), "router-1", _Definition()
        )
        plan = await journal_executor.plan(submission)
        with pytest.raises(RunbookError) as journal_error:
            await journal_executor.apply_approved(plan, submission.secrets)
        assert journal_error.value.code == "RUNBOOK_JOURNAL_MISSING"

    asyncio.run(scenario())


def test_history_is_secret_free_and_resolves_execution_or_journal(tmp_path) -> None:
    async def scenario() -> None:
        executor = RunbookExecutor(_Backend(), "router-1", _Definition())
        submission = RunbookSubmission("test", {"name": "sample"}, {"password": "secret"})
        plan = await executor.plan(submission)
        applied = await executor.apply_approved(plan, submission.secrets)
        store = RunbookHistoryStore(RunbookHistoryPaths(file=tmp_path / "history.json"))

        record = store.record(
            plan, "router-1", applied.journal_ids, status="applied_verified"
        )

        assert store.find(record.execution_id, "router-1") == record
        assert store.find("journal-1", "router-1") == record
        assert "secret" not in (tmp_path / "history.json").read_text(encoding="utf-8")

    asyncio.run(scenario())


def test_catalog_builds_only_reviewed_steps_and_rejects_lockout_services() -> None:
    pppoe = WanPppoeDefinition().parse_submission(
        {
            "name": "pppoe-wan",
            "interface": "ether1",
            "username": "isp",
            "password": "secret",
        }
    )
    assert pppoe.secrets == {"password": "secret"}
    assert "password" not in WanPppoeDefinition().build_steps(pppoe.values)[0].params

    bridge = LanBridgeDefinition().parse_submission(
        {"name": "bridge-lan", "interfaces": "ether2, ether3"}
    )
    assert [step.tool for step in LanBridgeDefinition().build_steps(bridge.values)] == [
        "manage_bridge",
        "manage_bridge_port",
        "manage_bridge_port",
    ]

    nat = WanNatDefinition().parse_submission({"outInterface": "pppoe-wan"})
    assert WanNatDefinition().build_steps(nat.values)[0].params["ruleAction"] == "masquerade"

    with pytest.raises(ValueError, match="unsupported"):
        AdminServicesDefinition().parse_submission({"services": "telnet,www-ssl"})


def test_pppoe_baseline_projects_router_output_without_password() -> None:
    class Session:
        async def call_tool(self, name, arguments=None):
            assert name == "list_pppoe_clients"
            return McpToolResult(
                (),
                {
                    "clients": [
                        {
                            "name": "pppoe-wan",
                            "interface": "ether1",
                            "user": "isp-user",
                            "password": 22234,
                            "running": False,
                            "comment": "untrusted device text",
                        }
                    ]
                },
                False,
            )

    baseline = asyncio.run(
        WanPppoeDefinition().capture_baseline(
            Session(),
            "router-1",
            {"name": "pppoe-wan"},
        )
    )

    serialized = str(baseline)
    assert "password" not in serialized
    assert "22234" not in serialized
    assert "untrusted device text" not in serialized
    assert baseline["record"] == {
        "name": "pppoe-wan",
        "interface": "ether1",
        "user": "isp-user",
        "service-name": None,
        "add-default-route": None,
        "dial-on-demand": None,
        "disabled": None,
    }
