from __future__ import annotations

from pathlib import Path

from mth.ui.textual.markdown import markdown_to_text
from mth.ui.textual.sessions import ChatSessionStore, SessionPaths, SessionTurn


def test_session_store_round_trip_and_latest(tmp_path: Path) -> None:
    store = ChatSessionStore(SessionPaths(file=tmp_path / "sessions.json"))
    first = store.create(
        "router-1",
        "Проверка DNS",
        (SessionTurn("Покажи DNS", "Серверы: 1.1.1.1"),),
        model="qwen",
    )
    updated = store.append_turn(
        first.session_id,
        "router-1",
        SessionTurn("Покажи ещё раз", "Готово"),
    )
    assert store.find(first.session_id, "router-1") == updated
    assert store.latest("router-1").session_id == first.session_id
    assert store.find(first.session_id, "other-router") is None

    reloaded = ChatSessionStore(SessionPaths(file=tmp_path / "sessions.json"))
    assert reloaded.find(first.session_id, "router-1").turns == updated.turns
    reloaded.delete(first.session_id, "router-1")
    assert reloaded.list("router-1") == ()


def test_markdown_renderer_preserves_text_and_bold() -> None:
    rendered = markdown_to_text("Итог: **готово** и `ether1`\n# Заголовок")
    assert rendered.plain == "Итог: готово и ether1\nЗаголовок"
    assert any(span.start == 6 and span.style == "bold white" for span in rendered.spans)
