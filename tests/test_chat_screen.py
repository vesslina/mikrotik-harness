import asyncio

from textual.app import App
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Input, OptionList, Static

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
    RunbookProposal,
    ToolCallFormat,
)
from mth.core.registration import RegistrationResult
from mth.core.runbooks import (
    RunbookApplyResult,
    RunbookHistoryPaths,
    RunbookHistoryStore,
    RunbookPlan,
    RunbookRollbackPreview,
    RunbookRollbackResult,
    RunbookStep,
    RunbookVerification,
    WanPppoeDefinition,
)
from mth.ui.textual.chat import (
    ChatProfile,
    ChatScreen,
    PixelLogo,
)
from mth.ui.textual.i18n import Language, UiSettingsPaths, UiSettingsStore


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


class _RunbookRunner:
    def __init__(self) -> None:
        self.definition = WanPppoeDefinition()
        self.submission = None
        self.applied_password = None
        self.rollback_journals = None

    async def plan(self, submission):
        self.submission = submission
        return RunbookPlan(
            plan_id="plan-1",
            runbook_id="wan_pppoe",
            title="WAN PPPoE",
            values=submission.values,
            baseline={"record": None},
            steps=(RunbookStep("manage_pppoe_client", {"action": "add"}),),
            preview="Dry run: would create pppoe-wan",
            summary="Create PPPoE client",
        )

    async def apply_approved(self, plan, secrets=None):
        self.applied_password = secrets["password"]
        return RunbookApplyResult(
            journal_ids=("journal-1",),
            verification=RunbookVerification(True, "PPPoE client exists."),
            backend_summary="Applied.",
        )

    async def preview_rollback(self, journal_ids):
        return RunbookRollbackPreview(tuple(journal_ids), "Would remove pppoe-wan")

    async def rollback_approved(self, plan, journal_ids):
        self.rollback_journals = tuple(journal_ids)
        return RunbookRollbackResult(True, "PPPoE client is absent.", "Rolled back.")


class _ChatApp(App[None]):
    def __init__(self, screen: ChatScreen) -> None:
        super().__init__()
        self._chat_screen = screen
        self.copied_text: list[str] = []

    def copy_to_clipboard(self, text: str) -> None:
        self.copied_text.append(text)

    async def on_mount(self) -> None:
        await self.push_screen(self._chat_screen)


def _screen(*args, **kwargs) -> ChatScreen:
    return ChatScreen(*args, language=Language.EN, **kwargs)


def test_chat_header_mode_cycle_and_prompt(tmp_path) -> None:
    async def scenario() -> None:
        store = ProviderPresetStore(PresetPaths(file=tmp_path / "providers.json"))
        store.save(_preset())
        runner = _Runner()
        screen = _screen(
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
        store = ProviderPresetStore(PresetPaths(file=preset_path))
        runner = _Runner()
        screen = _screen(
            _profile(),
            _registration(),
            preset_store=store,
            agent_factory=lambda _preset, _key: runner,
        )
        app = _ChatApp(screen)

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = screen.query_one("#chat-input", Input)
            prompt.value = "/model"
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, ChatScreen)
            assert screen.query_one("#interaction-panel").display is True
            assert screen.query_one("#composer-shell").display is False
            screen.query_one("#inline-model-name", Input).value = "vendor/model"
            screen.query_one("#inline-api-key", Input).value = "saved-api-secret"
            screen.query_one("#inline-save-model").focus()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, ChatScreen)
            assert "vendor/model" in str(screen.query_one("#device-info", Static).content)
            assert "saved-api-secret" not in preset_path.read_text(encoding="utf-8")
            assert "saved-api-secret" not in preset_path.with_name(
                "provider-secrets.json"
            ).read_text(encoding="utf-8")
            assert store.api_key(store.selected()) == "saved-api-secret"

    asyncio.run(scenario())


