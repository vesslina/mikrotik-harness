from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, Label, RichLog, Select, Static

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
    ReadOnlyAgentLoop,
    ReasoningControl,
    ToolCall,
    ToolCallFormat,
    ToolResult,
    VerificationResult,
)
from mth.core.mcp_client import MikroMcpClient
from mth.core.registration import MikroMcpConfigStore, RegistrationResult


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


@dataclass(frozen=True, slots=True)
class ModelSelection:
    preset: ProviderPreset
    api_key: str | None = field(default=None, repr=False)


class PixelLogo(Static):
    GLYPHS = {
        "A": ("01110", "10001", "11111", "10001", "10001"),
        "E": ("11111", "10000", "11110", "10000", "11111"),
        "H": ("10001", "10001", "11111", "10001", "10001"),
        "I": ("11111", "00100", "00100", "00100", "11111"),
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
        max-height: 32;
        padding: 1 2;
        border: round #ff3b30;
        background: #111315;
    }
    #model-dialog .model-row { height: 3; }
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


class ChatScreen(Screen[None]):
    BINDINGS = [
        Binding("tab", "cycle_mode", "Cycle mode", show=False, priority=True),
        Binding("ctrl+l", "clear_chat", "Clear", show=False),
    ]

    CSS = """
    ChatScreen { background: #090909; color: #e7e7e7; }
    #chat-header {
        height: 13;
        padding: 1 2;
        background: #090909;
        border-bottom: solid #5b6268;
    }
    #brand { width: 46; height: 11; background: #090909; }
    #device-info { width: 1fr; height: 11; padding-left: 3; color: #9aa2aa; }
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
    #mode-line { height: 3; padding: 1 3; color: #6fd3df; background: #090909; }
    """

    def __init__(
        self,
        profile: ChatProfile,
        registration: RegistrationResult,
        *,
        preset_store: ProviderPresetStore | None = None,
        agent_factory: AgentFactory | None = None,
    ) -> None:
        super().__init__()
        self.profile = profile
        self.registration = registration
        self._preset_store = preset_store or ProviderPresetStore()
        self._agent_factory = agent_factory or self._default_agent_factory
        self._preset: ProviderPreset | None = None
        self._agent: AgentRunner | None = None
        self._mode = AgentMode.PLAN

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

    def action_cycle_mode(self) -> None:
        self._mode = AgentMode.READY if self._mode is AgentMode.PLAN else AgentMode.PLAN
        self._refresh_mode()

    def action_clear_chat(self) -> None:
        self.query_one("#transcript", RichLog).clear()

    def _handle_command(self, raw: str) -> None:
        command, _, argument = raw.partition(" ")
        command = command.lower()
        if command == "/help":
            self._write_system(
                "/help  /info  /model  /models [name]  /log  /clear  /exit\n"
                "Tab cycles PLAN and read-only READY mode."
            )
        elif command == "/info":
            self._write_system(self._info_text())
        elif command == "/model":
            self.app.push_screen(ModelWizardScreen(), self._model_selected)
        elif command == "/models":
            self._show_models(argument.strip())
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
        lines = [
            f"{'*' if self._preset and self._preset.name == preset.name else ' '} "
            f"{preset.name}: {preset.provider} · {preset.model} · {preset.base_url}"
            for preset in presets
        ]
        self._write_system("Saved models:\n" + "\n".join(lines))

    def _model_selected(self, selection: ModelSelection | None) -> None:
        if selection is None:
            return
        self._activate_preset(selection.preset, selection.api_key)

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
        self._preset = preset
        self._agent = agent
        self._refresh_header()
        self._write_system(f"Model selected: {preset.model} via {preset.provider}.")

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
        )

    def _submit_prompt(self, prompt: str) -> None:
        self.query_one("#transcript", RichLog).write(Text(f"❯ {prompt}", style="bold white"))
        if self._agent is None:
            self._write_system("Select a model first with /model.", error=True)
            return
        self.query_one("#chat-input", Input).disabled = True
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
            for event in events:
                self._render_event(event)
        finally:
            input_widget = self.query_one("#chat-input", Input)
            input_widget.disabled = False
            input_widget.focus()

    def _render_event(self, event: AgentEvent) -> None:
        log = self.query_one("#transcript", RichLog)
        if isinstance(event, AgentMessage):
            log.write(Text(event.text, style="white"))
        elif isinstance(event, PlannedAction):
            log.write(Text(f"  · {event.summary}", style="#6fd3df"))
        elif isinstance(event, ToolCall):
            log.write(Text(f"  ⏺ {event.tool_name}", style="bold #6fd3df"))
        elif isinstance(event, ToolResult):
            status = "error" if event.is_error else "done"
            style = "#ff6b62" if event.is_error else "#7fd88f"
            log.write(Text(f"    ↳ {status}", style=style))
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
        label = "PLAN" if self._mode is AgentMode.PLAN else "READY · read-only tools"
        self.query_one("#mode-line", Static).update(f"▮▮ {label}  (Tab to cycle)")

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
