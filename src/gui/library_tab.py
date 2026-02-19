"""REF Library tab: manage user reference library and EndNote import."""

import logging
from pathlib import Path

from PySide6.QtCore import Signal, Qt, QSettings
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QGroupBox, QLineEdit, QTextEdit, QCheckBox, QSpinBox,
    QMessageBox, QSplitter, QScrollArea, QFrame,
)

from ..models.project import ProjectSettings
from ..services.ref_library import ReferenceLibrary, DEFAULT_LIBRARY_PATH

logger = logging.getLogger(__name__)

# ── Help text content ─────────────────────────────────────────────────
LIBRARY_HELP_TEXT = (
    "<h3>How to Export from EndNote</h3>"
    "<p>Follow these steps to export your EndNote library for import "
    "into AI REFs:</p>"
    "<ol>"
    "<li><b>Open your library</b> in EndNote</li>"
    "<li><b>Select all references</b> in the citations panel<br>"
    "<i>(Select All, or &#8984;A / Ctrl+A in the citations panel)</i></li>"
    "<li>Go to <b>File &rarr; Export&hellip;</b></li>"
    "<li>In the export dialog set:<br>"
    "&bull;&ensp;<b>Save file as type:</b>&ensp;XML<br>"
    "&bull;&ensp;<b>Output Style:</b>&ensp;Show All Fields</li>"
    "<li><b>Choose a local destination</b> and click <b>Save</b></li>"
    "<li>Back in AI REFs, click <b>\"Browse Export File&hellip;\"</b>, "
    "select the saved XML file, then click "
    "<b>\"Import Into Active Library\"</b></li>"
    "</ol>"
    "<h3>Supported Formats</h3>"
    "<p><b>EndNote XML (.xml)</b> &mdash; Recommended. Preserves the most "
    "metadata including DOIs, PMIDs, abstracts, and keywords.</p>"
    "<p><b>RIS (.ris)</b> &mdash; Also supported. A widely-used plain-text "
    "format that works well but may carry slightly less metadata than XML.</p>"
    "<p><b>EndNote .enl files</b> are not directly readable &mdash; you must "
    "export to XML or RIS first using the steps above.</p>"
)