def test_models_picker_can_delete_preset_and_encrypted_key(tmp_path) -> None:
    async def scenario() -> None:
        store = ProviderPresetStore(PresetPaths(file=tmp_path / "providers.json"))
        preset = _preset()
        store.save(preset, api_key="delete-me-secret")
        screen = _screen(
            _profile(),
            _registration(),
            preset_store=store,
            agent_factory=lambda _preset, _key: _Runner(),
        )
        app = _ChatApp(screen)

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = screen.query_one("#chat-input", Input)
            prompt.value = "/models"
            await pilot.press("enter")
            await pilot.pause()

            assert screen.query_one("#inline-models-view").display is True
            await pilot.click("#inline-delete-model")
            await pilot.pause()
            assert screen.query_one("#inline-approval-view").display is True
            assert "Delete saved model" in str(
                screen.query_one("#inline-approval-title", Static).content
            )
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, ChatScreen)
            assert store.list() == ()
            assert store.api_key(preset) is None
            assert "not selected" in str(
                screen.query_one("#device-info", Static).content
            )

    asyncio.run(scenario())


def test_pixel_logo_has_two_five_row_words() -> None:
    rendered = PixelLogo().render()

    assert rendered.plain.count("\n") == 13
    assert "\n\n" in rendered.plain
    assert "█" in rendered.plain
    assert "▓" in rendered.plain
    assert "▓" in rendered.plain.splitlines()[-1]
    assert max(len(line) for line in rendered.plain.splitlines()) <= 48
    assert any("ff3b30" in str(span.style) for span in rendered.spans)
    assert any("681d1d" in str(span.style) for span in rendered.spans)


def test_transcript_is_selectable_and_can_be_copied(tmp_path) -> None:
    async def scenario() -> None:
        screen = _screen(
            _profile(),
            _registration(),
            preset_store=ProviderPresetStore(
                PresetPaths(file=tmp_path / "providers.json")
            ),
        )
        app = _ChatApp(screen)

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen._write_system("Selectable transcript line")
            await pilot.pause()

            transcript = screen.query_one("#transcript")
            selected = transcript.get_selection(
                Selection.from_offsets(Offset(0, 0), Offset(9, 0))
            )
            assert selected is not None
            assert selected[0]
            screen.action_copy_transcript()
            assert app.copied_text
            assert "Selectable transcript line" in app.copied_text[-1]

    asyncio.run(scenario())


def test_transcript_double_click_target_selects_one_token(tmp_path) -> None:
    async def scenario() -> None:
        screen = _screen(
            _profile(),
            _registration(),
            preset_store=ProviderPresetStore(
                PresetPaths(file=tmp_path / "providers.json")
            ),
        )
        app = _ChatApp(screen)

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen._write_system("Address 192.168.1.33/24 on bridge-lan")
            await pilot.pause()
            transcript = screen.query_one("#transcript")
            line_index = next(
                index
                for index, line in enumerate(transcript.lines)
                if "192.168.1.33/24" in line.text
            )
            line = transcript.lines[line_index].text
            selection = transcript.word_selection_at(
                Offset(line.index("192.168.1.33/24") + 3, line_index)
            )

            assert selection is not None
            assert selection.extract(transcript.plain_text()) == "192.168.1.33/24"

    asyncio.run(scenario())


def test_slash_command_hints_filter_and_tab_completes(tmp_path) -> None:
    async def scenario() -> None:
        screen = _screen(
            _profile(),
            _registration(),
            preset_store=ProviderPresetStore(PresetPaths(file=tmp_path / "providers.json")),
            agent_factory=lambda _preset, _key: _Runner(),
        )
        app = _ChatApp(screen)

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = screen.query_one("#chat-input", Input)
            prompt.value = "/mod"
            await pilot.pause()

            hints = screen.query_one("#command-hints", Static)
            assert hints.display is True
            assert "/model" in str(hints.content)
            assert "/models" in str(hints.content)
            assert "/help" not in str(hints.content)

            prompt.value = "/cle"
            await pilot.press("tab")
            assert prompt.value == "/clear "

    asyncio.run(scenario())


def test_models_command_opens_picker_and_activates_saved_model(tmp_path) -> None:
    async def scenario() -> None:
        store = ProviderPresetStore(PresetPaths(file=tmp_path / "providers.json"))
        first = _preset()
        second = ProviderPreset(
            name="second",
            provider=ProviderKind.OPENAI_COMPATIBLE,
            base_url="http://localhost:20128/v1",
            model="oc/deepseek-v4-flash-free",
            capabilities=first.capabilities,
        )
        store.save(first)
        store.save(second, select=False)
        screen = _screen(
            _profile(),
            _registration(),
            preset_store=store,
            agent_factory=lambda _preset, _key: _Runner(),
        )
        app = _ChatApp(screen)

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = screen.query_one("#chat-input", Input)
            prompt.value = "/models"
            await pilot.press("enter")
            await pilot.pause()

            option_list = screen.query_one("#inline-models-list", OptionList)
            option_list.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, ChatScreen)
            assert "oc/deepseek-v4-flash-free" in str(
                screen.query_one("#device-info", Static).content
            )
            assert store.selected() == second

    asyncio.run(scenario())


