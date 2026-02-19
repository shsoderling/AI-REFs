"""Tab 2: Pipeline execution with progress and log panel."""

import logging
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextEdit, QProgressBar, QFrame
)
from PySide6.QtCore import Signal, Slot, QThread, Qt
from PySide6.QtGui import QTextCharFormat, QColor, QFont

from ..models.project import ProjectState
from ..pipeline.orchestrator import PipelineOrchestrator

logger = logging.getLogger(__name__)


class PipelineWorker(QThread):
    """Background worker that runs the pipeline."""
    progress = Signal(str, int, int)  # stage_name, current, total
    log_message = Signal(str, str)    # level, message
    finished = Signal(object)         # ProjectState
    error = Signal(str)

    def __init__(self, project: ProjectState, parent=None):
        super().__init__(parent)
        self.project = project
        self.orchestrator = None

    def run(self):
        try:
            self.orchestrator = PipelineOrchestrator(
                self.project,
                progress_callback=lambda s, c, t: self.progress.emit(s, c, t),
                log_callback=lambda l, m: self.log_message.emit(l, m),
            )
            result = self.orchestrator.run()
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))

    def pause(self):
        if self.orchestrator:
            self.orchestrator.pause()

    def resume(self):
        if self.orchestrator:
            self.orchestrator.resume()

    def cancel(self):
        if self.orchestrator:
            self.orchestrator.cancel()


class StageIndicator(QFrame):
    """Visual indicator for a pipeline stage."""

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.name = name
        self._status = "pending"  # pending, active, complete, error

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)

        self.icon_label = QLabel("○")
        self.icon_label.setFixedWidth(20)
        layout.addWidget(self.icon_label)

        self.name_label = QLabel(name)
        self.name_label.setStyleSheet("font-size: 13px;")
        layout.addWidget(self.name_label)
        layout.addStretch()

        self._update_style()

    def set_status(self, status: str):
        self._status = status
        self._update_style()

    def _update_style(self):
        styles = {
            "pending": ("○", "#999", "normal"),
            "active": ("◉", "#4a90d9", "bold"),
            "complete": ("✓", "#4CAF50", "normal"),
            "error": ("✗", "#F44336", "bold"),
        }
        icon, color, weight = styles.get(self._status, styles["pending"])
        self.icon_label.setText(icon)
        self.icon_label.setStyleSheet(f"color: {color}; font-size: 16px; font-weight: {weight};")
        self.name_label.setStyleSheet(f"color: {color}; font-size: 13px; font-weight: {weight};")


class RunTab(QWidget):
    """Tab for running the pipeline with progress tracking."""
    pipeline_complete = Signal(object)  # ProjectState

    STAGE_NAMES_FRESH = [
        "Parse Document",
        "Locate Markers",
        "AI Citation Search",
        "Global QA",
    ]
    STAGE_NAMES_INSERT = [
        "Analyze Existing Citations",
        "Parse Document",
        "Locate Markers",
        "AI Citation Search",
        "Global QA",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = None
        self._is_paused = False
        self._active_stage_names = list(self.STAGE_NAMES_FRESH)
        self.stage_indicators = {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # Header
        header = QLabel("Pipeline Execution")
        header.setObjectName("sectionHeader")
        layout.addWidget(header)

        # Control buttons
        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("Start Pipeline")
        self.start_btn.setObjectName("primaryButton")
        self.start_btn.clicked.connect(self._on_start)
        btn_layout.addWidget(self.start_btn)

        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._on_pause)
        btn_layout.addWidget(self.pause_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, len(self._active_stage_names))
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        # Stage indicators
        self.stages_layout = QHBoxLayout()
        layout.addLayout(self.stages_layout)
        self._rebuild_stage_indicators(self._active_stage_names)

        # Log panel
        log_header = QLabel("Processing Log")
        log_header.setObjectName("subHeader")
        layout.addWidget(log_header)

        self.log_panel = QTextEdit()
        self.log_panel.setObjectName("logPanel")
        self.log_panel.setReadOnly(True)
        self.log_panel.setMinimumHeight(250)
        layout.addWidget(self.log_panel, stretch=1)

    def start_pipeline(self, project: ProjectState):
        """Start the pipeline with the given project state."""
        if self._worker and self._worker.isRunning():
            return

        stage_names = (
            self.STAGE_NAMES_INSERT if project.is_insert_mode
            else self.STAGE_NAMES_FRESH
        )
        self._rebuild_stage_indicators(stage_names)
        self._reset_ui()
        self._log("info", "Starting pipeline...")

        self._worker = PipelineWorker(project)
        self._worker.progress.connect(self._on_progress)
        self._worker.log_message.connect(self._on_log)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)

    def _on_start(self):
        """Called when Start button is clicked. Parent window handles this."""
        pass  # Overridden by main_window connection

    @Slot(str, int, int)
    def _on_progress(self, stage_name: str, current: int, total: int):
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(current)

        # Update stage indicators
        for i, name in enumerate(self._active_stage_names):
            if i < current:
                self.stage_indicators[name].set_status("complete")
            elif i == current:
                self.stage_indicators[name].set_status("active")
            else:
                self.stage_indicators[name].set_status("pending")

    @Slot(str, str)
    def _on_log(self, level: str, message: str):
        self._log(level, message)

    @Slot(object)
    def _on_finished(self, project: ProjectState):
        self._log("info", "Pipeline complete!")
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)

        # Mark all stages complete
        for name in self._active_stage_names:
            self.stage_indicators[name].set_status("complete")
        self.progress_bar.setValue(self.progress_bar.maximum())

        self.pipeline_complete.emit(project)

    @Slot(str)
    def _on_error(self, error_msg: str):
        self._log("error", f"Pipeline error: {error_msg}")
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)

    def _on_pause(self):
        if not self._worker:
            return
        if self._is_paused:
            self._worker.resume()
            self.pause_btn.setText("Pause")
            self._is_paused = False
            self._log("info", "Resumed")
        else:
            self._worker.pause()
            self.pause_btn.setText("Resume")
            self._is_paused = True
            self._log("info", "Paused")

    def _log(self, level: str, message: str):
        """Append a colored message to the log panel."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        colors = {
            "debug": "#888",
            "info": "#d4d4d4",
            "warning": "#FFC107",
            "error": "#F44336",
        }
        color = colors.get(level, "#d4d4d4")

        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        fmt.setFont(QFont("Courier New", 11))

        cursor = self.log_panel.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(f"[{timestamp}] ", fmt)
        cursor.insertText(f"{message}\n", fmt)

        self.log_panel.setTextCursor(cursor)
        self.log_panel.ensureCursorVisible()

    def _reset_ui(self):
        self.log_panel.clear()
        self.progress_bar.setRange(0, len(self._active_stage_names))
        self.progress_bar.setValue(0)
        for name in self._active_stage_names:
            self.stage_indicators[name].set_status("pending")
        self._is_paused = False
        self.pause_btn.setText("Pause")

    def _rebuild_stage_indicators(self, stage_names: list[str]):
        """Rebuild the stage indicator row for fresh or insert mode."""
        self._active_stage_names = list(stage_names)
        self.stage_indicators = {}

        while self.stages_layout.count():
            item = self.stages_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for name in self._active_stage_names:
            indicator = StageIndicator(name)
            self.stage_indicators[name] = indicator
            self.stages_layout.addWidget(indicator)
