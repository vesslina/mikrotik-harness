import json

from mth.ui.textual.i18n import (
    Language,
    UiSettingsPaths,
    UiSettingsStore,
    detect_system_language,
)


def test_language_environment_override(monkeypatch) -> None:
    monkeypatch.setenv("MTH_LANGUAGE", "ru")
    assert detect_system_language() is Language.RU

    monkeypatch.setenv("MTH_LANGUAGE", "en")
    assert detect_system_language() is Language.EN


def test_ui_settings_persist_language_atomically(tmp_path) -> None:
    settings_file = tmp_path / "settings.json"
    store = UiSettingsStore(UiSettingsPaths(file=settings_file))

    store.save_language(Language.RU)

    assert store.language() is Language.RU
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {
        "version": 1,
        "language": "ru",
    }