def test_pppoe_command_requires_ready_then_approves_applies_and_rolls_back(tmp_path) -> None:
    async def scenario() -> None:
        class ReportingAgent(_Runner):
            def __init__(self) -> None:
                super().__init__()
                self.reports = 0

            async def report_change(self, plan, result):
                assert plan.title == "WAN PPPoE"
                assert result.verified is True
                self.reports += 1
                return (AgentMessage("PPPoE applied, verified, and ready to roll back."),)

        runner = _RunbookRunner()
        agent = ReportingAgent()
        preset_store = ProviderPresetStore(PresetPaths(file=tmp_path / "providers.json"))
        preset_store.save(_preset())
        screen = _screen(
            _profile(),
            _registration(),
            preset_store=preset_store,
            agent_factory=lambda _preset, _key: agent,
            runbook_factory=lambda _definition: runner,
            history_store=RunbookHistoryStore(
                RunbookHistoryPaths(file=tmp_path / "runbooks.json")
            ),
        )
        app = _ChatApp(screen)

        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            prompt = screen.query_one("#chat-input", Input)
            prompt.value = "/pppoe"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ChatScreen)

            await pilot.press("tab")
            prompt.value = "/pppoe"
            await pilot.press("enter")
            await pilot.pause()
            assert screen.query_one("#inline-runbook-view").display is True
            screen.query_one("#inline-runbook-input-2", Input).value = "isp-user"
            screen.query_one("#inline-runbook-input-3", Input).value = "isp-secret"
            screen.query_one("#inline-plan-runbook").focus()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert screen.query_one("#inline-approval-view").display is True
            summary = str(screen.query_one("#inline-approval-summary", Static).content)
            assert "isp-secret" not in summary
            await pilot.press("tab")
            await pilot.pause()
            assert screen.query_one("#inline-runbook-view").display is True
            assert screen.query_one("#inline-runbook-input-2", Input).value == "isp-user"
            assert screen.query_one("#inline-runbook-input-3", Input).value == "isp-secret"
            screen.query_one("#inline-plan-runbook").focus()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.query_one("#inline-approval-view").display is True
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert isinstance(app.screen, ChatScreen)
            assert runner.applied_password == "isp-secret"
            assert agent.reports == 1
            assert "PPPoE applied, verified" in screen.query_one(
                "#transcript"
            ).plain_text()
            prompt.value = "/rollback journal-1"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.query_one("#inline-approval-view").display is True
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert isinstance(app.screen, ChatScreen)
            assert runner.rollback_journals == ("journal-1",)

    asyncio.run(scenario())


def test_agent_pppoe_proposal_opens_prefilled_masked_runbook(tmp_path) -> None:
    async def scenario() -> None:
        class ProposalRunner:
            async def run(self, prompt: str, mode: AgentMode):
                assert mode is AgentMode.READY
                return (
                    RunbookProposal(
                        "wan_pppoe",
                        {
                            "name": "isp-uplink",
                            "interface": "ether3",
                            "username": "subscriber",
                            "serviceName": "internet",
                            "addDefaultRoute": True,
                            "dialOnDemand": False,
                        },
                    ),
                    FinalSummary("Runbook proposed.", FinalOutcome.COMPLETED),
                )

        store = ProviderPresetStore(PresetPaths(file=tmp_path / "providers.json"))
        store.save(_preset())
        screen = _screen(
            _profile(),
            _registration(),
            preset_store=store,
            agent_factory=lambda _preset, _key: ProposalRunner(),
        )
        app = _ChatApp(screen)

        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            await pilot.press("tab")
            prompt = screen.query_one("#chat-input", Input)
            prompt.value = "Настрой PPPoE на ether3"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert screen.query_one("#inline-runbook-view").display is True
            assert screen.query_one("#inline-runbook-input-0", Input).value == "isp-uplink"
            assert screen.query_one("#inline-runbook-input-1", Input).value == "ether3"
            assert screen.query_one("#inline-runbook-input-2", Input).value == "subscriber"
            assert screen.query_one("#inline-runbook-input-3", Input).value == ""
            assert screen.query_one("#inline-runbook-input-3", Input).password is True

    asyncio.run(scenario())


