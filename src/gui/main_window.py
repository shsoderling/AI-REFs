"""Main window for AI REFs application."""

import os
import shutil
import logging
import hashlib
from typing import Optional
from pathlib import Path
from collections import defaultdict
from datetime import datetime

from PySide6.QtWidgets import (
    QMainWindow, QTabWidget, QMessageBox, QFileDialog,
)
from PySide6.QtCore import Slot, Qt
from PySide6.QtGui import QAction, QCloseEvent, QIcon, QKeySequence

from ..models.project import (
    ProjectState, ProjectSettings, PipelineStage, CitationStyle,
    AUTHOR_DATE_STYLES, SUPERSCRIPT_STYLES,
)
from ..models.evidence import ReviewDecision
from ..models.markers import MarkerConfig, MarkerType, SuggestionKind
from ..services.docx_io import DocxHandler
from ..utils.markers import find_markers
from ..pipeline.existing_citation_parser import ExistingCitationParser
from ..pipeline.docx_export import (
    STRIPPED_NOTE, ExportBlocked, ExportDecisions, check_export_guard, export_fresh,
    export_legacy, export_tracked, fresh_append_needs_confirmation, looks_stripped,
)
from ..pipeline.citation_render import parse_csl_layout
from ..models.embedded import DocumentTier
from ..pipeline.renumbering import CitationKeyIndex
from ..pipeline.renumber_plan import build_renumber_plan
from ..pipeline.export_stats import ExportStats
from ..pipeline.renumber_apply import apply_renumbering
from ..pipeline.bib_format import format_bib_entry
from ..pipeline.author_date_convert import build_author_date_labels
from ..models.citation import is_valid_citation
from ..storage.project_io import save_project, load_project
from .inputs_tab import InputsTab
from .library_tab import LibraryTab
from .run_tab import RunTab
from .review_tab import ReviewTab
from .styles import MAIN_STYLESHEET

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Main application window with 4-tab layout."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._project = ProjectState()
        self._dirty = False  # True when there are unsaved changes
        self._setup_window()
        self._setup_menu()
        self._setup_tabs()
        self._connect_signals()
        self._update_title()

    def _setup_window(self):
        self.setWindowTitle("AI REFs — Reference Assistant")
        self.setMinimumSize(1000, 700)
        self.resize(1200, 800)
        self.setStyleSheet(MAIN_STYLESHEET)

        # Try to load icon
        icon_path = Path(__file__).parent.parent.parent / "assets" / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        # Status bar
        self.statusBar().showMessage("Ready")

    def _setup_menu(self):
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("File")

        new_action = QAction("New Project", self)
        new_action.setShortcut(QKeySequence.StandardKey.New)
        new_action.triggered.connect(self._new_project)
        file_menu.addAction(new_action)

        open_action = QAction("Open Project...", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._open_project)
        file_menu.addAction(open_action)

        save_action = QAction("Save Project", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self._save_project)
        file_menu.addAction(save_action)

        file_menu.addSeparator()

        export_action = QAction("Export Document...", self)
        export_action.setShortcut(QKeySequence("Ctrl+E"))
        export_action.triggered.connect(self._export_document)
        file_menu.addAction(export_action)

    def _setup_tabs(self):
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.inputs_tab = InputsTab()
        self.library_tab = LibraryTab()
        self.run_tab = RunTab()
        self.review_tab = ReviewTab()

        self.tabs.addTab(self.inputs_tab, "1. Input")
        self.tabs.addTab(self.library_tab, "2. REF Library")
        self.tabs.addTab(self.run_tab, "3. Run")
        self.tabs.addTab(self.review_tab, "4. Review")

    def _connect_signals(self):
        # Input tab -> file selection
        self.inputs_tab.file_selected.connect(self._on_file_selected)
        self.library_tab.settings_changed.connect(self._mark_dirty)

        # Run tab -> start button and completion
        self.run_tab.start_btn.clicked.connect(self._start_pipeline)
        self.run_tab.pipeline_complete.connect(self._on_pipeline_complete)

        # Review tab -> export, dirty tracking, and library sync
        self.review_tab.export_requested.connect(self._export_document)
        self.review_tab.preview_requested.connect(self._preview_numbering)
        self.review_tab.project_modified.connect(self._mark_dirty)
        self.review_tab.library_updated.connect(self.library_tab._refresh_stats)

    def _update_title(self):
        name = self._project.project_name
        modified = " *" if self._dirty else ""
        self.setWindowTitle(f"AI REFs — {name}{modified}")

    def _mark_dirty(self):
        """Mark the project as having unsaved changes."""
        self._dirty = True
        self._update_title()

    @Slot(str)
    def _on_file_selected(self, path: str):
        """Handle new DOCX file selection."""
        self._project.input_docx_path = path
        self._project.project_name = Path(path).stem

        # What the last export of this project wrote: lets us recognise that
        # export coming back without its citation fields.
        previous_output = self._project.output_docx_path
        previous_hashes = list(self._project.entry_hashes)

        # Discard results from any previously loaded document — sentence IDs
        # restart at S001 for every document, so stale evidence would attach
        # to the wrong sentences.
        self._project.sentences = []
        self._project.evidence_map = {}
        self._project.output_docx_path = None
        self._project.run_marker_config = None
        self.review_tab.load_project(None)

        # Compute hash for change detection
        self._project.input_docx_hash = self._hash_file(path)

        try:
            self._analyze_document(path, previous_output, previous_hashes)
        except Exception as e:                     # unreadable / corrupt / not a DOCX
            logger.exception("Could not open document")
            self._project.input_docx_path = None
            self._project.existing_citations = None
            self._project.is_insert_mode = False
            self.inputs_tab.set_document_mode("fresh")
            QMessageBox.critical(self, "Cannot Open Document",
                                 f"AI REFs could not read this file as a Word document:\n{e}")
            return

        self._mark_dirty()
        self.statusBar().showMessage(f"Loaded: {Path(path).name}")
        logger.info(f"Document loaded: {path}")

    @staticmethod
    def _unused_backup_path(input_path: Path) -> str:
        candidate = f"{input_path}.bak"
        n = 1
        while os.path.exists(candidate):
            n += 1
            candidate = f"{input_path}.bak{n}"
        return candidate

    @staticmethod
    def _hash_file(path: str) -> str:
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()

    @staticmethod
    def _document_mode(existing) -> str:
        """Banner mode for an analysed document (see InputsTab.set_document_mode)."""
        if existing is None:
            return "fresh"
        tracking = existing.tracking
        if tracking is not None:
            if tracking.tier == DocumentTier.FAILED:
                return "analysis-failed"
            if tracking.tier == DocumentTier.NEWER_VERSION:
                return "newer-version"
            if tracking.foreign_field_count:
                return "foreign"
            if tracking.tier == DocumentTier.TRACKED:
                return "tracked"
            if tracking.tier == DocumentTier.STRIPPED:
                return "stripped"
        return "legacy" if existing.has_existing_citations else "fresh"

    def _apply_document_mode(self, existing):
        mode = self._document_mode(existing)
        n = len(existing.bib_entries) if existing is not None else 0
        report = existing.tracking if existing is not None else None
        self.inputs_tab.set_document_mode(mode, report, n)
        return mode

    def _analyze_document(self, path: str, previous_output: Optional[str] = None,
                          previous_hashes: Optional[list[str]] = None):
        """Analyse the document's existing citations and set the project mode.

        ``analyze()`` never raises: a failure comes back as a FAILED tracking
        report, which is kept on the project so the export guard can refuse
        to write, and shown in the banner instead of silently treating the
        document as uncited. A former export that lost its citation fields
        (Google Docs, Pages...) is recognised from the project's mirror and
        read from its text.
        """
        handler = DocxHandler(path)
        existing = ExistingCitationParser(
            handler, keep_uncited=self._project.settings.keep_uncited_entries).analyze()
        if looks_stripped(existing, path, previous_output, previous_hashes or []):
            existing.tracking.tier = DocumentTier.STRIPPED
            existing.tracking.problems.append(STRIPPED_NOTE)
            logger.warning("Tracking data stripped from a former export: reading citations from text")
        self._project.existing_citations = existing
        self._project.doc_tracking = existing.tracking
        mode = self._apply_document_mode(existing)
        self._project.is_insert_mode = (mode in ("legacy", "foreign", "tracked", "stripped")
                                        and existing.has_existing_citations)
        # A tracked document can be exported (renumbered) without running
        # the pipeline: refresh the review tab so its export button follows.
        self.review_tab.load_project(self._project)
        if mode == "analysis-failed":
            logger.warning(f"Existing-citation analysis failed: {existing.tracking.problems}")
            return existing
        if existing.has_existing_citations:
            logger.info(f"Insert mode auto-detected: {len(existing.bib_entries)} existing references")
            # Leftover [?] tokens mean a previous export had unresolved
            # markers — those citations are still missing.
            leftover = sum(p.text.count("[?]") for p in handler.get_paragraphs())
            if leftover:
                QMessageBox.warning(
                    self, "Unresolved Placeholders Found",
                    f"This document contains {leftover} unresolved [?] "
                    "placeholder(s) from a previous export.\n\n"
                    "They will be left as-is. To fill them, replace each "
                    "[?] with a (REF) marker before running the pipeline.",
                )
        return existing

    def _start_pipeline(self):
        """Validate and start the pipeline."""
        if not self._project.input_docx_path:
            QMessageBox.warning(self, "No Document",
                              "Please load a DOCX file in the Input tab first.")
            return

        tracking = self._project.doc_tracking
        if tracking is not None and tracking.tier == DocumentTier.NEWER_VERSION:
            QMessageBox.warning(self, "Read-Only Document",
                                "This document was created by a newer version of AI REFs and "
                                "is opened read-only. Update AI REFs to work on it.")
            return

        # Collect settings
        settings = self.inputs_tab.get_settings()
        if not settings.ncbi_email:
            QMessageBox.warning(self, "Email Required",
                              "Please enter your email address in the NCBI settings.\n"
                              "This is required by NCBI E-utilities.")
            return

        if not settings.anthropic_api_key:
            QMessageBox.warning(self, "API Key Required",
                              "Please enter your Anthropic API key in the AI Settings.\n"
                              "This is required for Claude-powered citation search.")
            return

        self._project.settings = self.library_tab.apply_to_settings(settings)
        if not self._confirm_suggested_markers():
            return
        self._project.created_at = datetime.now().isoformat()

        # Switch to Run tab
        self.tabs.setCurrentIndex(2)

        # Start pipeline
        self.run_tab.start_pipeline(self._project)
        self.statusBar().showMessage("Pipeline running...")

    def _confirm_suggested_markers(self) -> bool:
        """Tell the user how many author-suggested citations will be verified.

        A document that already cites in author-year style can contain dozens
        of parentheticals; each one costs an AI evaluation.  The user can go
        ahead, restrict this run to (REF)/(REFS) markers, or cancel.
        Returns False when the run should not start.
        """
        config = self._project.marker_config
        if not (config.detect_ids or config.detect_author_year):
            return True
        try:
            handler = DocxHandler(self._project.input_docx_path)
            stop_at = -1
            if self._project.is_insert_mode and self._project.existing_citations:
                stop_at = self._project.existing_citations.body_end_para_idx
            n_search = n_ids = n_author_year = 0
            for idx, para in enumerate(handler.get_paragraphs()):
                if stop_at >= 0 and idx >= stop_at:
                    break
                text = para.text.strip()
                # Same heading rule as DocumentParser: headings are not processed
                if len(text) < 80 and not text.endswith('.') and para.style.name.startswith('Heading'):
                    continue
                for m in find_markers(text, config):
                    if m.kind != MarkerType.SUGGESTED:
                        n_search += 1
                    elif any(s.kind == SuggestionKind.AUTHOR_YEAR for s in m.suggestions):
                        n_author_year += 1
                    else:
                        n_ids += 1
        except Exception as exc:
            logger.warning(f"Marker pre-scan failed: {exc}")
            return True

        n_suggested = n_ids + n_author_year
        if n_suggested == 0:
            return True

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Author-suggested citations found")
        box.setText(
            f"This document contains {n_search} (REF)/(REFS) marker(s) to search and "
            f"{n_suggested} author-suggested citation marker(s) to verify "
            f"({n_ids} by PMID/PMCID/DOI, {n_author_year} by author and year)."
        )
        box.setInformativeText(
            "Each suggested citation is looked up in your library and the literature "
            "databases, then scored by the AI against its sentence so you can confirm "
            "or replace it in the Review tab. Unconfirmed suggestions keep their "
            "original text on export.\n\nVerify the suggested citations in this run? "
            "(To switch detection off for good, use the checkboxes in the Input tab.)"
        )
        verify_btn = box.addButton("Verify all", QMessageBox.ButtonRole.AcceptRole)
        refs_only_btn = box.addButton("Only (REF)/(REFS)", QMessageBox.ButtonRole.ActionRole)
        cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(verify_btn)
        box.exec()

        clicked = box.clickedButton()
        if clicked is None or clicked == cancel_btn:
            return False
        if clicked == refs_only_btn:
            # This run only: the settings (and the Input tab checkboxes) keep
            # the user's preference; the override is consumed by the run and
            # never written to the project file.
            self._project.run_marker_override = MarkerConfig.legacy()
        return True

    @Slot(object)
    def _on_pipeline_complete(self, project: ProjectState):
        """Handle pipeline completion."""
        self._project = project
        self._project.modified_at = datetime.now().isoformat()

        # Load results into review tab
        self.review_tab.load_project(self._project)

        self._mark_dirty()

        # Switch to review tab
        self.tabs.setCurrentIndex(3)
        self.statusBar().showMessage(
            f"Pipeline complete: {self._project.total_markers} markers, "
            f"{self._project.resolved_count} with candidates"
        )

    def _export_document(self):
        """Export the final document with citations."""
        if not self._project.input_docx_path:
            QMessageBox.warning(self, "No Document", "No document to export.")
            return

        if not self._project.all_resolved:
            result = QMessageBox.question(
                self, "Unresolved Sentences",
                "Some sentences are not yet resolved. Export anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if result == QMessageBox.StandardButton.No:
                return

        # Choose output path
        input_path = Path(self._project.input_docx_path)
        default_name = f"{input_path.stem}_with_refs.docx"
        output_path, _ = QFileDialog.getSaveFileName(
            self, "Save Document",
            str(input_path.parent / default_name),
            "Word Documents (*.docx)"
        )
        if not output_path:
            return

        existing = self._project.existing_citations
        is_tracked = (existing is not None and existing.tracking is not None
                      and existing.tracking.tier == DocumentTier.TRACKED)
        if is_tracked:
            mode = "tracked"
        else:
            mode = "legacy" if (self._project.is_insert_mode and existing) else "fresh"
        allow_fresh_append = False
        if mode == "fresh" and fresh_append_needs_confirmation(existing):
            choice = QMessageBox.question(
                self, "References Heading Found",
                "This document has a References heading but no numbered entries AI REFs "
                "can read (an author-date list, perhaps).\n\nTreat it as an uncited "
                "document and append a new bibliography?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if choice != QMessageBox.StandardButton.Yes:
                return
            allow_fresh_append = True
        reasons = check_export_guard(existing, mode, self._project.settings,
                                     allow_fresh_append=allow_fresh_append)
        if reasons:
            QMessageBox.critical(self, "Export Blocked", "\n\n".join(reasons))
            return

        # Exporting onto the input file: keep a backup of the original
        # (never overwriting an earlier backup).
        if Path(output_path).resolve() == input_path.resolve():
            backup = self._unused_backup_path(input_path)
            try:
                shutil.copy2(str(input_path), backup)
            except OSError as e:
                QMessageBox.critical(self, "Backup Failed",
                                     f"Could not back up the input document:\n{e}\n\n"
                                     "Choose a different output file.")
                return
            QMessageBox.warning(
                self, "Overwriting the Input Document",
                f"You chose to overwrite the input document.\n\n"
                f"The original was backed up to:\n{backup}")

        try:
            stats = self._do_export(output_path)
            if stats is None:
                self.statusBar().showMessage("Export cancelled")
                return
            self.statusBar().showMessage(f"Exported: {output_path}")
            summary = "\n".join(stats.summary_lines())
            message = f"Document exported to:\n{output_path}\n\n{summary}"
            if stats.unresolved_markers:
                message += (
                    "\n\nWarning: markers exported as [?] have no accepted "
                    "citation. Review those sentences and re-export."
                )
                QMessageBox.warning(self, "Export Complete (with gaps)", message)
            else:
                QMessageBox.information(self, "Export Complete", message)
        except ExportBlocked as e:
            QMessageBox.critical(self, "Export Blocked", "\n\n".join(e.reasons))
            logger.warning(f"Export blocked: {e.reasons}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Export failed:\n{e}")
            logger.error(f"Export error: {e}")

    def _preview_numbering(self):
        """Show the merged final numbering for insert mode without exporting."""
        if not (self._project.is_insert_mode and self._project.existing_citations):
            return
        if not self._project.input_docx_path:
            return
        try:
            handler = DocxHandler(self._project.input_docx_path)
            plan = build_renumber_plan(handler, self._project)
        except Exception as e:
            QMessageBox.critical(self, "Preview Error",
                                 f"Could not compute numbering:\n{e}")
            logger.error(f"Preview error: {e}")
            return

        from .renumber_preview_dialog import RenumberPreviewDialog
        dialog = RenumberPreviewDialog(plan.renumber_result, parent=self)
        dialog.exec()

    def _do_export(self, output_path: str) -> Optional[ExportStats]:
        """Route to the appropriate export path based on mode.

        Returns export statistics, or None if the user cancelled.
        """
        existing = self._project.existing_citations
        if (existing is not None and existing.tracking is not None
                and existing.tracking.tier == DocumentTier.TRACKED):
            return export_tracked(self._project, output_path)
        if self._project.is_insert_mode and existing:
            return self._do_insert_export(output_path)
        return self._do_fresh_export(output_path)

    def _do_fresh_export(self, output_path: str) -> ExportStats:
        """Export a fresh document (no pre-existing citations); headless in docx_export."""
        return export_fresh(self._project, output_path)

    # ── Insert-mode export ────────────────────────────────────────────

    def _do_insert_export(self, output_path: str) -> Optional[ExportStats]:
        """Export a plain-text (legacy) document; headless in docx_export.

        Only the questions the user must answer live here. Returns None if
        the user cancelled.
        """
        existing = self._project.existing_citations
        style = self._project.settings.citation_style
        decisions = ExportDecisions()
        if parse_csl_layout(style).is_author_date and existing.in_text_citations:
            labels = build_author_date_labels(existing)
            if labels.missing:
                choice = QMessageBox.question(
                    self, "Citation Style Mismatch",
                    "This document uses numbered citations and the selected "
                    "style is author-date, but author/year information could "
                    f"not be determined for {len(labels.missing)} existing reference(s) "
                    "(enable 'Enrich existing' and re-run the pipeline to "
                    "improve this).\n\n"
                    "Export using the document's numeric style instead?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if choice != QMessageBox.StandardButton.Yes:
                    return None
                decisions.convert_to_author_date = False
            else:
                choice = QMessageBox.question(
                    self, "Convert to Author-Date?",
                    "This document uses numbered citations. Convert all "
                    f"{len(labels.labels)} existing in-text "
                    "citations to author-date format (e.g. \"Smith et al., "
                    "2020\")?\n\n"
                    "Yes: convert everything to author-date (the document will "
                    "not be tracked).\n"
                    "No: keep the document's numeric style.",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No
                    | QMessageBox.StandardButton.Cancel,
                )
                if choice == QMessageBox.StandardButton.Cancel:
                    return None
                decisions.convert_to_author_date = (choice == QMessageBox.StandardButton.Yes)
        return export_legacy(self._project, output_path, decisions)

    def _new_project(self):
        self._project = ProjectState()
        self._dirty = False
        self.review_tab.load_project(None)
        self._update_title()
        self.statusBar().showMessage("New project created")

    def _open_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Project", "",
            "AI REFs Projects (*.airefsproj)"
        )
        if not path:
            return
        try:
            self._project = load_project(path)
            self._dirty = False
            self.review_tab.load_project(self._project)
            self.inputs_tab.set_settings(self._project.settings)
            self.library_tab.apply_project_settings(self._project.settings)
            message = f"Opened: {Path(path).name}"
            docx_path = self._project.input_docx_path
            if docx_path:
                self.inputs_tab.drop_zone.set_file(docx_path)
            if docx_path and os.path.exists(docx_path):
                current_hash = self._hash_file(docx_path)
                if current_hash != self._project.input_docx_hash:
                    # The document changed since the project was saved: the
                    # stored citation map (and any paragraph indices in it)
                    # can no longer be trusted.
                    self._project.input_docx_hash = current_hash
                    self._analyze_document(docx_path, self._project.output_docx_path,
                                           list(self._project.entry_hashes))
                    self.review_tab.load_project(self._project)
                    self._mark_dirty()
                    message += " — document changed since the project was saved; re-analysed"
                    if self._project.sentences or self._project.evidence_map:
                        QMessageBox.warning(
                            self, "Document Changed",
                            "The document was edited after this project was saved. Its "
                            "existing citations were re-analysed, but the pipeline results "
                            "(sentences and citations) may no longer line up with the "
                            "text. Re-run the pipeline before exporting.")
                else:
                    self._apply_document_mode(self._project.existing_citations)
            else:
                self._apply_document_mode(self._project.existing_citations)
            self._update_title()
            self.statusBar().showMessage(message)
        except Exception as e:
            QMessageBox.critical(self, "Open Error", f"Failed to open project:\n{e}")

    def _save_project(self):
        if self._project.project_path:
            path = self._project.project_path
        else:
            default_name = f"{self._project.project_name}.airefsproj"
            path, _ = QFileDialog.getSaveFileName(
                self, "Save Project", default_name,
                "AI REFs Projects (*.airefsproj)"
            )
            if not path:
                return
        try:
            save_project(self._project, path)
            self._dirty = False
            self._update_title()
            self.statusBar().showMessage(f"Saved: {Path(path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", f"Failed to save project:\n{e}")

    def closeEvent(self, event: QCloseEvent):
        """Prompt to save if there are unsaved changes."""
        if self._dirty:
            result = QMessageBox.question(
                self,
                "Unsaved Changes",
                "You have unsaved changes. Do you want to save before closing?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if result == QMessageBox.StandardButton.Save:
                self._save_project()
                # If save was cancelled (no path chosen), stay open
                if self._dirty:
                    event.ignore()
                    return
            elif result == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            # Discard → fall through and close

        # Stop background workers before Qt tears down their parents —
        # destroying a running QThread aborts the process.
        try:
            self.run_tab.shutdown_workers()
            self.review_tab.shutdown_workers()
        except Exception as e:
            logger.warning(f"Worker shutdown during close failed: {e}")
        event.accept()

    def keyPressEvent(self, event):
        """Handle keyboard shortcuts."""
        if event.key() == Qt.Key.Key_Return and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            # Ctrl+Enter: Accept current in review tab
            if self.tabs.currentIndex() == 3:
                self.review_tab._on_accept()
        super().keyPressEvent(event)
