from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from rich.padding import Padding
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import Button, Checkbox, Input, Label, OptionList, RichLog, Select, Static
from textual.widgets.option_list import Option

from mth import __version__
from mth.agent import (
    AgentEvent,
    AgentMessage,
    AgentMode,
    FinalOutcome,
    FinalSummary,
    ModelCapabilities,
    OpenAICompatibleClient,
    PlannedAction,
    ProviderError,
    ProviderKind,
    ProviderPreset,
    ProviderPresetStore,
    ProviderWarmup,
    ReadOnlyAgentLoop,
    ReasoningControl,
    ReasoningStatus,
    RunbookProposal,
    ToolCall,
    ToolCallFormat,
    ToolResult,
    VerificationResult,
)
from mth.core.mcp_client import MikroMcpClient
from mth.core.registration import MikroMcpConfigStore, RegistrationResult
from mth.core.runbooks import (
    DEFAULT_RUNBOOK_REGISTRY,
    RunbookApplyResult,
    RunbookDefinition,
    RunbookError,
    RunbookExecutionRecord,
    RunbookExecutor,
    RunbookFieldKind,
    RunbookHistoryStore,
    RunbookPlan,
    RunbookRegistry,
    RunbookRollbackPreview,
    RunbookRollbackResult,
    RunbookSubmission,
)
from mth.ui.textual.i18n import (
    THINKING_PHRASES,
    Language,
    UiSettingsStore,
    tr,
)


@dataclass(frozen=True, slots=True)
class ChatProfile:
    router_id: str
    address: str
    identity: str
    version: str
    board: str
    mac: str
    tool_count: int


class AgentRunner(Protocol):
    async def run(self, prompt: str, mode: AgentMode) -> tuple[AgentEvent, ...]: ...


AgentFactory = Callable[[ProviderPreset, str | None], AgentRunner]


class RunbookRunner(Protocol):
    definition: RunbookDefinition

    async def plan(self, submission: RunbookSubmission) -> RunbookPlan: ...

    async def apply_approved(
        self,
        plan: RunbookPlan,
        secrets: Mapping[str, str] | None = None,
    ) -> RunbookApplyResult: ...

    async def preview_rollback(
        self, journal_ids: Sequence[str]
    ) -> RunbookRollbackPreview: ...

    async def rollback_approved(
        self,
        plan: RunbookPlan,
        journal_ids: Sequence[str],
    ) -> RunbookRollbackResult: ...


RunbookFactory = Callable[[RunbookDefinition], RunbookRunner]


@dataclass(frozen=True, slots=True)
class ModelSelection:
    preset: ProviderPreset
    api_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ModelPickerResult:
    preset: ProviderPreset
    delete: bool = False


@dataclass(frozen=True, slots=True)
class RunbookSelection:
    submission: RunbookSubmission = field(repr=False)


class PixelLogo(Static):
    GLYPHS = {
        "A": ("01110", "10001", "11111", "10001", "10001"),
        "E": ("11111", "10000", "11110", "10000", "11111"),
        "H": ("10001", "10001", "11111", "10001", "10001"),
        "I": ("111", "010", "010", "010", "111"),
        "K": ("10001", "10010", "11100", "10010", "10001"),
        "M": ("10001", "11011", "10101", "10001", "10001"),
        "N": ("10001", "11001", "10101", "10011", "10001"),
        "O": ("01110", "10001", "10001", "10001", "01110"),
        "R": ("11110", "10001", "11110", "10010", "10001"),
        "S": ("01111", "10000", "01110", "00001", "11110"),
        "T": ("11111", "00100", "00100", "00100", "00100"),
    }

    @classmethod
    def _pixels(cls, value: str) -> set[tuple[int, int]]:
        pixels: set[tuple[int, int]] = set()
        offset = 0
        for letter in value:
            glyph = cls.GLYPHS[letter]
            for row, line in enumerate(glyph):
                pixels.update(
                    (row, offset + column)
                    for column, pixel in enumerate(line)
                    if pixel == "1"
                )
            offset += len(glyph[0]) + 1
        return pixels

    @classmethod
    def _shadowed_word(cls, value: str, foreground: str, shadow: str) -> Text:
        pixels = cls._pixels(value)
        shadows = {(row + 1, column + 1) for row, column in pixels}
        width = max(column for _, column in shadows | pixels) + 1
        output = Text()
        for row in range(6):
            for column in range(width):
                point = (row, column)
                if point in pixels:
                    output.append("█", style=f"bold {foreground} on #090909")
                elif point in shadows:
                    output.append("▓", style=f"{shadow} on #090909")
                else:
                    output.append(" ", style="on #090909")
            if row < 5:
                output.append("\n", style="on #090909")
        return output

    def render(self) -> Text:
        output = Text()
        output.append_text(self._shadowed_word("MIKROTIK", "white", "#5c5c5c"))
        output.append("\n\n", style="on #090909")
        output.append_text(self._shadowed_word("HARNESS", "#ff3b30", "#681d1d"))
        return output


