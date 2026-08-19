from __future__ import annotations

import builtins
import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mth.core.mcp_client.runtime import project_root
from mth.core.runbooks.base import RunbookPlan, RunbookStep


@dataclass(frozen=True, slots=True)
class RunbookHistoryPaths:
    file: Path = field(
        default_factory=lambda: project_root() / ".mth" / "runbook-history.json"
    )


@dataclass(frozen=True, slots=True)
class RunbookExecutionRecord:
    plan: RunbookPlan
    router_id: str
    journal_ids: tuple[str, ...]
    status: str
    created_at: str

    @property
    def execution_id(self) -> str:
        return self.plan.plan_id


class RunbookHistoryStore:
    """Persistent secret-free runbook history used to make rollback restart-safe."""

    def __init__(self, paths: RunbookHistoryPaths | None = None) -> None:
        self.paths = paths or RunbookHistoryPaths()

    def record(
        self,
        plan: RunbookPlan,
        router_id: str,
        journal_ids: tuple[str, ...],
        *,
        status: str,
    ) -> RunbookExecutionRecord:
        record = RunbookExecutionRecord(
            plan=plan,
            router_id=router_id,
            journal_ids=journal_ids,
            status=status,
            created_at=datetime.now(UTC).isoformat(),
        )
        records = [item for item in self.list() if item.execution_id != record.execution_id]
        records.append(record)
        self._write(records)
        return record

    def list(self, router_id: str | None = None) -> tuple[RunbookExecutionRecord, ...]:
        records = tuple(self._decode(item) for item in self._load())
        if router_id is None:
            return records
        return tuple(record for record in records if record.router_id == router_id)

    def find(self, token: str, router_id: str) -> RunbookExecutionRecord | None:
        candidates = self.list(router_id)
        if not token and candidates:
            return next(
                (record for record in reversed(candidates) if record.status != "rolled_back"),
                None,
            )
        return next(
            (
                record
                for record in reversed(candidates)
                if record.execution_id == token or token in record.journal_ids
            ),
            None,
        )

    def mark_rolled_back(self, execution_id: str) -> None:
        records = [*self.list()]
        updated = [
            replace(record, status="rolled_back")
            if record.execution_id == execution_id
            else record
            for record in records
        ]
        self._write(updated)

    def _load(self) -> builtins.list[dict[str, Any]]:
        if not self.paths.file.exists():
            return []
        loaded = json.loads(self.paths.file.read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            raise ValueError("runbook history must be a JSON list")
        return [dict(item) for item in loaded if isinstance(item, dict)]

    def _write(self, records: builtins.list[RunbookExecutionRecord]) -> None:
        payload = [self._encode(record) for record in records]
        content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        path = self.paths.file
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    @staticmethod
    def _encode(record: RunbookExecutionRecord) -> dict[str, Any]:
        return {
            "executionId": record.execution_id,
            "routerId": record.router_id,
            "runbookId": record.plan.runbook_id,
            "title": record.plan.title,
            "values": record.plan.values,
            "baseline": record.plan.baseline,
            "steps": [step.as_mcp() for step in record.plan.steps],
            "preview": record.plan.preview,
            "summary": record.plan.summary,
            "journalIds": list(record.journal_ids),
            "status": record.status,
            "createdAt": record.created_at,
        }

    @staticmethod
    def _decode(raw: dict[str, Any]) -> RunbookExecutionRecord:
        steps_raw = raw.get("steps")
        if not isinstance(steps_raw, list):
            raise ValueError("runbook history record has invalid steps")
        steps: list[RunbookStep] = []
        for item in steps_raw:
            if not isinstance(item, dict):
                raise ValueError("runbook history step must be an object")
            tool = item.get("tool")
            params = item.get("params")
            if not isinstance(tool, str) or not isinstance(params, dict):
                raise ValueError("runbook history step is malformed")
            steps.append(RunbookStep(tool, dict(params)))
        values = raw.get("values")
        baseline = raw.get("baseline")
        journals = raw.get("journalIds")
        if not isinstance(values, dict) or not isinstance(baseline, dict):
            raise ValueError("runbook history values or baseline are malformed")
        if not isinstance(journals, list) or not all(
            isinstance(item, str) for item in journals
        ):
            raise ValueError("runbook history journal IDs are malformed")
        plan = RunbookPlan(
            plan_id=str(raw.get("executionId", "")),
            runbook_id=str(raw.get("runbookId", "")),
            title=str(raw.get("title", "")),
            values=dict(values),
            baseline=dict(baseline),
            steps=tuple(steps),
            preview=str(raw.get("preview", "")),
            summary=str(raw.get("summary", "")),
        )
        return RunbookExecutionRecord(
            plan=plan,
            router_id=str(raw.get("routerId", "")),
            journal_ids=tuple(journals),
            status=str(raw.get("status", "unknown")),
            created_at=str(raw.get("createdAt", "")),
        )
