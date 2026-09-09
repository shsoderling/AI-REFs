"""Input tab: the Claude model list is discovered, never hard-wired."""
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.models.project import ProjectSettings  # noqa: E402
from src.services.model_catalog import NEWEST_MODEL, ModelInfo, ModelSource  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def isolated_settings(monkeypatch, tmp_path):
    """QSettings(org, app) always uses the native store on macOS: point the
    tab at an ini file in tmp so tests never see (or touch) the user's profile."""
    from PySide6.QtCore import QSettings
    import src.gui.inputs_tab as it
    ini = str(tmp_path / "settings.ini")

    class IsolatedSettings(QSettings):
        def __init__(self, *args, **kwargs):
            super().__init__(ini, QSettings.Format.IniFormat)
    monkeypatch.setattr(it, "QSettings", IsolatedSettings)
    import src.services.model_catalog as mc
    monkeypatch.setattr(mc, "CACHE_PATH", tmp_path / "models_cache.json")
    return IsolatedSettings


@pytest.fixture
def tab(qapp, isolated_settings):
    from src.gui.inputs_tab import InputsTab
    t = InputsTab()
    t.set_model_list(MODELS, ModelSource.CACHE)
    return t


MODELS = [
    ModelInfo("claude-fable-5-1", "Claude Fable 5.1", "2026-06-24T00:00:00+00:00"),
    ModelInfo("claude-opus-5", "Claude Opus 5", "2026-04-01T00:00:00+00:00"),
    ModelInfo("claude-haiku-4-5", "Claude Haiku 4.5", "2025-10-01T00:00:00+00:00"),
]


def test_combo_lists_models_newest_first_with_ids(tab):
    ids = [tab.model_combo.itemData(i) for i in range(tab.model_combo.count())]
    assert ids == ["claude-fable-5-1", "claude-opus-5", "claude-haiku-4-5"]
    assert tab.model_combo.itemText(0).startswith("Claude Fable 5.1")
    assert "claude-fable-5-1" in tab.model_combo.itemData(0, 3)   # ToolTipRole carries the id


def test_get_settings_returns_the_selected_id(tab):
    tab.model_combo.setCurrentIndex(1)
    assert tab.get_settings().claude_model == "claude-opus-5"


def test_set_settings_selects_by_id_and_keeps_unknown_ids(tab):
    tab.set_settings(ProjectSettings(claude_model="claude-haiku-4-5"))
    assert tab.current_model_id() == "claude-haiku-4-5"
    tab.set_settings(ProjectSettings(claude_model="claude-retired-3"))
    assert tab.current_model_id() == "claude-retired-3"
    assert tab.model_combo.currentText().startswith("claude-retired-3")
    assert "not in the current list" in tab.model_combo.currentText()
    # the list itself is unchanged apart from the appended entry
    assert tab.model_combo.count() == 4


def test_newest_default_selects_the_first_entry(tab):
    tab.model_combo.setCurrentIndex(2)
    tab.set_settings(ProjectSettings())                     # default is "newest"
    assert ProjectSettings().claude_model == NEWEST_MODEL
    assert tab.current_model_id() == "claude-fable-5-1"


def test_selection_survives_a_list_refresh(tab):
    tab.set_settings(ProjectSettings(claude_model="claude-opus-5"))
    newer = [ModelInfo("claude-opus-6", "Claude Opus 6", "2027-01-01T00:00:00+00:00")] + MODELS
    tab.set_model_list(newer, ModelSource.LIVE)
    assert tab.current_model_id() == "claude-opus-5"          # a new model is never auto-selected
    assert tab.model_combo.itemData(0) == "claude-opus-6"


def test_status_line_names_the_source(tab):
    tab.set_model_list(MODELS, ModelSource.LIVE)
    assert "Anthropic" in tab.model_status_label.text() and "3" in tab.model_status_label.text()
    tab.set_model_list(MODELS, ModelSource.FALLBACK)
    assert "built-in" in tab.model_status_label.text().lower()
    tab.set_model_list(MODELS, ModelSource.CACHE, fetched_at_text="2026-09-05")
    assert "2026-09-05" in tab.model_status_label.text()


def test_saved_selection_is_stored_by_id_and_old_index_migrates(tab, isolated_settings):
    tab.set_settings(ProjectSettings(claude_model="claude-opus-5"))
    tab._save_settings()
    s = isolated_settings("AIREFs", "AIREFs")
    assert s.value("claude_model_id") == "claude-opus-5"
    assert s.value("claude_model_index", None) is None
    # an old profile that only stored the index of the previous hard-coded list
    s.remove("claude_model_id")
    s.setValue("claude_model_index", 2)                        # was "Opus 4.8"
    s.sync()
    from src.gui.inputs_tab import InputsTab
    t2 = InputsTab()
    t2.set_model_list(MODELS, ModelSource.CACHE)
    t2._load_saved_settings()
    assert t2.current_model_id() == "claude-opus-4-8"


def test_refresh_uses_the_typed_key_in_the_background(tab, monkeypatch):
    import src.gui.inputs_tab as it
    calls = []

    def fake_refresh(api_key, timeout=15.0):
        calls.append(api_key)
        return [ModelInfo("claude-opus-6", "Claude Opus 6", "2027-01-01T00:00:00+00:00")]
    monkeypatch.setattr(it, "refresh_models", fake_refresh)
    tab.anthropic_key_edit.setText("sk-ant-key")
    tab.request_model_refresh()
    tab.wait_for_model_refresh(timeout_ms=5000)
    assert calls == ["sk-ant-key"]
    assert tab.model_combo.itemData(0) == "claude-opus-6"
    assert "Anthropic" in tab.model_status_label.text()


def test_refresh_without_key_explains(tab):
    tab.anthropic_key_edit.setText("")
    tab.request_model_refresh()
    assert "API key" in tab.model_status_label.text()


def test_refresh_failure_keeps_the_list(tab, monkeypatch):
    import src.gui.inputs_tab as it

    def boom(api_key, timeout=15.0):
        raise ConnectionError("offline")
    monkeypatch.setattr(it, "refresh_models", boom)
    tab.anthropic_key_edit.setText("sk-ant-key")
    tab.request_model_refresh()
    tab.wait_for_model_refresh(timeout_ms=5000)
    assert tab.model_combo.count() == 3
    assert "could not" in tab.model_status_label.text().lower()


def test_new_pipeline_settings_round_trip_through_the_tab(tab):
    settings = ProjectSettings(parallel_searches=6, verify_citations=False, use_full_text=False,
                               prefer_reviews=True, recency_bias=False)
    tab.set_settings(settings)
    assert tab.parallel_spin.value() == 6
    assert not tab.verify_check.isChecked() and not tab.fulltext_check.isChecked()

    back = tab.get_settings()
    assert back.parallel_searches == 6
    assert back.verify_citations is False and back.use_full_text is False
    assert back.prefer_reviews is True and back.recency_bias is False
