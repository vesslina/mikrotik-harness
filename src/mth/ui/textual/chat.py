from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
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
    def _word(cls, value: str) -> tuple[str, ...]:
        return tuple(
            " ".join(
                "".join("█" if pixel == "1" else " " for pixel in cls.GLYPHS[letter][row])
                for letter in value
            ).rstrip()
            for row in range(5)
        )

    def render(self) -> Text:
        output = Text()
        for line in self._word("MIKROTIK"):
            output.append(line + "\n", style="bold white on #090909")
        output.append("\n", style="on #090909")
        for index, line in enumerate(self._word("HARNESS")):
            suffix = "\n" if index < 4 else ""
            output.append(line + suffix, style="bold #ff3b30 on #090909")
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
                yield Label("API key (memory only)", classes="field-label")
                yield Input(
                    password=True,
                    placeholder="optional for local providers",
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


class ModelPickerScreen(ModalScreen[ProviderPreset | None]):
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
        self.dismiss(self._presets[event.option_index])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-picker":
            self.dismiss(None)
        elif event.button.id == "use-saved-model":
            option_list = self.query_one("#saved-model-list", OptionList)
            index = option_list.highlighted
            if index is not None:
                self.dismiss(self._presets[index])

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

    def __init__(self, summary: str, *, action_label: str = "Apply approved plan") -> None:
        super().__init__()
        self._summary = summary
        self._action_label = action_label

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-dialog"):
            yield Static("Approve RouterOS change", id="approval-title")
            yield Static(self._summary, id="approval-summary", markup=False)
            with Horizontal(id="approval-actions"):
                yield Button("Cancel", id="cancel-approval")
                yield Button(self._action_label, id="approve-plan", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "approve-plan")


class ChatScreen(Screen[None]):
    SLASH_COMMANDS = (
        ("/help", "show commands"),
        ("/info", "router and session info"),
        ("/model", "add or edit a model"),
        ("/models", "choose a saved model"),
        ("/pppoe", "configure WAN PPPoE safely"),
        ("/bridge", "create a LAN bridge safely"),
        ("/nat", "configure WAN masquerade safely"),
        ("/services", "disable unnecessary services safely"),
        ("/rollback", "rollback a runbook journal"),
        ("/log", "session log info"),
        ("/clear", "clear transcript"),
        ("/exit", "return to discovery"),
    )

    BINDINGS = [
        Binding("tab", "cycle_mode", "Cycle mode", show=False, priority=True),
        Binding("ctrl+l", "clear_chat", "Clear", show=False),
    ]

    CSS = """
    ChatScreen { background: #090909; color: #e7e7e7; }
    #chat-header {
        height: 14;
        padding: 1 2;
        background: #090909;
        border-bottom: solid #5b6268;
    }
    #brand { width: 46; min-width: 46; height: 12; background: #090909; }
    #device-info { width: 1fr; height: 12; padding-left: 2; color: #9aa2aa; }
    #transcript { height: 1fr; padding: 1 3; background: #090909; scrollbar-color: #5b6268; }
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
    ) -> None:
        super().__init__()
        self.profile = profile
        self.registration = registration
        self._preset_store = preset_store or ProviderPresetStore()
        self._agent_factory = agent_factory or self._default_agent_factory
        self._runbooks = runbook_registry
        self._runbook_factory = runbook_factory or self._default_runbook_factory
        self._history = history_store or RunbookHistoryStore()
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

    def compose(self) -> ComposeResult:
        with Horizontal(id="chat-header"):
            yield PixelLogo(id="brand")
            yield Static("", id="device-info", markup=False)
        yield RichLog(id="transcript", wrap=True, markup=False, auto_scroll=True)
        with Horizontal(id="composer"):
            yield Static("❯", id="prompt-mark")
            yield Input(
                placeholder="Ask about this MikroTik, or type /help",
                id="chat-input",
            )
        yield Static("", id="command-hints", markup=False)
        yield Static("", id="mode-line", markup=False)

    def on_mount(self) -> None:
        try:
            selected = self._preset_store.selected()
        except (OSError, ValueError) as error:
            self._write_system(f"Could not load model presets: {error}", error=True)
            selected = None
        if selected is not None:
            self._activate_preset(selected, self._preset_store.api_key(selected), persist=False)
        self._refresh_header()
        self._refresh_mode()
        self._write_system(
            "Connected through MikroMCP. PLAN mode is active; press Tab for read-only READY."
        )
        if self._preset is None:
            self._write_system("No model selected. Use /model to configure one.")
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

    def action_cycle_mode(self) -> None:
        input_widget = self.query_one("#chat-input", Input)
        matches = self._matching_commands(input_widget.value)
        if matches:
            if len(matches) == 1:
                input_widget.value = matches[0][0] + " "
                input_widget.cursor_position = len(input_widget.value)
            return
        self._mode = AgentMode.READY if self._mode is AgentMode.PLAN else AgentMode.PLAN
        self._refresh_mode()

    def action_clear_chat(self) -> None:
        self.query_one("#transcript", RichLog).clear()

    def _handle_command(self, raw: str) -> None:
        command, _, argument = raw.partition(" ")
        command = command.lower()
        if command == "/help":
            self._write_system(
                "/help  /info  /model  /models [name]  /pppoe  /bridge  /nat  "
                "/services  /rollback [execution|journal]  /log  /clear  /exit\n"
                "Tab cycles PLAN and READY. Writes are available only through approved runbooks."
            )
        elif command == "/info":
            self._write_system(self._info_text())
        elif command == "/model":
            self.app.push_screen(ModelWizardScreen(), self._model_selected)
        elif command == "/models":
            self._show_models(argument.strip())
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
        selected_name = self._preset.name if self._preset else None
        self.app.push_screen(
            ModelPickerScreen(presets, selected_name),
            self._saved_model_selected,
        )

    def _model_selected(self, selection: ModelSelection | None) -> None:
        if selection is None:
            return
        self._activate_preset(selection.preset, selection.api_key)

    def _saved_model_selected(self, preset: ProviderPreset | None) -> None:
        if preset is None:
            return
        api_key = self._session_api_keys.get(preset.name) or self._preset_store.api_key(preset)
        self._activate_preset(preset, api_key)

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
                self._preset_store.save(preset)
        except (OSError, ValueError) as error:
            self._write_system(f"Model preset failed: {error}", error=True)
            return
        if api_key:
            self._session_api_keys[preset.name] = api_key
        self._preset = preset
        self._agent = agent
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
        self.app.push_screen(
            RunbookWizardScreen(definition, proposal),
            self._runbook_selected,
        )

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
            self._write_system(f"{definition.title} dry-run complete:\n{plan.preview}")
            self.app.push_screen(
                ApprovalScreen(
                    f"Plan ID: {plan.plan_id}\n\n{plan.summary}\n\n"
                    "Every step is snapshotted and written to the audit journal before "
                    "the approved change."
                ),
                self._runbook_approved,
            )
        finally:
            input_widget = self.query_one("#chat-input", Input)
            input_widget.disabled = False
            self._refresh_mode()

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
            self.app.push_screen(
                ApprovalScreen(
                    f"Runbook: {record.plan.title}\n"
                    f"Execution: {record.execution_id}\n"
                    f"Journals: {', '.join(record.journal_ids)}\n\n{preview.preview}\n\n"
                    f"{runner.definition.rollback_note}",
                    action_label="Rollback this change",
                ),
                self._rollback_approved,
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
        self.query_one("#transcript", RichLog).write(Text(f"❯ {prompt}", style="bold white"))
        if self._agent is None:
            self._write_system("Select a model first with /model.", error=True)
            return
        self.query_one("#chat-input", Input).disabled = True
        model = self._preset.model if self._preset else "model"
        self.query_one("#mode-line", Static).update(f"✦ {model} is thinking…")
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
            proposal = next(
                (event for event in events if isinstance(event, RunbookProposal)),
                None,
            )
            for event in events:
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
            input_widget = self.query_one("#chat-input", Input)
            input_widget.disabled = False
            input_widget.focus()
            self._refresh_mode()

    def _render_event(self, event: AgentEvent) -> None:
        log = self.query_one("#transcript", RichLog)
        if isinstance(event, AgentMessage):
            log.write(Text(event.text, style="white"))
        elif isinstance(event, ReasoningStatus):
            detail = (
                f"{event.token_count} reasoning tokens"
                if event.token_count is not None
                else "reasoning received"
            )
            if event.recovered_final_answer:
                detail += " · final answer recovered"
            log.write(Text(f"  ✦ {detail}", style="#b9a0ff"))
        elif isinstance(event, PlannedAction):
            log.write(Text(f"  · {event.summary}", style="#6fd3df"))
        elif isinstance(event, ToolCall):
            log.write(Text(f"  ⏺ {event.tool_name}", style="bold #6fd3df"))
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
        model = self._preset.model if self._preset else "not selected · /model"
        provider = str(self._preset.provider) if self._preset else "—"
        self.query_one("#device-info", Static).update(
            f"MikroTik Harness  v{__version__}\n"
            f"Model     {model}\n"
            f"Provider  {provider}\n"
            f"Identity  {self.profile.identity}\n"
            f"Address   {self.profile.address}\n"
            f"MAC       {self.profile.mac}\n"
            f"RouterOS  {self.profile.version}  ·  {self.profile.board}\n"
            f"Router ID {self.profile.router_id}\n"
            f"MCP tools {self.profile.tool_count} live"
        )

    def _refresh_mode(self) -> None:
        label = "PLAN" if self._mode is AgentMode.PLAN else "READY · reads + approved runbooks"
        self.query_one("#mode-line", Static).update(f"▮▮ {label}  (Tab to cycle)")

    @classmethod
    def _matching_commands(cls, value: str) -> tuple[tuple[str, str], ...]:
        candidate = value.strip().lower()
        if not candidate.startswith("/") or " " in candidate:
            return ()
        return tuple(item for item in cls.SLASH_COMMANDS if item[0].startswith(candidate))

    def _refresh_command_hints(self, value: str) -> None:
        hints = self.query_one("#command-hints", Static)
        matches = self._matching_commands(value)
        hints.display = bool(matches)
        hints.update("    ".join(f"{command} — {description}" for command, description in matches))

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