class ModelWizardScreen(ModalScreen[ModelSelection | None]):
    DEFAULT_URLS = {
        ProviderKind.LM_STUDIO: "http://127.0.0.1:1234/v1",
        ProviderKind.OPENROUTER: "https://openrouter.ai/api/v1",
        ProviderKind.OPENAI_COMPATIBLE: "",
    }

    CSS = """
    ModelWizardScreen { align: center middle; }
    #model-dialog {
        width: 84;
        height: auto;
        max-height: 36;
        padding: 1 2;
        border: round #ff3b30;
        background: #111315;
    }
    #model-dialog .model-row { height: 3; }
    #model-dialog .model-option { height: 3; padding-left: 22; }
    #model-dialog .field-label {
        width: 22;
        height: 3;
        content-align: left middle;
        color: #aeb4ba;
    }
    #model-dialog Input, #provider-kind { width: 1fr; }
    #model-error { height: auto; min-height: 1; margin-top: 1; color: #ff6b62; }
    #model-actions { height: auto; margin-top: 1; align-horizontal: right; }
    #model-actions Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="model-dialog"):
            yield Static("Select model provider", classes="dialog-title")
            with Horizontal(classes="model-row"):
                yield Label("Provider", classes="field-label")
                yield Select(
                    (
                        ("Local — LM Studio", ProviderKind.LM_STUDIO),
                        ("OpenRouter", ProviderKind.OPENROUTER),
                        ("Custom OpenAI-compatible", ProviderKind.OPENAI_COMPATIBLE),
                    ),
                    value=ProviderKind.OPENROUTER,
                    allow_blank=False,
                    id="provider-kind",
                )
            with Horizontal(classes="model-row"):
                yield Label("Preset name", classes="field-label")
                yield Input(value="openrouter", id="preset-name")
            with Horizontal(classes="model-row"):
                yield Label("Base URL", classes="field-label")
                yield Input(
                    value=self.DEFAULT_URLS[ProviderKind.OPENROUTER],
                    id="provider-url",
                )
            with Horizontal(classes="model-row"):
                yield Label("Model", classes="field-label")
                yield Input(
                    placeholder="provider/model-name (OpenAI-format tools required)",
                    id="model-name",
                )
            with Horizontal(classes="model-row"):
                yield Label("API key (encrypted)", classes="field-label")
                yield Input(
                    password=True,
                    placeholder="saved per preset; optional for local providers",
                    id="api-key",
                )
            with Horizontal(classes="model-row"):
                yield Label("API-key env variable", classes="field-label")
                yield Input(placeholder="OPENROUTER_API_KEY", id="api-key-env")
            with Horizontal(classes="model-row"):
                yield Label("Max context tokens", classes="field-label")
                yield Input(value="32768", id="context-tokens", type="integer")
            with Horizontal(classes="model-option"):
                yield Checkbox(
                    "Expose secrets to this LLM (loopback endpoints only)",
                    value=False,
                    id="allow-sensitive-tool-data",
                )
            yield Static("", id="model-error", markup=False)
            with Horizontal(id="model-actions"):
                yield Button("Cancel", id="cancel-model")
                yield Button("Use model", id="save-model", variant="primary")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "provider-kind" or not isinstance(event.value, ProviderKind):
            return
        self.query_one("#provider-url", Input).value = self.DEFAULT_URLS[event.value]
        default_name = {
            ProviderKind.LM_STUDIO: "local",
            ProviderKind.OPENROUTER: "openrouter",
            ProviderKind.OPENAI_COMPATIBLE: "custom",
        }[event.value]
        self.query_one("#preset-name", Input).value = default_name

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-model":
            self.dismiss(None)
            return
        if event.button.id != "save-model":
            return
        self._save()

    def _save(self) -> None:
        provider = self.query_one("#provider-kind", Select).value
        if not isinstance(provider, ProviderKind):
            self._error("Select a provider.")
            return
        try:
            context_tokens = int(self.query_one("#context-tokens", Input).value)
            preset = ProviderPreset(
                name=self.query_one("#preset-name", Input).value.strip(),
                provider=provider,
                base_url=self.query_one("#provider-url", Input).value.strip(),
                model=self.query_one("#model-name", Input).value.strip(),
                api_key_env=self.query_one("#api-key-env", Input).value.strip() or None,
                allow_sensitive_tool_data=self.query_one(
                    "#allow-sensitive-tool-data", Checkbox
                ).value,
                capabilities=ModelCapabilities(
                    supports_tools=True,
                    supports_streaming=False,
                    supports_reasoning=False,
                    supports_json_schema=False,
                    max_context_tokens=context_tokens,
                    reasoning_control=ReasoningControl.NONE,
                    tool_call_format=ToolCallFormat.OPENAI,
                ),
            )
            preset.require_agent_loop_support()
        except (TypeError, ValueError) as error:
            self._error(str(error))
            return
        api_key = self.query_one("#api-key", Input).value or None
        self.dismiss(ModelSelection(preset=preset, api_key=api_key))

    def _error(self, message: str) -> None:
        self.query_one("#model-error", Static).update(message)


class ModelPickerScreen(ModalScreen[ModelPickerResult | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    CSS = """
    ModelPickerScreen { align: center middle; }
    #model-picker-dialog {
        width: 88;
        height: auto;
        max-height: 24;
        padding: 1 2;
        border: round #ff3b30;
        background: #111315;
    }
    #model-picker-title { height: 2; color: white; text-style: bold; }
    #saved-model-list {
        height: auto;
        max-height: 14;
        border: solid #40464d;
        background: #090909;
    }
    #model-picker-help { height: 2; padding-top: 1; color: #8b949e; }
    #model-picker-actions { height: auto; margin-top: 1; align-horizontal: right; }
    #model-picker-actions Button { margin-left: 1; }
    """

    def __init__(
        self,
        presets: tuple[ProviderPreset, ...],
        selected_name: str | None,
    ) -> None:
        super().__init__()
        self._presets = presets
        self._selected_name = selected_name

    def compose(self) -> ComposeResult:
        options = [
            Option(
                f"{preset.name}  ·  {preset.provider}  ·  {preset.model}\n"
                f"    {preset.base_url}"
            )
            for preset in self._presets
        ]
        with Vertical(id="model-picker-dialog"):
            yield Static("Saved models", id="model-picker-title")
            yield OptionList(*options, id="saved-model-list", markup=False)
            yield Static("↑/↓ choose  ·  Enter activate  ·  Esc cancel", id="model-picker-help")
            with Horizontal(id="model-picker-actions"):
                yield Button("Cancel", id="cancel-picker")
                yield Button("Delete selected", id="delete-saved-model", variant="error")
                yield Button("Use model", id="use-saved-model", variant="primary")

    def on_mount(self) -> None:
        option_list = self.query_one("#saved-model-list", OptionList)
        selected_index = next(
            (
                index
                for index, preset in enumerate(self._presets)
                if preset.name == self._selected_name
            ),
            0,
        )
        option_list.highlighted = selected_index
        option_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(ModelPickerResult(self._presets[event.option_index]))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-picker":
            self.dismiss(None)
        elif event.button.id in {"use-saved-model", "delete-saved-model"}:
            option_list = self.query_one("#saved-model-list", OptionList)
            index = option_list.highlighted
            if index is not None:
                self.dismiss(
                    ModelPickerResult(
                        self._presets[index],
                        delete=event.button.id == "delete-saved-model",
                    )
                )

    def action_cancel(self) -> None:
        self.dismiss(None)


class RunbookWizardScreen(ModalScreen[RunbookSelection | None]):
    CSS = """
    RunbookWizardScreen, PppoeWizardScreen { align: center middle; }
    #runbook-dialog {
        width: 78;
        height: auto;
        max-height: 38;
        padding: 1 2;
        border: round #ff3b30;
        background: #111315;
    }
    #runbook-dialog .runbook-row { height: 3; }
    #runbook-dialog .field-label { width: 24; content-align: left middle; color: #aeb4ba; }
    #runbook-dialog Input { width: 1fr; }
    #runbook-error { height: auto; min-height: 1; color: #ff6b62; }
    #runbook-actions { height: auto; margin-top: 1; align-horizontal: right; }
    #runbook-actions Button { margin-left: 1; }
    """

    def __init__(
        self,
        definition: RunbookDefinition,
        proposal: RunbookProposal | None = None,
    ) -> None:
        super().__init__()
        self.definition = definition
        self._proposal = proposal

    @staticmethod
    def field_id(name: str) -> str:
        return "runbook-field-" + "".join(
            character.lower() if character.isalnum() else "-" for character in name
        )

    def _initial(self, name: str, default: object) -> object:
        if self._proposal is not None and name in self._proposal.parameters:
            return self._proposal.parameters[name]
        return default

    def compose(self) -> ComposeResult:
        with Vertical(id="runbook-dialog"):
            yield Static(f"{self.definition.title} runbook", classes="dialog-title")
            yield Static(
                (
                    "The LLM proposed editable values; no change has been made. "
                    "Secret fields stay masked and are never sent to the LLM."
                    if self._proposal is not None
                    else "Review every value before building the live dry-run plan."
                ),
                markup=False,
            )
            for spec in self.definition.fields:
                initial = self._initial(spec.name, spec.default)
                with Horizontal(classes="runbook-row"):
                    yield Label(spec.label, classes="field-label")
                    if spec.kind is RunbookFieldKind.BOOLEAN:
                        yield Checkbox(
                            spec.description or spec.label,
                            value=initial if isinstance(initial, bool) else False,
                            id=self.field_id(spec.name),
                        )
                    else:
                        if isinstance(initial, (list, tuple)):
                            value = ", ".join(str(item) for item in initial)
                        else:
                            value = initial if isinstance(initial, str) else ""
                        yield Input(
                            value=value,
                            password=spec.kind is RunbookFieldKind.SECRET,
                            placeholder=spec.placeholder,
                            id=self.field_id(spec.name),
                        )
            yield Static("", id="runbook-error", markup=False)
            with Horizontal(id="runbook-actions"):
                yield Button("Cancel", id="cancel-runbook")
                yield Button("Build dry-run plan", id="plan-runbook", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-runbook":
            self.dismiss(None)
            return
        if event.button.id != "plan-runbook":
            return
        raw: dict[str, object] = {}
        for spec in self.definition.fields:
            selector = f"#{self.field_id(spec.name)}"
            if spec.kind is RunbookFieldKind.BOOLEAN:
                raw[spec.name] = self.query_one(selector, Checkbox).value
            else:
                raw[spec.name] = self.query_one(selector, Input).value
        try:
            submission = self.definition.parse_submission(raw)
        except ValueError as error:
            self.query_one("#runbook-error", Static).update(str(error))
            return
        self.dismiss(RunbookSelection(submission))


class PppoeWizardScreen(RunbookWizardScreen):
    """Compatibility name for integrations that opened the original PPPoE modal."""

    def __init__(self, proposal: RunbookProposal | None = None) -> None:
        super().__init__(DEFAULT_RUNBOOK_REGISTRY.get("wan_pppoe"), proposal)


class ApprovalScreen(ModalScreen[bool]):
    CSS = """
    ApprovalScreen { align: center middle; }
    #approval-dialog {
        width: 82;
        height: auto;
        max-height: 28;
        padding: 1 2;
        border: double #ffb454;
        background: #111315;
    }
    #approval-title { height: 2; color: #ffb454; text-style: bold; }
    #approval-summary { height: auto; max-height: 18; color: white; }
    #approval-actions { height: auto; margin-top: 1; align-horizontal: right; }
    #approval-actions Button { margin-left: 1; }
    """

    def __init__(
        self,
        summary: str,
        *,
        title: str = "Approve RouterOS change",
        action_label: str = "Apply approved plan",
    ) -> None:
        super().__init__()
        self._summary = summary
        self._title = title
        self._action_label = action_label

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-dialog"):
            yield Static(self._title, id="approval-title")
            yield Static(self._summary, id="approval-summary", markup=False)
            with Horizontal(id="approval-actions"):
                yield Button("Cancel", id="cancel-approval")
                yield Button(self._action_label, id="approve-plan", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "approve-plan")


class ChatScreen(Screen[None]):
    MAX_RUNBOOK_FIELDS = 12
    SLASH_COMMANDS = (
        ("/help", "commands"),
        ("/info", "info"),
        ("/model", "model"),
        ("/models", "models"),
        ("/language", "language"),
        ("/pppoe", "pppoe"),
        ("/bridge", "bridge"),
        ("/dhcp", "dhcp"),
        ("/dns", "dns"),
        ("/nat", "nat"),
        ("/services", "services"),
        ("/wireguard", "wireguard"),
        ("/rollback", "rollback"),
        ("/log", "log"),
        ("/clear", "clear"),
        ("/exit", "exit"),
    )
    COMMAND_DESCRIPTIONS = {
        "commands": ("show commands", "показать команды"),
        "info": ("router and session info", "информация о роутере и сессии"),
        "model": ("add or edit a model", "добавить или изменить модель"),
        "models": ("choose a saved model", "выбрать сохранённую модель"),
        "language": ("change interface language", "изменить язык интерфейса"),
        "pppoe": ("configure WAN PPPoE safely", "безопасно настроить WAN PPPoE"),
        "bridge": ("create a LAN bridge safely", "безопасно создать LAN bridge"),
        "dhcp": ("create a DHCP pool and server", "создать DHCP pool и server"),
        "dns": ("configure DNS resolver", "настроить DNS resolver"),
        "nat": ("configure WAN masquerade", "настроить WAN masquerade"),
        "services": ("disable unnecessary services", "отключить ненужные сервисы"),
        "wireguard": ("create a WireGuard peer", "создать WireGuard peer"),
        "rollback": ("rollback a runbook", "откатить runbook"),
        "log": ("session log info", "информация о журнале"),
        "clear": ("clear transcript and memory", "очистить чат и память"),
        "exit": ("return to discovery", "вернуться к discovery"),
    }
    RUNBOOK_LABELS_RU = {
        "Client name": "Имя клиента",
        "Parent interface": "Родительский интерфейс",
        "ISP username": "Логин провайдера",
        "ISP password": "Пароль провайдера",
        "Service name": "Имя сервиса",
        "Add default route": "Добавить default route",
        "Dial on demand": "Подключаться по запросу",
        "Bridge name": "Имя bridge",
        "Member interfaces": "Интерфейсы bridge",
        "Comment": "Комментарий",
        "Create disabled": "Создать отключённым",
        "DHCP server name": "Имя DHCP server",
        "LAN interface": "LAN-интерфейс",
        "Pool name": "Имя pool",
        "Lease range": "Диапазон адресов",
        "Lease time": "Время аренды",
        "DHCP network exists": "DHCP network существует",
        "Upstream DNS servers": "Внешние DNS-серверы",
        "Serve LAN DNS queries": "Отвечать на LAN DNS-запросы",
        "Maximum cache TTL": "Максимальный TTL кэша",
        "WAN interface": "WAN-интерфейс",
        "Source network": "Исходная сеть",
        "Rule comment": "Комментарий правила",
        "Services to disable": "Сервисы для отключения",
        "Interface name": "Имя интерфейса",
        "Listen port": "Порт прослушивания",
        "Peer public key": "Публичный ключ peer",
        "Peer allowed address": "Разрешённый адрес peer",
        "Peer endpoint": "Endpoint peer",
    }

    BINDINGS = [
        Binding("tab", "cycle_mode", "Cycle mode", show=False, priority=True),
        Binding("escape", "cancel_interaction", "Cancel", show=False, priority=True),
        Binding("ctrl+o", "tool_details", "Tool details", show=False),
        Binding("ctrl+l", "clear_chat", "Clear", show=False),
    ]

    CSS = """
    ChatScreen { background: #090909; color: #e7e7e7; }
    #chat-header {
        height: 15;
        padding: 1 2;
        background: #090909;
        border-bottom: solid #5b6268;
    }
    #brand { width: 48; min-width: 48; height: 13; background: #090909; }
    #device-info { width: 1fr; height: 13; padding-left: 2; color: #9aa2aa; }
    #transcript { height: 1fr; padding: 1 3; background: #090909; scrollbar-color: #5b6268; }
    #activity-line {
        display: none;
        height: 2;
        padding: 0 3;
        color: #ff8a73;
        background: #090909;
    }
    #composer-shell { height: auto; background: #090909; }
    #composer {
        height: 3;
        margin: 0 2;
        border-top: solid #5b6268;
        border-bottom: solid #5b6268;
        background: #090909;
    }
    #prompt-mark { width: 3; height: 3; padding: 0 0 0 1; content-align: left middle; }
    #chat-input { width: 1fr; height: 3; border: none; background: #090909; color: white; }
    #chat-input:focus { border: none; }
    #command-hints {
        display: none;
        height: auto;
        max-height: 4;
        padding: 0 3 1 3;
        color: #9aa2aa;
        background: #090909;
    }
    #mode-line { height: 3; padding: 1 3; color: #6fd3df; background: #090909; }
    #interaction-panel {
        display: none;
        height: auto;
        max-height: 22;
        padding: 1 3;
        border-top: solid #5b6268;
        background: #090909;
        scrollbar-color: #5b6268;
    }
    .interaction-view { display: none; height: auto; }
    .interaction-title { height: 2; color: white; text-style: bold; }
    .interaction-body { height: auto; max-height: 13; color: #d8d8d8; }
    .interaction-help { height: 2; padding-top: 1; color: #8b949e; }
    .inline-row { height: 3; }
    .inline-label { width: 24; height: 3; content-align: left middle; color: #aeb4ba; }
    .inline-row Input, .inline-row Select { width: 1fr; }
    .inline-option { height: 3; padding-left: 24; }
    .inline-error { height: auto; min-height: 1; color: #ff6b62; }
    .inline-actions { height: auto; align-horizontal: right; }
    .inline-actions Button { margin-left: 1; }
    #inline-models-list { height: auto; max-height: 12; border: none; background: #090909; }
    #inline-approval-options, #inline-language-options {
        height: auto;
        max-height: 7;
        border: none;
        background: #090909;
    }
    .runbook-inline-row { display: none; height: 3; }
    .runbook-inline-row Input { width: 1fr; }
    """

    def __init__(
        self,
        profile: ChatProfile,
        registration: RegistrationResult,
        *,
        preset_store: ProviderPresetStore | None = None,
        agent_factory: AgentFactory | None = None,
        runbook_registry: RunbookRegistry = DEFAULT_RUNBOOK_REGISTRY,
        runbook_factory: RunbookFactory | None = None,
        history_store: RunbookHistoryStore | None = None,
        settings_store: UiSettingsStore | None = None,
        language: Language | None = None,
    ) -> None:
        super().__init__()
        self.profile = profile
        self.registration = registration
        self._preset_store = preset_store or ProviderPresetStore()
        self._agent_factory = agent_factory or self._default_agent_factory
        self._runbooks = runbook_registry
        self._runbook_factory = runbook_factory or self._default_runbook_factory
        self._history = history_store or RunbookHistoryStore()
        self._settings = settings_store or UiSettingsStore()
        self._language = language or self._settings.language()
        self._preset: ProviderPreset | None = None
        self._agent: AgentRunner | None = None
        self._mode = AgentMode.PLAN
        self._session_api_keys: dict[str, str] = {}
        self._pending_runbook: (
            tuple[RunbookRunner, RunbookPlan, dict[str, str]] | None
        ) = None
        self._pending_rollback: (
            tuple[RunbookRunner, RunbookExecutionRecord] | None
        ) = None
        self._pending_model_delete: ProviderPreset | None = None
        self._interaction: str | None = None
        self._approval_callback: Callable[[bool | None], None] | None = None
        self._approval_amend: Callable[[], None] | None = None
        self._runbook_form_definition: RunbookDefinition | None = None
        self._runbook_draft: RunbookSubmission | None = None
        self._model_picker_presets: tuple[ProviderPreset, ...] = ()
        self._activity_timer: Timer | None = None
        self._activity_started = 0.0
        self._activity_ticks = 0
        self._phrase_cursor = 0
        self._last_activity_elapsed = 0
        self._tool_trace: list[str] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="chat-header"):
            yield PixelLogo(id="brand")
            yield Static("", id="device-info", markup=False)
        yield RichLog(id="transcript", wrap=True, markup=False, auto_scroll=True)
        yield Static("", id="activity-line", markup=False)
        with VerticalScroll(id="interaction-panel"):
            with Vertical(id="inline-model-view", classes="interaction-view"):
                yield Static("", id="inline-model-title", classes="interaction-title")
                with Horizontal(classes="inline-row"):
                    yield Label("Provider", id="inline-provider-label", classes="inline-label")
                    yield Select(
                        (
                            ("Local — LM Studio", ProviderKind.LM_STUDIO),
                            ("OpenRouter", ProviderKind.OPENROUTER),
                            ("Custom OpenAI-compatible", ProviderKind.OPENAI_COMPATIBLE),
                        ),
                        value=ProviderKind.OPENROUTER,
                        allow_blank=False,
                        id="inline-provider-kind",
                    )
                for label, widget_id, value, placeholder, password in (
                    ("Preset name", "inline-preset-name", "openrouter", "", False),
                    (
                        "Base URL",
                        "inline-provider-url",
                        ModelWizardScreen.DEFAULT_URLS[ProviderKind.OPENROUTER],
                        "",
                        False,
                    ),
                    ("Model", "inline-model-name", "", "provider/model-name", False),
                    ("API key", "inline-api-key", "", "saved encrypted", True),
                    ("API-key env", "inline-api-key-env", "", "OPENROUTER_API_KEY", False),
                    ("Max context", "inline-context-tokens", "32768", "", False),
                ):
                    with Horizontal(classes="inline-row"):
                        yield Label(label, id=f"{widget_id}-label", classes="inline-label")
                        yield Input(
                            value=value,
                            placeholder=placeholder,
                            password=password,
                            id=widget_id,
                        )
                with Horizontal(classes="inline-option"):
                    yield Checkbox(
                        "Expose secrets to this LLM (loopback only)",
                        id="inline-allow-sensitive",
                    )
                yield Static("", id="inline-model-error", classes="inline-error")
                with Horizontal(classes="inline-actions"):
                    yield Button("Cancel", id="inline-cancel-model")
                    yield Button("Use model", id="inline-save-model", variant="primary")
                yield Static("", id="inline-model-help", classes="interaction-help")
            with Vertical(id="inline-models-view", classes="interaction-view"):
                yield Static("", id="inline-models-title", classes="interaction-title")
                yield OptionList(id="inline-models-list", markup=False)
                with Horizontal(classes="inline-actions"):
                    yield Button("Cancel", id="inline-cancel-models")
                    yield Button("Delete selected", id="inline-delete-model", variant="error")
                    yield Button("Use model", id="inline-use-model", variant="primary")
                yield Static("", id="inline-models-help", classes="interaction-help")
            with Vertical(id="inline-runbook-view", classes="interaction-view"):
                yield Static("", id="inline-runbook-title", classes="interaction-title")
                yield Static("", id="inline-runbook-body", classes="interaction-body", markup=False)
                for index in range(self.MAX_RUNBOOK_FIELDS):
                    with Horizontal(id=f"inline-runbook-row-{index}", classes="runbook-inline-row"):
                        yield Label("", id=f"inline-runbook-label-{index}", classes="inline-label")
                        yield Input(id=f"inline-runbook-input-{index}")
                        yield Checkbox("", id=f"inline-runbook-check-{index}")
                yield Static("", id="inline-runbook-error", classes="inline-error")
                with Horizontal(classes="inline-actions"):
                    yield Button("Cancel", id="inline-cancel-runbook")
                    yield Button("Build dry-run plan", id="inline-plan-runbook", variant="primary")
                yield Static("", id="inline-runbook-help", classes="interaction-help")
            with Vertical(id="inline-approval-view", classes="interaction-view"):
                yield Static("", id="inline-approval-title", classes="interaction-title")
                yield Static(
                    "",
                    id="inline-approval-summary",
                    classes="interaction-body",
                    markup=False,
                )
                yield OptionList(id="inline-approval-options", markup=False)
                yield Static("", id="inline-approval-help", classes="interaction-help")
            with Vertical(id="inline-language-view", classes="interaction-view"):
                yield Static("", id="inline-language-title", classes="interaction-title")
                yield Static("", id="inline-language-body", classes="interaction-body")
                yield OptionList(
                    Option("English"),
                    Option("Русский"),
                    id="inline-language-options",
                    markup=False,
                )
                yield Static("", id="inline-language-help", classes="interaction-help")
        with Vertical(id="composer-shell"):
            with Horizontal(id="composer"):
                yield Static("❯", id="prompt-mark")
                yield Input(placeholder=tr(self._language, "chat.placeholder"), id="chat-input")
            yield Static("", id="command-hints", markup=False)
            yield Static("", id="mode-line", markup=False)

    def on_mount(self) -> None:
        self._refresh_language()
        try:
            selected = self._preset_store.selected()
        except (OSError, ValueError) as error:
            self._write_system(f"Could not load model presets: {error}", error=True)
            selected = None
        if selected is not None:
            self._activate_preset(selected, self._preset_store.api_key(selected), persist=False)
        self._refresh_header()
        self._refresh_mode()
        self._write_system(tr(self._language, "chat.connected"))
        if self._preset is None:
            self._write_system(tr(self._language, "chat.no_model"))
        self.query_one("#chat-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "chat-input":
            return
        value = event.value.strip()
        event.input.value = ""
        if not value:
            return
        if value.startswith("/"):
            self._handle_command(value)
            return
        self._submit_prompt(value)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "chat-input":
            self._refresh_command_hints(event.value)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "inline-provider-kind" or not isinstance(
            event.value, ProviderKind
        ):
            return
        self.query_one("#inline-provider-url", Input).value = (
            ModelWizardScreen.DEFAULT_URLS[event.value]
        )
        self.query_one("#inline-preset-name", Input).value = {
            ProviderKind.LM_STUDIO: "local",
            ProviderKind.OPENROUTER: "openrouter",
            ProviderKind.OPENAI_COMPATIBLE: "custom",
        }[event.value]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id in {
            "inline-cancel-model",
            "inline-cancel-models",
            "inline-cancel-runbook",
        }:
            self.action_cancel_interaction()
        elif button_id == "inline-save-model":
            self._save_inline_model()
        elif button_id == "inline-use-model":
            self._use_inline_model()
        elif button_id == "inline-delete-model":
            self._delete_inline_model()
        elif button_id == "inline-plan-runbook":
            self._submit_inline_runbook()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "inline-models-list":
            self._use_inline_model(event.option_index)
        elif event.option_list.id == "inline-approval-options":
            self._choose_inline_approval(event.option_index)
        elif event.option_list.id == "inline-language-options":
            self._set_language(Language.EN if event.option_index == 0 else Language.RU)

    def action_cycle_mode(self) -> None:
        if self._interaction is not None:
            if self._interaction == "approval" and self._approval_amend is not None:
                self._choose_inline_approval(1)
            else:
                self.focus_next()
            return
        input_widget = self.query_one("#chat-input", Input)
        matches = self._matching_commands(input_widget.value)
        if matches:
            if len(matches) == 1:
                input_widget.value = matches[0][0] + " "
                input_widget.cursor_position = len(input_widget.value)
            return
        self._mode = AgentMode.READY if self._mode is AgentMode.PLAN else AgentMode.PLAN
        self._refresh_mode()

    def action_cancel_interaction(self) -> None:
        if self._interaction is None:
            return
        if self._interaction == "approval":
            callback = self._approval_callback
            self._close_inline()
            if callback is not None:
                callback(False)
            return
        self._close_inline()

    def action_clear_chat(self) -> None:
        self.query_one("#transcript", RichLog).clear()
        clear_history = getattr(self._agent, "clear_history", None)
        if callable(clear_history):
            clear_history()
        self._write_system(tr(self._language, "chat.cleared"))

    def action_tool_details(self) -> None:
        if not self._tool_trace:
            self._write_system(
                "В текущем ходе ещё нет вызовов инструментов."
                if self._language is Language.RU
                else "There are no tool calls in the current turn yet."
            )
            return
        title = "Детали инструментов" if self._language is Language.RU else "Tool details"
        self.query_one("#transcript", RichLog).write(
            Text(title + "\n  " + "\n  ".join(self._tool_trace), style="#8b949e")
        )

    def _show_inline(self, interaction: str, view_id: str, focus_id: str) -> None:
        for view in self.query(".interaction-view"):
            view.display = False
        self.query_one(view_id).display = True
        self.query_one("#composer-shell").display = False
        self.query_one("#interaction-panel").display = True
        self._interaction = interaction
        self.query_one(focus_id).focus()

    def _close_inline(self) -> None:
        self.query_one("#interaction-panel").display = False
        self.query_one("#composer-shell").display = True
        self._interaction = None
        self._approval_callback = None
        self._approval_amend = None
        input_widget = self.query_one("#chat-input", Input)
        input_widget.disabled = False
        input_widget.focus()

    def _show_model_form(self) -> None:
        self.query_one("#inline-model-error", Static).update("")
        self._show_inline("model", "#inline-model-view", "#inline-provider-kind")

    def _save_inline_model(self) -> None:
        provider = self.query_one("#inline-provider-kind", Select).value
        if not isinstance(provider, ProviderKind):
            self.query_one("#inline-model-error", Static).update("Select a provider.")
            return
        try:
            context_tokens = int(self.query_one("#inline-context-tokens", Input).value)
            preset = ProviderPreset(
                name=self.query_one("#inline-preset-name", Input).value.strip(),
                provider=provider,
                base_url=self.query_one("#inline-provider-url", Input).value.strip(),
                model=self.query_one("#inline-model-name", Input).value.strip(),
                api_key_env=self.query_one("#inline-api-key-env", Input).value.strip()
                or None,
                allow_sensitive_tool_data=self.query_one(
                    "#inline-allow-sensitive", Checkbox
                ).value,
                capabilities=ModelCapabilities(
                    supports_tools=True,
                    supports_streaming=False,
                    supports_reasoning=False,
                    supports_json_schema=False,
                    max_context_tokens=context_tokens,
                    reasoning_control=ReasoningControl.NONE,
                    tool_call_format=ToolCallFormat.OPENAI,
                ),
            )
            preset.require_agent_loop_support()
        except (TypeError, ValueError) as error:
            self.query_one("#inline-model-error", Static).update(str(error))
            return
        api_key = self.query_one("#inline-api-key", Input).value or None
        self._close_inline()
        self._activate_preset(preset, api_key)

    def _show_model_picker(self, presets: tuple[ProviderPreset, ...]) -> None:
        self._model_picker_presets = presets
        options = [
            Option(
                f"{preset.name}  ·  {preset.provider}  ·  {preset.model}\n"
                f"    {preset.base_url}"
            )
            for preset in presets
        ]
        option_list = self.query_one("#inline-models-list", OptionList)
        option_list.clear_options()
        option_list.add_options(options)
        selected_name = self._preset.name if self._preset else None
        option_list.highlighted = next(
            (
                index
                for index, preset in enumerate(presets)
                if preset.name == selected_name
            ),
            0,
        )
        self._show_inline("models", "#inline-models-view", "#inline-models-list")

    def _selected_inline_model(self, index: int | None = None) -> ProviderPreset | None:
        option_list = self.query_one("#inline-models-list", OptionList)
        selected = option_list.highlighted if index is None else index
        if selected is None or not 0 <= selected < len(self._model_picker_presets):
            return None
        return self._model_picker_presets[selected]

    def _use_inline_model(self, index: int | None = None) -> None:
        preset = self._selected_inline_model(index)
        if preset is None:
            return
        api_key = self._session_api_keys.get(preset.name) or self._preset_store.api_key(
            preset
        )
        self._close_inline()
        self._activate_preset(preset, api_key)

    def _delete_inline_model(self) -> None:
        preset = self._selected_inline_model()
        if preset is None:
            return
        self._pending_model_delete = preset
        self._show_approval(
            title=(
                "Delete saved model?"
                if self._language is Language.EN
                else "Удалить сохранённую модель?"
            ),
            summary=(
                f'Delete preset "{preset.name}" and its encrypted API key?'
                if self._language is Language.EN
                else f'Удалить preset "{preset.name}" и его зашифрованный API-ключ?'
            ),
            callback=self._model_delete_approved,
            amend=lambda: self._show_model_picker(self._model_picker_presets),
        )

    def _show_language_picker(self) -> None:
        options = self.query_one("#inline-language-options", OptionList)
        options.highlighted = 0 if self._language is Language.EN else 1
        self._show_inline(
            "language", "#inline-language-view", "#inline-language-options"
        )

    def _set_language(self, language: Language) -> None:
        self._language = language
        try:
            self._settings.save_language(language)
        except OSError as error:
            self._write_system(f"Could not save UI language: {error}", error=True)
        self._close_inline()
        self._refresh_language()
        self._refresh_header()
        self._refresh_mode()
        self._write_system(tr(self._language, "language.changed"))

    def _show_approval(
        self,
        *,
        title: str,
        summary: str,
        callback: Callable[[bool | None], None],
        amend: Callable[[], None],
    ) -> None:
        self._approval_callback = callback
        self._approval_amend = amend
        self.query_one("#inline-approval-title", Static).update(title)
        self.query_one("#inline-approval-summary", Static).update(summary)
        options = self.query_one("#inline-approval-options", OptionList)
        options.clear_options()
        options.add_options(
            (
                Option(tr(self._language, "approval.yes")),
                Option(tr(self._language, "approval.amend")),
                Option(tr(self._language, "approval.no")),
            )
        )
        options.highlighted = 0
        self._show_inline(
            "approval", "#inline-approval-view", "#inline-approval-options"
        )

    def _choose_inline_approval(self, index: int) -> None:
        callback = self._approval_callback
        amend = self._approval_amend
        if index == 1 and amend is not None:
            self._close_inline()
            amend()
            return
        approved = index == 0
        self._close_inline()
        if callback is not None:
            callback(approved)

    def _show_runbook_form(
        self,
        definition: RunbookDefinition,
        proposal: RunbookProposal | None = None,
        draft: RunbookSubmission | None = None,
    ) -> None:
        if len(definition.fields) > self.MAX_RUNBOOK_FIELDS:
            self._write_system("Runbook form has too many fields.", error=True)
            return
        self._runbook_form_definition = definition
        self._runbook_draft = draft
        self.query_one("#inline-runbook-title", Static).update(
            f"{definition.title} runbook"
        )
        self.query_one("#inline-runbook-body", Static).update(
            "Модель предложила редактируемые значения. Изменений ещё нет."
            if self._language is Language.RU and proposal is not None
            else "Проверьте все значения перед созданием dry-run плана."
            if self._language is Language.RU
            else "The model proposed editable values; no change has been made."
            if proposal is not None
            else "Review every value before building the live dry-run plan."
        )
        for index in range(self.MAX_RUNBOOK_FIELDS):
            row = self.query_one(f"#inline-runbook-row-{index}")
            row.display = index < len(definition.fields)
            if index >= len(definition.fields):
                continue
            spec = definition.fields[index]
            initial: object = spec.default
            if proposal is not None and spec.name in proposal.parameters:
                initial = proposal.parameters[spec.name]
            if draft is not None:
                initial = draft.secrets.get(spec.name, draft.values.get(spec.name, initial))
            label = (
                self.RUNBOOK_LABELS_RU.get(spec.label, spec.label)
                if self._language is Language.RU
                else spec.label
            )
            self.query_one(f"#inline-runbook-label-{index}", Label).update(label)
            input_widget = self.query_one(f"#inline-runbook-input-{index}", Input)
            checkbox = self.query_one(f"#inline-runbook-check-{index}", Checkbox)
            is_boolean = spec.kind is RunbookFieldKind.BOOLEAN
            input_widget.display = not is_boolean
            checkbox.display = is_boolean
            if is_boolean:
                checkbox.label = spec.description or label
                checkbox.value = initial if isinstance(initial, bool) else False
            else:
                input_widget.password = spec.kind is RunbookFieldKind.SECRET
                input_widget.placeholder = spec.placeholder
                input_widget.value = (
                    ", ".join(str(item) for item in initial)
                    if isinstance(initial, (list, tuple))
                    else initial
                    if isinstance(initial, str)
                    else ""
                )
        self.query_one("#inline-runbook-error", Static).update("")
        focus = (
            "#inline-runbook-check-0"
            if definition.fields[0].kind is RunbookFieldKind.BOOLEAN
            else "#inline-runbook-input-0"
        )
        self._show_inline("runbook", "#inline-runbook-view", focus)

    def _submit_inline_runbook(self) -> None:
        definition = self._runbook_form_definition
        if definition is None:
            return
        raw: dict[str, object] = {}
        for index, spec in enumerate(definition.fields):
            if spec.kind is RunbookFieldKind.BOOLEAN:
                raw[spec.name] = self.query_one(
                    f"#inline-runbook-check-{index}", Checkbox
                ).value
            else:
                raw[spec.name] = self.query_one(
                    f"#inline-runbook-input-{index}", Input
                ).value
        try:
            submission = definition.parse_submission(raw)
        except ValueError as error:
            self.query_one("#inline-runbook-error", Static).update(str(error))
            return
        self._runbook_draft = submission
        self._close_inline()
        self._plan_runbook(RunbookSelection(submission))

    def _handle_command(self, raw: str) -> None:
        command, _, argument = raw.partition(" ")
        command = command.lower()
        if command == "/help":
            self._write_system(
                "/help  /info  /model  /models [name]  /language [en|ru]  "
                "/pppoe  /bridge  /dhcp  /dns  "
                "/nat  /services  /wireguard  /rollback [execution|journal]  "
                "/log  /clear  /exit\n"
                + (
                    "Tab переключает PLAN и READY. Изменения доступны только через "
                    "подтверждённые runbook'и."
                    if self._language is Language.RU
                    else "Tab cycles PLAN and READY. Writes are available only through "
                    "approved runbooks."
                )
            )
        elif command == "/info":
            self._write_system(self._info_text())
        elif command == "/model":
            self._show_model_form()
        elif command == "/models":
            self._show_models(argument.strip())
        elif command == "/language":
            requested_language = argument.strip().casefold()
            if requested_language in {Language.EN, Language.RU}:
                self._set_language(Language(requested_language))
            elif requested_language:
                self._write_system("Use /language en or /language ru.", error=True)
            else:
                self._show_language_picker()
        elif definition := self._runbooks.for_command(command):
            if self._mode is not AgentMode.READY:
                self._write_system(
                    f"{definition.title} is an approved runbook. "
                    "Press Tab to enter READY first.",
                    error=True,
                )
            else:
                self._open_runbook(definition)
        elif command == "/rollback":
            self._start_rollback(argument.strip())
        elif command == "/log":
            self._write_system(
                "Session transcript is already displayed above; backend audit is local."
            )
        elif command == "/clear":
            self.action_clear_chat()
        elif command == "/exit":
            self.app.pop_screen()
        else:
            self._write_system(f"Unknown command: {command}. Use /help.", error=True)

    def _show_models(self, requested: str) -> None:
        try:
            presets = self._preset_store.list()
        except (OSError, ValueError) as error:
            self._write_system(f"Could not read presets: {error}", error=True)
            return
        if requested:
            selected = next((preset for preset in presets if preset.name == requested), None)
            if selected is None:
                self._write_system(f"Preset not found: {requested}", error=True)
                return
            self._activate_preset(selected, self._preset_store.api_key(selected))
            return
        if not presets:
            self._write_system("No saved presets. Use /model.")
            return
        self._show_model_picker(presets)

    def _model_selected(self, selection: ModelSelection | None) -> None:
        if selection is None:
            return
        self._activate_preset(selection.preset, selection.api_key)

    def _saved_model_selected(self, result: ModelPickerResult | None) -> None:
        if result is None:
            return
        preset = result.preset
        if result.delete:
            self._pending_model_delete = preset
            self._show_approval(
                title="Delete saved model",
                summary=(
                    f'Delete saved model preset "{preset.name}"?\n\n'
                    "Its encrypted API key will also be permanently removed."
                ),
                callback=self._model_delete_approved,
                amend=lambda: self._show_model_picker(self._model_picker_presets),
            )
            return
        api_key = self._session_api_keys.get(preset.name) or self._preset_store.api_key(preset)
        self._activate_preset(preset, api_key)

    def _model_delete_approved(self, approved: bool | None) -> None:
        preset = self._pending_model_delete
        self._pending_model_delete = None
        if preset is None or not approved:
            return
        try:
            self._preset_store.delete(preset.name)
        except (KeyError, OSError, ValueError) as error:
            self._write_system(f"Could not delete model preset: {error}", error=True)
            return
        self._session_api_keys.pop(preset.name, None)
        if self._preset is not None and self._preset.name == preset.name:
            self._preset = None
            self._agent = None
            self._refresh_header()
        self._write_system(
            f'Model preset "{preset.name}" and its saved API key were deleted.'
        )

    def _activate_preset(
        self,
        preset: ProviderPreset,
        api_key: str | None,
        *,
        persist: bool = True,
    ) -> None:
        try:
            agent = self._agent_factory(preset, api_key)
            if persist:
                self._preset_store.save(preset, api_key=api_key)
        except (OSError, ValueError) as error:
            self._write_system(f"Model preset failed: {error}", error=True)
            return
        if api_key:
            self._session_api_keys[preset.name] = api_key
        self._preset = preset
        self._agent = agent
        set_progress_sink = getattr(agent, "set_progress_sink", None)
        if callable(set_progress_sink):
            set_progress_sink(self._render_event)
        self._refresh_header()
        self._write_system(f"Model selected: {preset.model} via {preset.provider}.")
        if preset.allow_sensitive_tool_data:
            self._write_system(
                "Local privacy override active: sensitive MCP fields are visible to this "
                "loopback LLM.",
            )
        warm_up = getattr(agent, "warm_up", None)
        if callable(warm_up):
            self._warm_up_model()

    def _default_agent_factory(
        self,
        preset: ProviderPreset,
        api_key: str | None,
    ) -> AgentRunner:
        resolved_key = api_key
        if resolved_key is None and preset.api_key_env:
            resolved_key = os.environ.get(preset.api_key_env)
        provider = OpenAICompatibleClient(
            base_url=preset.base_url,
            model=preset.model,
            api_key=resolved_key,
        )
        backend = MikroMcpClient(environment=MikroMcpConfigStore().runtime_environment())
        return ReadOnlyAgentLoop(
            preset=preset,
            provider=provider,
            backend=backend,
            router_id=self.profile.router_id,
            runbooks=self._runbooks,
        )

    def _default_runbook_factory(self, definition: RunbookDefinition) -> RunbookRunner:
        store = MikroMcpConfigStore()
        backend = MikroMcpClient(environment=store.runtime_environment())
        return RunbookExecutor(backend, self.profile.router_id, definition)

    @work(exclusive=True, group="warmup", exit_on_error=False)
    async def _warm_up_model(self) -> None:
        agent = self._agent
        if agent is None:
            return
        warm_up = getattr(agent, "warm_up", None)
        if not callable(warm_up):
            return
        self._write_system("Warming up the selected model…")
        try:
            result = await warm_up()
        except ProviderError as error:
            self._write_system(f"Warm-up failed — {error.code}: {error}", error=True)
        except Exception as error:
            self._write_system(f"Warm-up failed: {error}", error=True)
        else:
            if isinstance(result, ProviderWarmup):
                self._write_system(f"Model ready · warm-up {result.latency_ms} ms.")
            else:
                self._write_system("Model ready.")

    def _open_runbook(
        self,
        definition: RunbookDefinition,
        proposal: RunbookProposal | None = None,
    ) -> None:
        self._show_runbook_form(definition, proposal)

    def _runbook_selected(self, selection: RunbookSelection | None) -> None:
        if selection is None:
            return
        self._plan_runbook(selection)

    @work(exclusive=True, group="runbook", exit_on_error=False)
    async def _plan_runbook(self, selection: RunbookSelection) -> None:
        definition = self._runbooks.get(selection.submission.runbook_id)
        self.query_one("#chat-input", Input).disabled = True
        self.query_one("#mode-line", Static).update(
            f"◌ Building {definition.title} dry-run plan…"
        )
        try:
            runner = self._runbook_factory(definition)
            plan = await runner.plan(selection.submission)
        except RunbookError as error:
            self._write_system(f"{error.code}: {error}", error=True)
        except Exception as error:
            self._write_system(f"{definition.title} planning failed: {error}", error=True)
        else:
            self._pending_runbook = (runner, plan, selection.submission.secrets)
            self._runbook_draft = selection.submission
            self._write_system(f"{definition.title} dry-run complete:\n{plan.preview}")
            self._show_approval(
                title=tr(self._language, "approval.title"),
                summary=(
                    f"Plan ID: {plan.plan_id}\n\n{plan.summary}\n\n"
                    "Every step is snapshotted and written to the audit journal before "
                    "the approved change."
                ),
                callback=self._runbook_approved,
                amend=self._amend_pending_runbook,
            )
        finally:
            input_widget = self.query_one("#chat-input", Input)
            input_widget.disabled = False
            self._refresh_mode()

    def _amend_pending_runbook(self) -> None:
        pending = self._pending_runbook
        draft = self._runbook_draft
        self._pending_runbook = None
        if pending is None:
            return
        self._show_runbook_form(pending[0].definition, draft=draft)

    def _runbook_approved(self, approved: bool | None) -> None:
        pending = self._pending_runbook
        if pending is None:
            return
        if not approved:
            self._pending_runbook = None
            self._write_system("Runbook plan cancelled; no changes were made.")
            return
        self._apply_runbook(*pending)

    @work(exclusive=True, group="runbook", exit_on_error=False)
    async def _apply_runbook(
        self,
        runner: RunbookRunner,
        plan: RunbookPlan,
        secrets: dict[str, str],
    ) -> None:
        self.query_one("#chat-input", Input).disabled = True
        self.query_one("#mode-line", Static).update(
            f"⏺ Applying approved {plan.title} plan…"
        )
        try:
            result = await runner.apply_approved(plan, secrets)
        except RunbookError as error:
            suffix = ""
            if error.journal_ids:
                try:
                    self._history.record(
                        plan,
                        self.profile.router_id,
                        error.journal_ids,
                        status="partial_failure",
                    )
                    suffix = (
                        f"\nPartial apply journals were preserved. Use /rollback {plan.plan_id}."
                    )
                except (OSError, ValueError) as history_error:
                    suffix = f"\nCould not persist partial journals: {history_error}"
            self._write_system(f"{error.code}: {error}{suffix}", error=True)
        except Exception as error:
            self._write_system(f"{plan.title} apply failed: {error}", error=True)
        else:
            journals = ", ".join(result.journal_ids) or "not returned"
            status = "applied_verified" if result.verified else "applied_unverified"
            try:
                self._history.record(
                    plan,
                    self.profile.router_id,
                    result.journal_ids,
                    status=status,
                )
            except (OSError, ValueError) as error:
                self._write_system(f"Could not persist runbook history: {error}", error=True)
            outcome = "applied and verified" if result.verified else "applied but unverified"
            if result.operational is True:
                outcome += "; operational state active"
            elif result.operational is False:
                outcome += "; operational state inactive"
            message = f"{plan.title} {outcome}.\n{result.verification.details}\n"
            message += f"Rollback journal: {journals}\n"
            if result.journal_ids:
                message += f"Use /rollback {plan.plan_id} to undo the complete runbook."
            self._write_system(message, error=not result.verified)
        finally:
            self._pending_runbook = None
            input_widget = self.query_one("#chat-input", Input)
            input_widget.disabled = False
            input_widget.focus()
            self._refresh_mode()

    def _start_rollback(self, token: str) -> None:
        if self._mode is not AgentMode.READY:
            self._write_system("Press Tab to enter READY before rollback.", error=True)
            return
        try:
            record = self._history.find(token, self.profile.router_id)
        except (OSError, ValueError) as error:
            self._write_system(f"Could not read runbook history: {error}", error=True)
            return
        if record is None or record.status == "rolled_back":
            self._write_system(
                "No active runbook execution matches that execution or journal ID.",
                error=True,
            )
            return
        try:
            definition = self._runbooks.get(record.plan.runbook_id)
        except KeyError:
            self._write_system(
                f"Runbook definition is unavailable: {record.plan.runbook_id}", error=True
            )
            return
        self._preview_rollback(self._runbook_factory(definition), record)

    @work(exclusive=True, group="runbook", exit_on_error=False)
    async def _preview_rollback(
        self,
        runner: RunbookRunner,
        record: RunbookExecutionRecord,
    ) -> None:
        self.query_one("#mode-line", Static).update("◌ Building rollback preview…")
        try:
            preview = await runner.preview_rollback(record.journal_ids)
        except RunbookError as error:
            self._write_system(f"{error.code}: {error}", error=True)
        except Exception as error:
            self._write_system(f"Rollback preview failed: {error}", error=True)
        else:
            self._pending_rollback = (runner, record)
            self._show_approval(
                title=(
                    "Rollback this RouterOS change?"
                    if self._language is Language.EN
                    else "Откатить это изменение RouterOS?"
                ),
                summary=(
                    f"Runbook: {record.plan.title}\n"
                    f"Execution: {record.execution_id}\n"
                    f"Journals: {', '.join(record.journal_ids)}\n\n{preview.preview}\n\n"
                    f"{runner.definition.rollback_note}"
                ),
                callback=self._rollback_approved,
                amend=lambda: self._start_rollback(record.execution_id),
            )
        finally:
            self._refresh_mode()

    def _rollback_approved(self, approved: bool | None) -> None:
        pending = self._pending_rollback
        if pending is None:
            return
        if not approved:
            self._pending_rollback = None
            self._write_system("Rollback cancelled; the applied change was kept.")
            return
        self._apply_rollback(*pending)

    @work(exclusive=True, group="runbook", exit_on_error=False)
    async def _apply_rollback(
        self,
        runner: RunbookRunner,
        record: RunbookExecutionRecord,
    ) -> None:
        self.query_one("#mode-line", Static).update("⏺ Applying approved rollback…")
        try:
            result = await runner.rollback_approved(record.plan, record.journal_ids)
        except RunbookError as error:
            self._write_system(f"{error.code}: {error}", error=True)
        except Exception as error:
            self._write_system(f"Rollback failed: {error}", error=True)
        else:
            try:
                self._history.mark_rolled_back(record.execution_id)
            except (OSError, ValueError) as error:
                self._write_system(f"Could not update runbook history: {error}", error=True)
            self._write_system(f"Rollback applied and verified.\n{result.verification_details}")
        finally:
            self._pending_rollback = None
            self._refresh_mode()

    def _submit_prompt(self, prompt: str) -> None:
        self._tool_trace.clear()
        self.query_one("#transcript", RichLog).write(
            Padding(
                Text(f"❯ {prompt}", style="bold white"),
                (0, 1),
                style="on #303030",
            )
        )
        if self._agent is None:
            self._write_system("Select a model first with /model.", error=True)
            return
        self.query_one("#chat-input", Input).disabled = True
        self._start_activity()
        self._run_agent(prompt)

    @work(exclusive=True, group="agent", exit_on_error=False)
    async def _run_agent(self, prompt: str) -> None:
        assert self._agent is not None
        try:
            events = await self._agent.run(prompt, self._mode)
        except ProviderError as error:
            self._write_system(f"{error.code}: {error}", error=True)
        except Exception as error:
            self._write_system(f"Agent loop failed: {error}", error=True)
        else:
            self._stop_activity()
            proposal = next(
                (event for event in events if isinstance(event, RunbookProposal)),
                None,
            )
            streamed_progress = bool(getattr(self._agent, "streams_progress", False))
            for event in events:
                if streamed_progress and isinstance(
                    event, (PlannedAction, ToolCall, ToolResult)
                ):
                    continue
                self._render_event(event)
            if proposal is not None:
                try:
                    definition = self._runbooks.get(proposal.runbook)
                except KeyError:
                    self._write_system(
                        f"Unknown runbook proposal: {proposal.runbook}", error=True
                    )
                else:
                    self._open_runbook(definition, proposal)
        finally:
            self._stop_activity()
            input_widget = self.query_one("#chat-input", Input)
            input_widget.disabled = False
            input_widget.focus()
            self._refresh_mode()

    def _start_activity(self) -> None:
        self._activity_started = time.monotonic()
        self._activity_ticks = 0
        self._phrase_cursor = (self._phrase_cursor + 1) % len(
            THINKING_PHRASES[self._language]
        )
        self.query_one("#activity-line", Static).display = True
        self._update_activity()
        if self._activity_timer is not None:
            self._activity_timer.stop()
        self._activity_timer = self.set_interval(1.0, self._update_activity)

    def _update_activity(self) -> None:
        if self._activity_started <= 0:
            return
        elapsed = max(0, int(time.monotonic() - self._activity_started))
        self._activity_ticks += 1
        if self._activity_ticks % 7 == 0:
            self._phrase_cursor = (self._phrase_cursor + 1) % len(
                THINKING_PHRASES[self._language]
            )
        phrase = THINKING_PHRASES[self._language][self._phrase_cursor]
        token_word = tr(self._language, "reasoning.tokens")
        self.query_one("#activity-line", Static).update(
            f"✦ {phrase}  ({elapsed}s · ↓ … {token_word})"
        )

    def _stop_activity(self) -> None:
        if self._activity_started > 0:
            self._last_activity_elapsed = max(
                0, int(time.monotonic() - self._activity_started)
            )
        self._activity_started = 0
        if self._activity_timer is not None:
            self._activity_timer.stop()
            self._activity_timer = None
        self.query_one("#activity-line", Static).display = False

    def _render_event(self, event: AgentEvent) -> None:
        log = self.query_one("#transcript", RichLog)
        if isinstance(event, AgentMessage):
            log.write(Text(f"● {event.text}", style="white"))
        elif isinstance(event, ReasoningStatus):
            detail = (
                f"{event.token_count} {tr(self._language, 'reasoning.tokens')}"
                if event.token_count is not None
                else tr(self._language, "reasoning.received")
            )
            if event.recovered_final_answer:
                detail += " · " + tr(self._language, "reasoning.recovered")
            log.write(
                Text(
                    (
                        f"✦ Размышлял {self._last_activity_elapsed}с · {detail}"
                        if self._language is Language.RU
                        else f"✦ Thought for {self._last_activity_elapsed}s · {detail}"
                    ),
                    style="#ff8a73",
                )
            )
        elif isinstance(event, PlannedAction):
            log.write(Text(f"  {event.summary}", style="#b6b6b6"))
        elif isinstance(event, ToolCall):
            self._tool_trace.append(
                f"{event.tool_name} "
                + json.dumps(event.arguments, ensure_ascii=False, sort_keys=True)
            )
            log.write(Text(f"  └ {event.tool_name}", style="#8b949e"))
        elif isinstance(event, ToolResult):
            status = "error" if event.is_error else "done"
            style = "#ff6b62" if event.is_error else "#7fd88f"
            log.write(Text(f"    ↳ {status}", style=style))
        elif isinstance(event, RunbookProposal):
            try:
                title = self._runbooks.get(event.runbook).title
            except KeyError:
                title = event.runbook
            log.write(
                Text(
                    f"  ↳ {title} proposal ready · opening editable approval form",
                    style="#ffb454",
                )
            )
        elif isinstance(event, VerificationResult):
            style = "#7fd88f" if event.passed else "#ff6b62"
            log.write(Text(f"  verify: {event.check} — {event.details}", style=style))
        elif isinstance(event, FinalSummary) and event.outcome is not FinalOutcome.COMPLETED:
            log.write(Text(event.text, style="#ffb454"))

    def _refresh_header(self) -> None:
        model = self._preset.model if self._preset else (
            "не выбрана · /model"
            if self._language is Language.RU
            else "not selected · /model"
        )
        provider = str(self._preset.provider) if self._preset else "—"
        labels = (
            (
                "Модель",
                "Провайдер",
                "Устройство",
                "Адрес",
                "MAC",
                "RouterOS",
                "Router ID",
                "MCP tools",
            )
            if self._language is Language.RU
            else (
                "Model",
                "Provider",
                "Identity",
                "Address",
                "MAC",
                "RouterOS",
                "Router ID",
                "MCP tools",
            )
        )
        self.query_one("#device-info", Static).update(
            f"MikroTik Harness  v{__version__}\n"
            f"{labels[0]:<10}{model}\n"
            f"{labels[1]:<10}{provider}\n"
            f"{labels[2]:<10}{self.profile.identity}\n"
            f"{labels[3]:<10}{self.profile.address}\n"
            f"{labels[4]:<10}{self.profile.mac}\n"
            f"{labels[5]:<10}{self.profile.version}  ·  {self.profile.board}\n"
            f"{labels[6]:<10}{self.profile.router_id}\n"
            f"{labels[7]:<10}{self.profile.tool_count} live"
        )

    def _refresh_mode(self) -> None:
        label = (
            tr(self._language, "chat.plan")
            if self._mode is AgentMode.PLAN
            else tr(self._language, "chat.ready")
        )
        self.query_one("#mode-line", Static).update(
            f"▮▮ {label}  ({tr(self._language, 'chat.tab_cycle')})"
        )

    def _matching_commands(self, value: str) -> tuple[tuple[str, str], ...]:
        candidate = value.strip().lower()
        if not candidate.startswith("/") or " " in candidate:
            return ()
        language_index = 1 if self._language is Language.RU else 0
        return tuple(
            (command, self.COMMAND_DESCRIPTIONS[key][language_index])
            for command, key in self.SLASH_COMMANDS
            if command.startswith(candidate)
        )

    def _refresh_command_hints(self, value: str) -> None:
        hints = self.query_one("#command-hints", Static)
        matches = self._matching_commands(value)
        hints.display = bool(matches)
        hints.update("    ".join(f"{command} — {description}" for command, description in matches))

    def _refresh_language(self) -> None:
        self.query_one("#chat-input", Input).placeholder = tr(
            self._language, "chat.placeholder"
        )
        self.query_one("#inline-model-title", Static).update(
            tr(self._language, "model.title")
        )
        self.query_one("#inline-models-title", Static).update(
            tr(self._language, "models.title")
        )
        self.query_one("#inline-language-title", Static).update(
            tr(self._language, "language.title")
        )
        self.query_one("#inline-language-body", Static).update(
            tr(self._language, "language.body")
        )
        for selector in (
            "#inline-model-help",
            "#inline-models-help",
            "#inline-runbook-help",
            "#inline-language-help",
        ):
            self.query_one(selector, Static).update(tr(self._language, "inline.help"))
        self.query_one("#inline-approval-help", Static).update(
            tr(self._language, "inline.approval_help")
        )
        model_labels = {
            "#inline-provider-label": ("Provider", "Провайдер"),
            "#inline-preset-name-label": ("Preset name", "Имя preset"),
            "#inline-provider-url-label": ("Base URL", "Base URL"),
            "#inline-model-name-label": ("Model", "Модель"),
            "#inline-api-key-label": ("API key", "API-ключ"),
            "#inline-api-key-env-label": ("API-key env", "Env-переменная ключа"),
            "#inline-context-tokens-label": ("Max context", "Макс. контекст"),
        }
        index = 1 if self._language is Language.RU else 0
        for selector, values in model_labels.items():
            self.query_one(selector, Label).update(values[index])
        button_labels = {
            "#inline-cancel-model": ("Cancel", "Отмена"),
            "#inline-save-model": ("Use model", "Использовать модель"),
            "#inline-cancel-models": ("Cancel", "Отмена"),
            "#inline-delete-model": ("Delete selected", "Удалить выбранную"),
            "#inline-use-model": ("Use model", "Использовать модель"),
            "#inline-cancel-runbook": ("Cancel", "Отмена"),
            "#inline-plan-runbook": ("Build dry-run plan", "Создать dry-run план"),
        }
        for selector, values in button_labels.items():
            self.query_one(selector, Button).label = values[index]
        self.query_one("#inline-allow-sensitive", Checkbox).label = (
            "Показывать секреты этой LLM (только loopback)"
            if self._language is Language.RU
            else "Expose secrets to this LLM (loopback only)"
        )
        self._refresh_command_hints(self.query_one("#chat-input", Input).value)

    def _info_text(self) -> str:
        model = self._preset.model if self._preset else "not selected"
        return (
            f"mth {__version__}\n"
            f"{self.profile.identity} · {self.profile.address} · {self.profile.mac}\n"
            f"RouterOS {self.profile.version} · {self.profile.board}\n"
            f"Router ID {self.profile.router_id} · {self.profile.tool_count} live MCP tools\n"
            f"Model {model} · mode {self._mode}"
        )

    def _write_system(self, message: str, *, error: bool = False) -> None:
        style = "#ff6b62" if error else "#8b949e"
        self.query_one("#transcript", RichLog).write(Text(message, style=style))