class LibraryTab(QWidget):
    """Tab for selecting/importing a user reference library."""

    settings_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._library_db_path = str(DEFAULT_LIBRARY_PATH)
        self._import_file_path = ""
        self._suppress_change_signal = False
        self._help_visible = False
        self._setup_ui()
        self._load_saved_settings()
        self._refresh_stats()

    def _setup_ui(self):
        # Top-level: horizontal splitter between content and help panel
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        self.splitter = QSplitter(Qt.Horizontal)

        # ── LEFT: scrollable content panel ────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")

        form_widget = QWidget()
        layout = QVBoxLayout(form_widget)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        header = QLabel("REF Library")
        header.setObjectName("sectionHeader")
        layout.addWidget(header)

        description = QLabel(
            "Load your reference library (including EndNote exports) and let AI REFs "
            "prefer your existing papers before searching new literature."
        )
        description.setWordWrap(True)
        description.setStyleSheet("color: #444; font-size: 13px;")
        layout.addWidget(description)

        # Library location / default manager library
        lib_group = QGroupBox("Active Library")
        lib_layout = QVBoxLayout(lib_group)
        lib_layout.setSpacing(8)

        path_row = QHBoxLayout()
        self.library_path_edit = QLineEdit()
        self.library_path_edit.setReadOnly(True)
        path_row.addWidget(self.library_path_edit, stretch=1)

        self.use_default_btn = QPushButton("Use Default Library")
        self.use_default_btn.clicked.connect(self._on_use_default_library)
        path_row.addWidget(self.use_default_btn)

        self.choose_library_btn = QPushButton("Choose Library File...")
        self.choose_library_btn.clicked.connect(self._on_choose_library)
        path_row.addWidget(self.choose_library_btn)
        lib_layout.addLayout(path_row)

        self.library_stats_label = QLabel("Library size: 0 references")
        self.library_stats_label.setStyleSheet("color: #2c3e50; font-size: 12px;")
        lib_layout.addWidget(self.library_stats_label)

        layout.addWidget(lib_group)

        # Import from EndNote export
        import_group = QGroupBox("Import EndNote Library Export")
        import_layout = QVBoxLayout(import_group)
        import_layout.setSpacing(8)

        import_header_row = QHBoxLayout()
        import_help = QLabel(
            "Supported formats: EndNote RIS (.ris) and EndNote XML (.xml). "
            "If your library is a .enl file, export it from EndNote first."
        )
        import_help.setWordWrap(True)
        import_help.setStyleSheet("color: #555; font-size: 12px;")
        import_header_row.addWidget(import_help, stretch=1)

        self.import_help_btn = QPushButton("?")
        self.import_help_btn.setFixedSize(28, 28)
        self.import_help_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.import_help_btn.setStyleSheet(
            "QPushButton { background-color: #4a90d9; color: white; "
            "font-weight: bold; font-size: 14px; border-radius: 14px; border: none; }"
            "QPushButton:hover { background-color: #357abd; }"
            "QPushButton:pressed { background-color: #2a5f9e; }"
        )
        self.import_help_btn.setToolTip("How to export from EndNote")
        self.import_help_btn.clicked.connect(self._toggle_help)
        import_header_row.addWidget(self.import_help_btn)

        import_layout.addLayout(import_header_row)

        import_row = QHBoxLayout()
        self.import_path_edit = QLineEdit()
        self.import_path_edit.setReadOnly(True)
        import_row.addWidget(self.import_path_edit, stretch=1)

        self.browse_import_btn = QPushButton("Browse Export File...")
        self.browse_import_btn.clicked.connect(self._on_browse_import_file)
        import_row.addWidget(self.browse_import_btn)

        self.import_btn = QPushButton("Import Into Active Library")
        self.import_btn.setObjectName("primaryButton")
        self.import_btn.clicked.connect(self._on_import_library)
        import_row.addWidget(self.import_btn)
        import_layout.addLayout(import_row)

        self.import_log = QTextEdit()
        self.import_log.setReadOnly(True)
        self.import_log.setMinimumHeight(120)
        self.import_log.setPlaceholderText("Import results will appear here.")
        import_layout.addWidget(self.import_log)

        layout.addWidget(import_group)

        # Run-time behavior controls
        behavior_group = QGroupBox("Run Behavior")
        behavior_layout = QVBoxLayout(behavior_group)
        behavior_layout.setSpacing(8)

        self.enable_library_check = QCheckBox("Search this library during AI citation runs")
        self.enable_library_check.setChecked(True)
        self.enable_library_check.stateChanged.connect(self._on_setting_changed)
        behavior_layout.addWidget(self.enable_library_check)

        self.prefer_library_check = QCheckBox("Prefer library citations when relevance is similar")
        self.prefer_library_check.setChecked(True)
        self.prefer_library_check.stateChanged.connect(self._on_setting_changed)
        behavior_layout.addWidget(self.prefer_library_check)

        max_row = QHBoxLayout()
        max_row.addWidget(QLabel("Max library matches per query:"))
        self.max_results_spin = QSpinBox()
        self.max_results_spin.setRange(3, 50)
        self.max_results_spin.setValue(10)
        self.max_results_spin.valueChanged.connect(self._on_setting_changed)
        max_row.addWidget(self.max_results_spin)
        max_row.addStretch()
        behavior_layout.addLayout(max_row)

        layout.addWidget(behavior_group)
        layout.addStretch()

        scroll.setWidget(form_widget)
        self.splitter.addWidget(scroll)

        # ── RIGHT: Help panel ─────────────────────────────────────────
        self.help_panel = QFrame()
        self.help_panel.setStyleSheet(
            "QFrame#helpPanel {"
            "  background-color: #f8f9fa;"
            "  border-left: 1px solid #ddd;"
            "}"
        )
        self.help_panel.setObjectName("helpPanel")
        help_panel_layout = QVBoxLayout(self.help_panel)
        help_panel_layout.setContentsMargins(16, 16, 16, 16)
        help_panel_layout.setSpacing(8)

        # Help panel header
        help_header_layout = QHBoxLayout()
        help_title = QLabel("Help")
        help_title.setStyleSheet("font-size: 16px; font-weight: bold; color: #2c3e50;")
        help_header_layout.addWidget(help_title)
        help_header_layout.addStretch()

        self.help_close_btn = QPushButton("\u2715")
        self.help_close_btn.setFixedSize(24, 24)
        self.help_close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.help_close_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; font-size: 16px; color: #888; }"
            "QPushButton:hover { color: #e74c3c; }"
        )
        self.help_close_btn.clicked.connect(self._hide_help)
        help_header_layout.addWidget(self.help_close_btn)
        help_panel_layout.addLayout(help_header_layout)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #ddd;")
        help_panel_layout.addWidget(sep)

        # Help content area
        self.help_content = QTextEdit()
        self.help_content.setReadOnly(True)
        self.help_content.setStyleSheet(
            "QTextEdit {"
            "  background-color: transparent;"
            "  border: none;"
            "  font-size: 13px;"
            "  color: #333;"
            "  line-height: 1.5;"
            "}"
        )
        help_panel_layout.addWidget(self.help_content)

        self.splitter.addWidget(self.help_panel)

        # Start with help panel hidden
        self.help_panel.setVisible(False)
        self.splitter.setSizes([600, 0])

        outer_layout.addWidget(self.splitter)

    # ------------------------------------------------------------------
    # Help panel
    # ------------------------------------------------------------------

    def _toggle_help(self):
        """Toggle the help panel open/closed."""
        if self._help_visible:
            self._hide_help()
        else:
            self._show_help()

    def _show_help(self):
        """Show the help panel with EndNote export instructions."""
        self._help_visible = True
        self.help_content.setHtml(LIBRARY_HELP_TEXT)
        if not self.help_panel.isVisible():
            self.help_panel.setVisible(True)
            total = self.splitter.width() or 900
            self.splitter.setSizes([int(total * 0.6), int(total * 0.4)])

    def _hide_help(self):
        """Hide the help panel."""
        self._help_visible = False
        self.help_panel.setVisible(False)

    # ------------------------------------------------------------------
    # Public API used by MainWindow
    # ------------------------------------------------------------------

    def apply_project_settings(self, settings: ProjectSettings):
        """Set tab state from saved project settings."""
        self._suppress_change_signal = True
        path = settings.reference_library_path or self._library_db_path
        self._set_library_path(path, save=False)
        self.enable_library_check.setChecked(settings.reference_library_enabled)
        self.prefer_library_check.setChecked(settings.prefer_user_library)
        self.max_results_spin.setValue(settings.max_library_results)
        self._suppress_change_signal = False
        self._refresh_stats()

    def apply_to_settings(self, settings: ProjectSettings) -> ProjectSettings:
        """Overlay library-tab values onto a ProjectSettings instance."""
        settings.reference_library_enabled = self.enable_library_check.isChecked()
        settings.reference_library_path = self._library_db_path
        settings.prefer_user_library = self.prefer_library_check.isChecked()
        settings.max_library_results = self.max_results_spin.value()
        self._save_settings()
        return settings

    # ------------------------------------------------------------------
    # Internal behavior
    # ------------------------------------------------------------------

    def _on_use_default_library(self):
        self._set_library_path(str(DEFAULT_LIBRARY_PATH))

    def _on_choose_library(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Choose AI REFs Library File",
            self._library_db_path,
            "SQLite Database (*.db)",
        )
        if path:
            self._set_library_path(path)

    def _on_browse_import_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select EndNote Export File",
            "",
            "EndNote Exports (*.ris *.xml *.enl);;All Files (*)",
        )
        if path:
            self._import_file_path = path
            self.import_path_edit.setText(path)

    def _on_import_library(self):
        if not self._import_file_path:
            self.import_log.append("Select a .ris or .xml file first.")
            return

        lib = ReferenceLibrary(self._library_db_path)
        try:
            report = lib.import_from_path(self._import_file_path)
        finally:
            lib.close()

        if report.errors:
            for err in report.errors:
                self.import_log.append(f"Error: {err}")
        else:
            self.import_log.append(
                f"Imported {report.imported}, updated {report.updated}, skipped {report.skipped} "
                f"(records seen: {report.total_records})"
            )

        self._refresh_stats()
        self._on_setting_changed()

    def _set_library_path(self, path: str, save: bool = True):
        if not path:
            return
        self._library_db_path = self._resolve_library_path(path)
        self.library_path_edit.setText(self._library_db_path)

        self._refresh_stats()
        if save:
            self._save_settings()
        self._on_setting_changed()

    def _refresh_stats(self):
        try:
            lib = ReferenceLibrary(self._library_db_path)
            total = lib.count()
            lib.close()
        except Exception as exc:
            total = 0
            logger.warning(f"Failed to read library stats: {exc}")
        self.library_stats_label.setText(f"Library size: {total} references")
        self.library_path_edit.setText(self._library_db_path)

    def _on_setting_changed(self):
        if self._suppress_change_signal:
            return
        self.settings_changed.emit()

    def _load_saved_settings(self):
        s = QSettings("AIREFs", "AIREFs")
        path = s.value("ref_library_path", str(DEFAULT_LIBRARY_PATH))
        self._library_db_path = self._resolve_library_path(str(path))
        self.enable_library_check.setChecked(s.value("ref_library_enabled", True, type=bool))
        self.prefer_library_check.setChecked(s.value("prefer_user_library", True, type=bool))
        self.max_results_spin.setValue(int(s.value("max_library_results", 10)))
        self.library_path_edit.setText(self._library_db_path)
        self._save_settings()

    def _resolve_library_path(self, path: str) -> str:
        """Resolve and initialize library path, falling back to default on error."""
        target = str(Path(path).expanduser())
        try:
            lib = ReferenceLibrary(target)
            lib.close()
            return target
        except Exception as exc:
            fallback = str(DEFAULT_LIBRARY_PATH)
            logger.warning(
                f"Could not open library '{target}' ({exc}); falling back to '{fallback}'"
            )
            lib = ReferenceLibrary(fallback)
            lib.close()
            return fallback

    def _save_settings(self):
        s = QSettings("AIREFs", "AIREFs")
        s.setValue("ref_library_path", self._library_db_path)
        s.setValue("ref_library_enabled", self.enable_library_check.isChecked())
        s.setValue("prefer_user_library", self.prefer_library_check.isChecked())
        s.setValue("max_library_results", self.max_results_spin.value())
        s.sync()