def test_agent_typed_proposal_goes_directly_to_dry_run_approval(tmp_path) -> None:
    async def scenario() -> None:
        class ProposalAgent:
            async def run(self, prompt: str, mode: AgentMode):
                assert mode is AgentMode.READY
                return (
                    RunbookProposal(
                        "typed:manage_route",
                        {
                            "action": "add",
                            "dstAddress": "10.20.0.0/16",
                            "gateway": "192.0.2.1",
                        },
                    ),
                    FinalSummary("Typed change proposed.", FinalOutcome.COMPLETED),
                )

        class TypedRunner:
            def __init__(self, definition) -> None:
                self.definition = definition

            async def plan(self, submission):
                assert submission.runbook_id == "typed:manage_route"
                assert submission.values["arguments"]["gateway"] == "192.0.2.1"
                return RunbookPlan(
                    plan_id="typed-route-1",
                    runbook_id=submission.runbook_id,
                    title=self.definition.title,
                    values=submission.values,
                    baseline={"stateHash": "before"},
                    steps=(
                        RunbookStep(
                            "manage_route",
                            {
                                "action": "add",
                                "dstAddress": "10.20.0.0/16",
                                "gateway": "192.0.2.1",
                            },
                        ),
                    ),
                    preview="Dry run: would add route",
                    summary="Add route 10.20.0.0/16 via 192.0.2.1",
                )

        store = ProviderPresetStore(PresetPaths(file=tmp_path / "providers.json"))
        store.save(_preset())
        definitions = []

        def factory(definition):
            definitions.append(definition)
            return TypedRunner(definition)

        screen = _screen(
            _profile(),
            _registration(),
            preset_store=store,
            agent_factory=lambda _preset, _key: ProposalAgent(),
            runbook_factory=factory,
        )
        app = _ChatApp(screen)

        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            await pilot.press("tab")
            prompt = screen.query_one("#chat-input", Input)
            prompt.value = "Добавь маршрут 10.20.0.0/16 через 192.0.2.1"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert definitions[0].tool_name == "manage_route"
            assert screen.query_one("#inline-runbook-view").display is False
            assert screen.query_one("#inline-approval-view").display is True
            assert "10.20.0.0/16" in str(
                screen.query_one("#inline-approval-summary", Static).content
            )

    asyncio.run(scenario())


def test_bridge_command_uses_the_generic_schema_driven_wizard(tmp_path) -> None:
    async def scenario() -> None:
        screen = _screen(
            _profile(),
            _registration(),
            preset_store=ProviderPresetStore(
                PresetPaths(file=tmp_path / "providers.json")
            ),
        )
        app = _ChatApp(screen)

        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            await pilot.press("tab")
            prompt = screen.query_one("#chat-input", Input)
            prompt.value = "/bridge"
            await pilot.press("enter")
            await pilot.pause()

            assert screen.query_one("#inline-runbook-view").display is True
            assert screen.query_one("#inline-runbook-input-0", Input).value == "bridge-lan"
            assert screen.query_one("#inline-runbook-input-1", Input).password is False
            assert screen.query_one("#inline-runbook-input-2", Input).value == "Managed by mth"

    asyncio.run(scenario())


def test_language_command_switches_inline_and_persists(tmp_path) -> None:
    async def scenario() -> None:
        settings = UiSettingsStore(UiSettingsPaths(file=tmp_path / "settings.json"))
        screen = ChatScreen(
            _profile(),
            _registration(),
            preset_store=ProviderPresetStore(
                PresetPaths(file=tmp_path / "providers.json")
            ),
            agent_factory=lambda _preset, _key: _Runner(),
            settings_store=settings,
            language=Language.EN,
        )
        app = _ChatApp(screen)

        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            prompt = screen.query_one("#chat-input", Input)
            prompt.value = "/language"
            await pilot.press("enter")
            await pilot.pause()

            assert screen.query_one("#inline-language-view").display is True
            choices = screen.query_one("#inline-language-options", OptionList)
            choices.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()

            assert settings.language() is Language.RU
            assert "Спросите" in str(prompt.placeholder)
            assert screen.query_one("#composer-shell").display is True

    asyncio.run(scenario())
