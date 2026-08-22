from __future__ import annotations

import json
import locale
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from mth.core.mcp_client.runtime import project_root


class Language(StrEnum):
    EN = "en"
    RU = "ru"


TEXT: dict[str, dict[Language, str]] = {
    "discovery.subtitle": {
        Language.EN: "Discovery and select a RouterOS device",
        Language.RU: "Поиск и выбор устройства RouterOS",
    },
    "discovery.ready": {
        Language.EN: "Ready to search for MNDP neighbors.",
        Language.RU: "Готов к поиску соседей MNDP.",
    },
    "discovery.connect_to": {Language.EN: "Connect to", Language.RU: "Подключиться к"},
    "discovery.address": {
        Language.EN: "IP address or hostname",
        Language.RU: "IP-адрес или имя хоста",
    },
    "discovery.login": {Language.EN: "Login", Language.RU: "Логин"},
    "discovery.login_placeholder": {
        Language.EN: "RouterOS login",
        Language.RU: "Логин RouterOS",
    },
    "discovery.password": {Language.EN: "Password", Language.RU: "Пароль"},
    "discovery.password_placeholder": {
        Language.EN: "RouterOS password",
        Language.RU: "Пароль RouterOS",
    },
    "discovery.connect": {Language.EN: "Connect", Language.RU: "Подключиться"},
    "discovery.backend_idle": {
        Language.EN: "Backend not connected. Discovery data is untrusted.",
        Language.RU: "Backend не подключён. Данные discovery не являются доверенными.",
    },
    "discovery.searching": {
        Language.EN: "Searching for MNDP neighbors…",
        Language.RU: "Поиск соседей MNDP…",
    },
    "discovery.found": {
        Language.EN: "Found {count} device(s). Select a row or enter an address.{warning}",
        Language.RU: "Найдено устройств: {count}. Выберите строку или введите адрес.{warning}",
    },
    "discovery.none": {
        Language.EN: "No devices found. Press r to retry or enter an address manually.",
        Language.RU: "Устройства не найдены. Нажмите r или введите адрес вручную.",
    },
    "discovery.selected": {
        Language.EN: "Selected {identity} at {address}.",
        Language.RU: "Выбрано устройство {identity} по адресу {address}.",
    },
    "discovery.need_address": {
        Language.EN: "Enter a RouterOS address or select a discovered device.",
        Language.RU: "Введите адрес RouterOS или выберите найденное устройство.",
    },
    "discovery.need_login": {
        Language.EN: "Enter a RouterOS login.",
        Language.RU: "Введите логин RouterOS.",
    },
    "discovery.need_password": {
        Language.EN: "Enter a non-empty RouterOS password.",
        Language.RU: "Введите непустой пароль RouterOS.",
    },
    "discovery.connecting": {
        Language.EN: "Connecting to {target} through MikroMCP…",
        Language.RU: "Подключение к {target} через MikroMCP…",
    },
    "discovery.refresh": {Language.EN: "Refresh", Language.RU: "Обновить"},
    "discovery.quit": {Language.EN: "Quit", Language.RU: "Выйти"},
    "discovery.error": {
        Language.EN: "Discovery error: {message}",
        Language.RU: "Ошибка поиска: {message}",
    },
    "discovery.fingerprint_captured": {
        Language.EN: "TLS fingerprint captured. Confirm it before registration.",
        Language.RU: "TLS fingerprint получен. Подтвердите его перед регистрацией.",
    },
    "discovery.fingerprint_title": {
        Language.EN: "Verify TLS fingerprint",
        Language.RU: "Проверка TLS fingerprint",
    },
    "discovery.fingerprint_body": {
        Language.EN: "Compare this RouterOS SHA-256 fingerprint with a trusted source.",
        Language.RU: "Сверьте SHA-256 fingerprint RouterOS с доверенным источником.",
    },
    "discovery.target": {Language.EN: "Target", Language.RU: "Цель"},
    "discovery.trust": {
        Language.EN: "Trust and connect",
        Language.RU: "Доверять и подключиться",
    },
    "discovery.cancelled": {
        Language.EN: "Connection cancelled; TLS fingerprint was not trusted.",
        Language.RU: "Подключение отменено: TLS fingerprint не был подтверждён.",
    },
    "discovery.registering": {
        Language.EN: "Fingerprint trusted. Registering router with MikroMCP…",
        Language.RU: "Fingerprint подтверждён. Регистрация роутера в MikroMCP…",
    },
    "discovery.connected": {
        Language.EN: "Connected to {identity} via MikroMCP.",
        Language.RU: "Подключено к {identity} через MikroMCP.",
    },
    "discovery.backend_connected": {
        Language.EN: (
            "Router ID: {router_id} | RouterOS: {version} | CPU: {cpu} | "
            "Live MCP tools: {tool_count}"
        ),
        Language.RU: (
            "ID роутера: {router_id} | RouterOS: {version} | CPU: {cpu} | "
            "Доступно MCP-инструментов: {tool_count}"
        ),
    },
    "chat.placeholder": {
        Language.EN: "Ask about this MikroTik, or type /help",
        Language.RU: "Спросите об этом MikroTik или введите /help",
    },
    "chat.connected": {
        Language.EN: "Connected through MikroMCP. PLAN mode is active; press Tab for READY.",
        Language.RU: "Подключено через MikroMCP. Активен режим PLAN; нажмите Tab для READY.",
    },
    "chat.no_model": {
        Language.EN: "No model selected. Use /model to configure one.",
        Language.RU: "Модель не выбрана. Используйте /model для настройки.",
    },
    "chat.cleared": {
        Language.EN: "Conversation and model memory cleared.",
        Language.RU: "Диалог и память модели очищены.",
    },
    "chat.plan": {Language.EN: "PLAN", Language.RU: "PLAN"},
    "chat.ready": {
        Language.EN: "READY · reads + approved runbooks",
        Language.RU: "READY · чтение + подтверждённые runbook'и",
    },
    "chat.high_risk": {
        Language.EN: "HIGH RISK · direct CLI · no per-command approval",
        Language.RU: "HIGH RISK · прямой CLI · без подтверждения каждой команды",
    },
    "chat.tab_cycle": {Language.EN: "Tab to cycle", Language.RU: "Tab — сменить режим"},
    "inline.cancel": {Language.EN: "Cancel", Language.RU: "Отмена"},
    "inline.help": {
        Language.EN: "Esc to cancel · Tab to move",
        Language.RU: "Esc — отмена · Tab — перейти дальше",
    },
    "inline.approval_help": {
        Language.EN: "Esc to cancel · Tab to amend · Enter to choose",
        Language.RU: "Esc — отмена · Tab — изменить · Enter — выбрать",
    },
    "language.title": {Language.EN: "Interface language", Language.RU: "Язык интерфейса"},
    "language.body": {
        Language.EN: "Choose a language. The setting is saved for the next launch.",
        Language.RU: "Выберите язык. Настройка сохранится для следующего запуска.",
    },
    "language.changed": {
        Language.EN: "Interface language changed to English.",
        Language.RU: "Язык интерфейса изменён на русский.",
    },
    "model.title": {Language.EN: "Configure model", Language.RU: "Настройка модели"},
    "models.title": {Language.EN: "Saved models", Language.RU: "Сохранённые модели"},
    "sessions.title": {Language.EN: "Saved sessions", Language.RU: "Сохранённые сессии"},
    "approval.title": {
        Language.EN: "Do you want to apply this RouterOS plan?",
        Language.RU: "Применить этот план RouterOS?",
    },
    "approval.yes": {Language.EN: "1. Yes", Language.RU: "1. Да"},
    "approval.amend": {
        Language.EN: "2. Amend parameters",
        Language.RU: "2. Изменить параметры",
    },
    "approval.no": {Language.EN: "3. No", Language.RU: "3. Нет"},
    "reasoning.tokens": {Language.EN: "tokens", Language.RU: "токенов"},
    "reasoning.received": {
        Language.EN: "reasoning received",
        Language.RU: "рассуждение получено",
    },
    "reasoning.recovered": {
        Language.EN: "final answer recovered",
        Language.RU: "финальный ответ восстановлен",
    },
}


