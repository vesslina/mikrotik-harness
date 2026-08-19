import asyncio

from textual.app import App
from textual.widgets import Input, Static

from mth.agent import (
    AgentMessage,
    AgentMode,
    FinalOutcome,
    FinalSummary,
    ModelCapabilities,
    PresetPaths,
    ProviderKind,
    ProviderPreset,
    ProviderPresetStore,
    ReasoningControl,
    ToolCallFormat,
)
from mth.core.registration import RegistrationResult
from mth.ui.textual.chat import (
    ChatProfile,
    ChatScreen,
    ModelWizardScreen,
    PixelLogo,
)


def _preset() -> ProviderPreset:
    return ProviderPreset(
        name="lab",
        provider=ProviderKind.LM_STUDIO,
        base_url="http://127.0.0.1:1234/v1",
        model="lab-model",
        capabilities=ModelCapabilities(
            supports_tools=True,
            supports_streaming=True,
            supports_reasoning=False,
            supports_json_schema=True,
            max_context_tokens=32_768,
            reasoning_control=ReasoningControl.NONE,
            tool_call_format=ToolCallFormat.OPENAI,
        ),
    )


def _profile() -> ChatProfile:
    return ChatProfile(
        router_id="mikrotik-afe23e",
        address="192.168.56.103",
        identity="MikroTik",
        version="7.21.5",
        board="CHR",
        mac="08:00:27:AF:E2:3E",
        tool_count=122,
    )


def _registration() -> RegistrationResult:
    return RegistrationResult(
        router_id="mikrotik-afe23e",
        identity="MikroTik",
        tool_count=122,
        health={"healthy": True},
        system_status={},
    )


class _Runner:
    def __init__(self) -> None:
        self.calls = []

    async def run(self, prompt: str, mode: AgentMode):
        self.calls.append((prompt, mode))
        return (
            AgentMessage("ether1 is running."),
            FinalSummary("ether1 is running.", FinalOutcome.COMPLETED),
        )


class _ChatApp(App[None]):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._chat_screen = screen

    async def on_mount(self) -> None:
        await self.push_screen(self._chat_screen)


def test_chat_header_mode_cycle_and_prompt(tmp_path) -> None:
    async def scenario() -> None:
        store = ProviderPresetStore(PresetPaths(file=tmp_path / "providers.json"))
        store.save(_preset())
        runner = _Runner()
        screen = ChatScreen(
            _profile(),
            _registration(),
            preset_store=store,
            agent_factory=lambda _preset, _key: runner,
        )
        app = _ChatApp(screen)

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            info = str(screen.query_one("#device-info", Static).content)
            assert "192.168.56.103" in info
            assert "08:00:27:AF:E2:3E" in info
            assert "lab-model" in info
            assert "122 live" in info
            assert "PLAN" in str(screen.query_one("#mode-line", Static).content)

            await pilot.press("tab")
            assert "READY" in str(screen.query_one("#mode-line", Static).content)

            prompt = screen.query_one("#chat-input", Input)
            prompt.value = "Show interfaces"
            await pilot.press("enter")
            await app.workers.wait_for_complete()

            assert runner.calls == [("Show interfaces", AgentMode.READY)]
            assert prompt.disabled is False

    asyncio.run(scenario())


def test_model_command_opens_wizard(tmp_path) -> None:
    async def scenario() -> None:
        preset_path = tmp_path / "providers.json"
        runner = _Runner()
        screen = ChatScreen(
            _profile(),
            _registration(),
            preset_store=ProviderPresetStore(PresetPaths(file=preset_path)),
            agent_factory=lambda _preset, _key: runner,
        )
        app = _ChatApp(screen)

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = screen.query_one("#chat-input", Input)
            prompt.value = "/model"
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, ModelWizardScreen)
            wizard = app.screen
            wizard.query_one("#model-name", Input).value = "vendor/model"
            wizard.query_one("#api-key", Input).value = "memory-only-secret"
            await pilot.click("#save-model")
            await pilot.pause()

            assert isinstance(app.screen, ChatScreen)
            assert "vendor/model" in str(screen.query_one("#device-info", Static).content)
            assert "memory-only-secret" not in preset_path.read_text(encoding="utf-8")

    asyncio.run(scenario())


def test_pixel_logo_has_two_five_row_words() -> None:
    rendered = PixelLogo().render()

    assert rendered.plain.count("\n") == 9
    assert "█" in rendered.plain
    assert any("ff3b30" in str(span.style) for span in rendered.spans)
