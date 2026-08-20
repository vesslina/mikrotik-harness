from __future__ import annotations

import json
import os
import tempfile
import uuid
from builtins import list as builtins_list
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mth.core.mcp_client.runtime import project_root


@dataclass(frozen=True, slots=True)
class SessionPaths:
    file: Path = field(default_factory=lambda: project_root() / ".mth" / "sessions.json")


@dataclass(frozen=True, slots=True)
class SessionTurn:
    prompt: str
    response: str


@dataclass(frozen=True, slots=True)
class ChatSession:
    session_id: str
    router_id: str
    title: str
    created_at: str
    updated_at: str
    model: str | None
    turns: tuple[SessionTurn, ...]

    @property
    def last_prompt(self) -> str:
        return self.turns[-1].prompt if self.turns else ""


class ChatSessionStore:
    """Small atomic, secret-free store for resumable agent conversations."""

    def __init__(self, paths: SessionPaths | None = None) -> None:
        self.paths = paths or SessionPaths()

    def list(self, router_id: str | None = None) -> tuple[ChatSession, ...]:
        sessions = tuple(self._decode(item) for item in self._load())
        if router_id is not None:
            sessions = tuple(item for item in sessions if item.router_id == router_id)
        return tuple(sorted(sessions, key=lambda item: item.updated_at, reverse=True))

    def find(self, session_id: str, router_id: str | None = None) -> ChatSession | None:
        return next(
            (
                item
                for item in self.list(router_id)
                if item.session_id == session_id
            ),
            None,
        )

    def latest(self, router_id: str) -> ChatSession | None:
        sessions = self.list(router_id)
        return sessions[0] if sessions else None

    def create(
        self,
        router_id: str,
        title: str,
        turns: Iterable[SessionTurn],
        *,
        model: str | None = None,
    ) -> ChatSession:
        now = datetime.now(UTC).isoformat()
        session = ChatSession(
            session_id=f"session-{uuid.uuid4().hex[:12]}",
            router_id=router_id,
            title=title,
            created_at=now,
            updated_at=now,
            model=model,
            turns=tuple(turns),
        )
        self._write((*self.list(), session))
        return session

    def append_turn(self, session_id: str, router_id: str, turn: SessionTurn) -> ChatSession:
        session = self.find(session_id, router_id)
        if session is None:
            raise KeyError(session_id)
        updated = replace(
            session,
            updated_at=datetime.now(UTC).isoformat(),
            turns=(*session.turns, turn),
        )
        self._replace(updated)
        return updated

    def append_response(self, session_id: str, router_id: str, response: str) -> ChatSession:
        session = self.find(session_id, router_id)
        if session is None:
            raise KeyError(session_id)
        if not session.turns:
            return self.append_turn(session_id, router_id, SessionTurn("", response))
        turns = (*session.turns[:-1], replace(session.turns[-1], response=(
            f"{session.turns[-1].response}\n\n{response}"
            if session.turns[-1].response
            else response
        )))
        updated = replace(session, updated_at=datetime.now(UTC).isoformat(), turns=turns)
        self._replace(updated)
        return updated

    def delete(self, session_id: str, router_id: str) -> None:
        session = self.find(session_id, router_id)
        if session is None:
            raise KeyError(session_id)
        self._write(item for item in self.list() if item.session_id != session_id)

    def _replace(self, updated: ChatSession) -> None:
        self._write(
            updated if item.session_id == updated.session_id else item
            for item in self.list()
        )

    def _load(self) -> builtins_list[dict[str, Any]]:
        if not self.paths.file.exists():
            return []
        loaded = json.loads(self.paths.file.read_text(encoding="utf-8"))
        if not isinstance(loaded, builtins_list):
            raise ValueError("sessions store must be a JSON list")
        return [dict(item) for item in loaded if isinstance(item, dict)]

    def _write(self, sessions: Iterable[ChatSession]) -> None:
        payload = [self._encode(session) for session in sessions]
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
    def _encode(session: ChatSession) -> dict[str, Any]:
        return {
            "sessionId": session.session_id,
            "routerId": session.router_id,
            "title": session.title,
            "createdAt": session.created_at,
            "updatedAt": session.updated_at,
            "model": session.model,
            "turns": [
                {"prompt": turn.prompt, "response": turn.response}
                for turn in session.turns
            ],
        }

    @staticmethod
    def _decode(raw: dict[str, Any]) -> ChatSession:
        raw_turns = raw.get("turns", [])
        if not isinstance(raw_turns, list):
            raise ValueError("session turns must be a JSON list")
        turns = tuple(
            SessionTurn(str(item.get("prompt", "")), str(item.get("response", "")))
            for item in raw_turns
            if isinstance(item, dict)
        )
        return ChatSession(
            session_id=str(raw.get("sessionId", "")),
            router_id=str(raw.get("routerId", "")),
            title=str(raw.get("title", "Untitled session")),
            created_at=str(raw.get("createdAt", "")),
            updated_at=str(raw.get("updatedAt", "")),
            model=str(raw["model"]) if raw.get("model") is not None else None,
            turns=turns,
        )