THINKING_PHRASES: dict[Language, tuple[str, ...]] = {
    Language.EN: (
        "Thinking…",
        "Thinking about not breaking anything…",
        "Counting the bytes…",
        "Tracing the packets…",
        "Checking the route twice…",
        "Negotiating with RouterOS…",
        "Reading the fine print…",
        "Keeping the rollback close…",
        "Untangling the interfaces…",
        "Measuring twice, applying once…",
        "Asking the packets politely…",
    ),
    Language.RU: (
        "Размышляю…",
        "Думаю, как ничего не сломать…",
        "Считаю байты…",
        "Прослеживаю пакеты…",
        "Перепроверяю маршрут…",
        "Договариваюсь с RouterOS…",
        "Читаю мелкий шрифт…",
        "Держу rollback под рукой…",
        "Распутываю интерфейсы…",
        "Семь раз проверяю, один применяю…",
        "Вежливо спрашиваю пакеты…",
    ),
}


def tr(language: Language, key: str, **values: object) -> str:
    translations = TEXT.get(key)
    if translations is None:
        return key.format(**values)
    template = translations.get(language, translations[Language.EN])
    return template.format(**values)


def detect_system_language() -> Language:
    override = os.environ.get("MTH_LANGUAGE", "").strip().casefold()
    if override in {Language.EN, Language.RU}:
        return Language(override)
    candidates = [
        locale.getlocale()[0],
        os.environ.get("LANG"),
        os.environ.get("LANGUAGE"),
    ]
    is_russian = any(str(item).casefold().startswith("ru") for item in candidates)
    return Language.RU if is_russian else Language.EN


@dataclass(frozen=True, slots=True)
class UiSettingsPaths:
    file: Path = field(default_factory=lambda: project_root() / ".mth" / "settings.json")


class UiSettingsStore:
    def __init__(self, paths: UiSettingsPaths | None = None) -> None:
        self.paths = paths or UiSettingsPaths()

    def language(self) -> Language:
        if not self.paths.file.exists():
            return detect_system_language()
        try:
            document = json.loads(self.paths.file.read_text(encoding="utf-8"))
            return Language(str(document.get("language", "")))
        except (OSError, ValueError, json.JSONDecodeError):
            return detect_system_language()

    def save_language(self, language: Language) -> None:
        document = self._load()
        document["version"] = 1
        document["language"] = language
        self._write(document)

    def last_address(self) -> str:
        value = self._load().get("last_address")
        return str(value).strip() if isinstance(value, str) else ""

    def save_last_address(self, address: str) -> None:
        document = self._load()
        document["version"] = 1
        document["last_address"] = address.strip()
        self._write(document)

    def _load(self) -> dict[str, Any]:
        if not self.paths.file.exists():
            return {"version": 1, "language": detect_system_language()}
        try:
            loaded = json.loads(self.paths.file.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {"version": 1, "language": detect_system_language()}
        return loaded if isinstance(loaded, dict) else {"version": 1}

    def _write(self, document: dict[str, Any]) -> None:
        path = self.paths.file
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(document, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)
