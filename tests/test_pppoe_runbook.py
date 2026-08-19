import asyncio
from contextlib import asynccontextmanager

import pytest

from mth.core.mcp_client import McpToolResult
from mth.core.runbooks import (
    PppoeRequest,
    PppoeRunbookError,
    PppoeRunbookExecutor,
    PppoeSecret,
)


class _Backend:
    def __init__(
        self,
        *,
        bypass_confirmation: bool = False,
        verification_error: bool = False,
    ) -> None:
        self.calls = []
        self.open_count = 0
        self.bypass_confirmation = bypass_confirmation
        self.verification_error = verification_error
        self.rolled_back = False

    @asynccontextmanager
    async def open_session(self):
        self.open_count += 1
        yield self

    async def call_tool(self, name, arguments=None) -> McpToolResult:
        arguments = dict(arguments or {})
        self.calls.append((name, arguments))
        if name == "plan_changes":
            params = arguments["steps"][0]["params"]
            assert "password" not in params
            return McpToolResult(
                ("Dry run: would create pppoe-wan",),
                {
                    "steps": [
                        {
                            "dryRunResult": "Would create pppoe-wan",
                            "structuredDryRun": {"action": "dry_run"},
                        }
                    ]
                },
                False,
            )
        if name == "apply_plan" and "confirmationToken" not in arguments:
            if self.bypass_confirmation:
                return McpToolResult(("Applied without a gate",), {"status": "success"}, False)
            return McpToolResult(
                ("Confirmation required",),
                {
                    "code": "CONFIRMATION_REQUIRED",
                    "details": {"confirmationToken": "token-1"},
                },
                True,
            )
        if name == "apply_plan":
            assert arguments["confirmationToken"] == "token-1"
            assert arguments["steps"][0]["params"]["password"] == "isp-secret"
            return McpToolResult(
                ("Applied 1/1 step",),
                {"status": "success", "steps": [{"journalId": "journal-1"}]},
                False,
            )
        if name == "rollback_change" and "confirmationToken" not in arguments:
            return McpToolResult(
                ("Confirmation required",),
                {
                    "code": "CONFIRMATION_REQUIRED",
                    "details": {"confirmationToken": "token-1"},
                },
                True,
            )
        if name == "rollback_change":
            assert arguments["confirmationToken"] == "token-1"
            if arguments["dryRun"]:
                return McpToolResult(("Would remove pppoe-wan",), {"action": "dry_run"}, False)
            self.rolled_back = True
            return McpToolResult(("Rolled back journal-1",), {"action": "rolled_back"}, False)
        if name == "list_pppoe_clients":
            if self.verification_error:
                return McpToolResult(
                    ("Router timeout",),
                    {"code": "ROUTER_TIMEOUT", "message": "Router timeout"},
                    True,
                )
            clients = [] if self.rolled_back else [
                {
                    "name": "pppoe-wan",
                    "interface": "ether1",
                    "user": "isp-user",
                    "running": False,
                }
            ]
            return McpToolResult(
                ("pppoe-wan",),
                {"clients": clients},
                False,
            )
        raise AssertionError(name)


def _request() -> PppoeRequest:
    return PppoeRequest(
        name="pppoe-wan",
        interface="ether1",
        username="isp-user",
    )


def test_pppoe_plan_apply_and_verify_keep_secret_out_of_plan() -> None:
    async def scenario() -> None:
        backend = _Backend()
        executor = PppoeRunbookExecutor(backend, "mikrotik-afe23e")

        plan = await executor.plan(_request())
        result = await executor.apply_approved(plan, PppoeSecret("isp-secret"))

        assert "isp-secret" not in plan.preview
        assert "isp-secret" not in plan.summary
        assert result.verified is True
        assert result.journal_ids == ("journal-1",)
        assert backend.open_count == 1
        assert [name for name, _ in backend.calls[-3:]] == [
            "apply_plan",
            "apply_plan",
            "list_pppoe_clients",
        ]

    asyncio.run(scenario())


def test_pppoe_apply_fails_closed_when_backend_bypasses_confirmation() -> None:
    async def scenario() -> None:
        backend = _Backend(bypass_confirmation=True)
        executor = PppoeRunbookExecutor(backend, "mikrotik-afe23e")
        plan = await executor.plan(_request())

        with pytest.raises(PppoeRunbookError, match="did not issue") as captured:
            await executor.apply_approved(plan, PppoeSecret("isp-secret"))

        assert captured.value.code == "CONFIRMATION_GATE_BYPASSED"

    asyncio.run(scenario())


def test_pppoe_plan_rejects_nested_would_fail_step() -> None:
    async def scenario() -> None:
        class Backend(_Backend):
            async def call_tool(self, name, arguments=None):
                if name == "plan_changes":
                    return McpToolResult(
                        ("Plan contains a failing step",),
                        {
                            "steps": [
                                {
                                    "dryRunResult": "Connection refused",
                                    "structuredDryRun": {
                                        "action": "would_fail",
                                        "error": "ROUTER_UNREACHABLE",
                                    },
                                }
                            ]
                        },
                        False,
                    )
                return await super().call_tool(name, arguments)

        executor = PppoeRunbookExecutor(Backend(), "mikrotik-afe23e")

        with pytest.raises(PppoeRunbookError, match="not safe to approve") as captured:
            await executor.plan(_request())

        assert captured.value.code == "PPPOE_PLAN_WOULD_FAIL"

    asyncio.run(scenario())


def test_pppoe_post_check_failure_keeps_journal_available_for_rollback() -> None:
    async def scenario() -> None:
        backend = _Backend(verification_error=True)
        executor = PppoeRunbookExecutor(backend, "mikrotik-afe23e")
        plan = await executor.plan(_request())

        result = await executor.apply_approved(plan, PppoeSecret("isp-secret"))

        assert result.verified is False
        assert result.journal_ids == ("journal-1",)
        assert "post-check failed" in result.verification_details

    asyncio.run(scenario())


def test_pppoe_rollback_is_previewed_confirmed_and_verified() -> None:
    async def scenario() -> None:
        backend = _Backend()
        executor = PppoeRunbookExecutor(backend, "mikrotik-afe23e")

        preview = await executor.preview_rollback("journal-1")
        result = await executor.rollback_approved("journal-1", _request())

        assert preview.preview == "Would remove pppoe-wan"
        assert result.verified is True
        assert "absent" in result.verification_details
        assert backend.open_count == 2

    asyncio.run(scenario())
