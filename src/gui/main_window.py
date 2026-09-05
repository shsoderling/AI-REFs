"""Main window for AI REFs application."""

import os
import re
import shutil
import logging
import hashlib
from typing import Optional
from pathlib import Path
from collections import defaultdict
from datetime import datetime

from PySide6.QtWidgets import (
    QMainWindow, QTabWidget, QMessageBox, QFileDialog,
    QMenuBar, QStatusBar
)
from PySide6.QtCore import Slot, Qt
from PySide6.QtGui import QAction, QCloseEvent, QIcon, QKeySequence

from ..models.project import (
    ProjectState, ProjectSettings, PipelineStage, CitationStyle,
    AUTHOR_DATE_STYLES, SUPERSCRIPT_STYLES, get_csl_path,
)
from ..models.evidence import ReviewDecision
from ..models.sentence import MarkerType
from ..services.docx_io import DocxHandler
from ..pipeline.existing_citation_parser import ExistingCitationParser
from ..pipeline.docx_export import ExportBlocked, check_export_guard
from ..models.embedded import DocumentTier
from ..pipeline.renumbering import CitationKeyIndex
from ..pipeline.renumber_plan import build_renumber_plan
from ..pipeline.export_stats import ExportStats
from ..pipeline.renumber_apply import apply_renumbering
from ..pipeline.bib_format import format_bib_entry
from ..pipeline.author_date_convert import (
    build_author_date_labels, convert_in_text_to_author_date,
    build_author_date_bibliography,
)
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

        # Discard results from any previously loaded document — sentence IDs
        # restart at S001 for every document, so stale evidence would attach
        # to the wrong sentences.
        self._project.sentences = []
        self._project.evidence_map = {}
        self._project.output_docx_path = None
        self.review_tab.load_project(None)

        # Compute hash for change detection
        self._project.input_docx_hash = self._hash_file(path)

        self._analyze_document(path)

        self._mark_dirty()
        self.statusBar().showMessage(f"Loaded: {Path(path).name}")
        logger.info(f"Document loaded: {path}")

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
            if tracking.foreign_field_count:
                return "foreign"
        return "legacy" if existing.has_existing_citations else "fresh"

    def _apply_document_mode(self, existing):
        mode = self._document_mode(existing)
        n = len(existing.bib_entries) if existing is not None else 0
        report = existing.tracking if existing is not None else None
        self.inputs_tab.set_document_mode(mode, report, n)
        return mode

    def _analyze_document(self, path: str):
        """Analyse the document's existing citations and set the project mode.

        ``analyze()`` never raises: a failure comes back as a FAILED tracking
        report, which is kept on the project so the export guard can refuse
        to write, and shown in the banner instead of silently treating the
        document as uncited.
        """
        handler = DocxHandler(path)
        existing = ExistingCitationParser(handler).analyze()
        self._project.existing_citations = existing
        mode = self._apply_document_mode(existing)
        self._project.is_insert_mode = mode in ("legacy", "foreign") and existing.has_existing_citations
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
        self._project.created_at = datetime.now().isoformat()

        # Switch to Run tab
        self.tabs.setCurrentIndex(2)

        # Start pipeline
        self.run_tab.start_pipeline(self._project)
        self.statusBar().showMessage("Pipeline running...")

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

        # Exporting onto the input file: keep a backup of the original.
        if Path(output_path).resolve() == input_path.resolve():
            backup = f"{input_path}.bak"
            shutil.copy2(str(input_path), backup)
            QMessageBox.warning(
                self, "Overwriting the Input Document",
                f"You chose to overwrite the input document.\n\n"
                f"The original was backed up to:\n{backup}")

        mode = "legacy" if (self._project.is_insert_mode
                            and self._project.existing_citations) else "fresh"
        reasons = check_export_guard(
            self._project.existing_citations, mode, self._project.settings)
        if reasons:
            QMessageBox.critical(self, "Export Blocked", "\n\n".join(reasons))
            return

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

    # ── CSL-derived citation formatting helpers ──────────────────────

    @staticmethod
    def _parse_csl_citation_layout(style: CitationStyle) -> dict:
        """Parse the <citation><layout> element from the CSL file.

        Returns dict with keys: prefix, suffix, delimiter, is_author_date.
        Falls back to sensible defaults (numeric with brackets) on error.
        """
        defaults = {"prefix": "[", "suffix": "]", "delimiter": ", ",
                     "is_author_date": style in AUTHOR_DATE_STYLES}
        try:
            import xml.etree.ElementTree as ET
            csl_path = get_csl_path(style)
            if not csl_path.exists():
                return defaults
            tree = ET.parse(csl_path)
            root = tree.getroot()
            ns = {"csl": "http://purl.org/net/xbiblio/csl"}

            citation_el = root.find(".//csl:citation", ns)
            if citation_el is None:
                return defaults
            layout_el = citation_el.find(".//csl:layout", ns)
            if layout_el is None:
                return defaults

            return {
                "prefix": layout_el.get("prefix", ""),
                "suffix": layout_el.get("suffix", ""),
                "delimiter": layout_el.get("delimiter", ","),
                "is_author_date": style in AUTHOR_DATE_STYLES,
            }
        except Exception as exc:
            logger.warning(f"Could not parse CSL for {style}: {exc}")
            return defaults

    def _do_export(self, output_path: str) -> Optional[ExportStats]:
        """Route to the appropriate export path based on mode.

        Returns export statistics, or None if the user cancelled.
        """
        if self._project.is_insert_mode and self._project.existing_citations:
            return self._do_insert_export(output_path)
        return self._do_fresh_export(output_path)

    def _do_fresh_export(self, output_path: str) -> ExportStats:
        """Export a fresh document (no pre-existing citations).

        Matches DOCX markers to sentence evidence using paragraph_index
        (structural matching) instead of text-content heuristics.  Within a
        paragraph that contains multiple markers, markers are matched to
        sentences in document order.
        """
        handler = DocxHandler(self._project.input_docx_path)
        style = self._project.settings.citation_style

        # Parse the CSL file once for in-text citation formatting
        csl_info = self._parse_csl_citation_layout(style)
        is_author_date = csl_info["is_author_date"]
        cite_prefix = csl_info["prefix"]
        cite_suffix = csl_info["suffix"]
        cite_delim = csl_info["delimiter"]

        use_superscript = style in SUPERSCRIPT_STYLES

        logger.info(f"Export style: {style.value}  author-date={is_author_date}  "
                     f"superscript={use_superscript}  "
                     f"prefix={cite_prefix!r}  suffix={cite_suffix!r}  delim={cite_delim!r}")

        # Build bibliography
        bib_entries = []
        bib_number = {}
        key_index = CitationKeyIndex()
        current_num = 1

        # ── Build a lookup: paragraph_index → expanded list of (sentence, evidence, ref_index) ──
        para_to_markers: dict[int, list[tuple]] = defaultdict(list)
        for sent in self._project.sentences:
            if sent.marker_type is not None:
                ev = self._project.evidence_map.get(sent.id)
                count = max(sent.marker_count, 1)
                # Per-marker slots whenever the orchestrator ran independent
                # per-marker searches (SentenceRecord.searched_per_marker).
                if sent.searched_per_marker:
                    for ref_idx in range(count):
                        para_to_markers[sent.paragraph_index].append((sent, ev, ref_idx))
                else:
                    for _ in range(count):
                        para_to_markers[sent.paragraph_index].append((sent, ev, None))

        # ── Collect all markers from the DOCX ──
        markers = handler.find_markers()
        para_marker_counter: dict[int, int] = defaultdict(int)

        total_expanded = sum(len(v) for v in para_to_markers.values())
        logger.info(f"Export: {len(markers)} DOCX markers, "
                     f"{total_expanded} expanded sentence-marker slots")

        stats = ExportStats(total_markers=len(markers), output_path=output_path)

        for marker_info in markers:
            para = marker_info['paragraph']
            para_idx = marker_info['para_index']
            marker_type = marker_info['marker_type']
            marker_text = f"({marker_type})"

            expanded_list = para_to_markers.get(para_idx, [])
            marker_order = para_marker_counter[para_idx]
            para_marker_counter[para_idx] += 1

            matching_ev = None
            matching_sent = None
            ref_index = None

            if marker_order < len(expanded_list):
                matching_sent, ev, ref_index = expanded_list[marker_order]
                if ev and ev.review_decision in (ReviewDecision.ACCEPTED, ReviewDecision.MODIFIED):
                    matching_ev = ev

            sent_id = matching_sent.id if matching_sent else "???"
            sent_preview = (matching_sent.clean_text[:50] + "...") if matching_sent else "NO MATCH"
            if matching_ev and matching_ev.selected:
                ref_titles = "; ".join(s.title[:40] for s in matching_ev.selected)
                logger.info(f"  Marker para={para_idx}[{marker_order}] ref_index={ref_index} -> {sent_id} "
                            f"({sent_preview}) -> refs=[{ref_titles}]")
            else:
                logger.info(f"  Marker para={para_idx}[{marker_order}] -> {sent_id} "
                            f"({sent_preview}) -> NO EVIDENCE")

            refs_for_marker = []
            if matching_ev and matching_ev.selected:
                if ref_index is not None:
                    if ref_index < len(matching_ev.selected):
                        refs_for_marker = [matching_ev.selected[ref_index]]
                else:
                    refs_for_marker = matching_ev.selected
                # Placeholder slots ("No citation found") must never become
                # bibliography entries — drop them so the marker gets [?].
                refs_for_marker = [c for c in refs_for_marker if is_valid_citation(c)]

            if refs_for_marker:
                citation_parts = []
                for sel in refs_for_marker:
                    bib_key = key_index.key_for_candidate(sel)
                    if bib_key not in bib_number:
                        bib_number[bib_key] = current_num
                        bib_entries.append(self._format_bib_entry(sel, current_num, style))
                        current_num += 1

                    num = bib_number[bib_key]
                    if is_author_date:
                        citation_parts.append(sel.first_author_year)
                    else:
                        citation_parts.append(str(num))

                inner = cite_delim.join(citation_parts)
                replacement = f"{cite_prefix}{inner}{cite_suffix}"
                handler.replace_marker_by_regex(para, marker_text, replacement,
                                               superscript=use_superscript)
                stats.resolved_markers += 1
            else:
                handler.replace_marker_by_regex(para, marker_text, "[?]")
                stats.unresolved_markers += 1
                if matching_sent and matching_sent.id not in stats.unresolved_sentence_ids:
                    stats.unresolved_sentence_ids.append(matching_sent.id)
                logger.warning(f"  Marker para={para_idx}[{marker_order}] exported as [?]")

        if bib_entries:
            handler.append_bibliography(bib_entries)

        handler.save(output_path)
        self._project.output_docx_path = output_path
        stats.new_refs_added = len(bib_entries)
        stats.bibliography_size = len(bib_entries)
        logger.info(f"Exported document with {len(bib_entries)} bibliography entries: {output_path}")
        return stats

    # ── Insert-mode export ────────────────────────────────────────────

    def _do_insert_export(self, output_path: str) -> Optional[ExportStats]:
        """Export a document in insert mode: resolve new markers + renumber.

        Steps:
        1. Match new (REF)/(REFS) markers to their resolved citations
        2. Compute merged renumbering across existing + new citations
        3. Renumber all existing in-text citation numbers (before inserting new ones)
        4. Replace new markers with assigned citation numbers
        5. Remove old References section and append merged bibliography

        Returns export statistics, or None if the user cancelled.
        """
        handler = DocxHandler(self._project.input_docx_path)
        existing = self._project.existing_citations
        style = self._project.settings.citation_style

        csl_info = self._parse_csl_citation_layout(style)
        is_author_date = csl_info["is_author_date"]
        cite_prefix = csl_info["prefix"]
        cite_suffix = csl_info["suffix"]
        cite_delim = csl_info["delimiter"]
        use_superscript = style in SUPERSCRIPT_STYLES

        # An author-date style cannot coherently merge with a numerically-cited
        # document unless the existing numeric citations are converted too.
        # Offer the conversion when every cited entry has author/year info
        # (best with PubMed enrichment); otherwise fall back to numeric.
        convert_existing_to_author_date = False
        author_date_labels = None
        if is_author_date and existing.in_text_citations:
            author_date_labels = build_author_date_labels(existing)
            if author_date_labels.missing:
                n_missing = len(author_date_labels.missing)
                choice = QMessageBox.question(
                    self, "Citation Style Mismatch",
                    "This document uses numbered citations and the selected "
                    "style is author-date, but author/year information could "
                    f"not be determined for {n_missing} existing reference(s) "
                    "(enable 'Enrich existing' and re-run the pipeline to "
                    "improve this).\n\n"
                    "Export using the document's numeric style instead?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if choice != QMessageBox.StandardButton.Yes:
                    return None
                is_author_date = False
            else:
                choice = QMessageBox.question(
                    self, "Convert to Author-Date?",
                    "This document uses numbered citations. Convert all "
                    f"{len(author_date_labels.labels)} existing in-text "
                    "citations to author-date format (e.g. \"Smith et al., "
                    "2020\")?\n\n"
                    "Yes: convert everything to author-date.\n"
                    "No: keep the document's numeric style.",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No
                    | QMessageBox.StandardButton.Cancel,
                )
                if choice == QMessageBox.StandardButton.Cancel:
                    return None
                if choice == QMessageBox.StandardButton.Yes:
                    convert_existing_to_author_date = True
                else:
                    is_author_date = False

        # Author-date in-text citations need parenthesized, "; "-joined text;
        # fall back to that when the CSL layout gives empty delimiters.
        if is_author_date and not (cite_prefix or cite_suffix):
            cite_prefix, cite_suffix, cite_delim = "(", ")", "; "

        # New citations must match the EXISTING document's in-text style, not
        # the nominal CSL delimiters — otherwise renumbered existing citations
        # (which keep their original brackets/superscript) and freshly inserted
        # ones would look different (e.g. existing "[3]" next to new "(2)").
        if not is_author_date:
            use_superscript = existing.detected_style_is_superscript
            if use_superscript:
                cite_prefix, cite_suffix, cite_delim = "", "", ","
            else:
                cite_prefix, cite_suffix, cite_delim = "[", "]", ", "

        logger.info(f"Insert-mode export: style={style.value}  "
                     f"existing_refs={len(existing.bib_entries)}  "
                     f"superscript={use_superscript}")

        # ── Steps 1+2: Match markers to evidence and compute renumbering ──
        plan = build_renumber_plan(handler, self._project)
        markers = plan.markers
        marker_resolved_map = plan.marker_resolved_map
        marker_sentences = plan.marker_sentences
        renumber_result = plan.renumber_result
        renumber_map = renumber_result.renumber_map

        stats = ExportStats(total_markers=len(markers), output_path=output_path)

        logger.info(f"Renumbering: {len(renumber_result.assignments)} total citations, "
                     f"renumber_map has {sum(1 for o, n in renumber_map.items() if o != n)} changes")

        # ── Step 3: Update existing in-text citations ──
        # Must happen BEFORE replacing new markers, otherwise the newly-inserted
        # citation text would be caught and double-processed.
        if convert_existing_to_author_date:
            converted = convert_in_text_to_author_date(
                handler, existing, author_date_labels.labels,
                prefix=cite_prefix, suffix=cite_suffix, delimiter=cite_delim,
            )
            stats.citations_converted = converted
        elif not is_author_date and any(o != n for o, n in renumber_map.items()):
            self._renumber_existing_citations(handler, existing, renumber_map)

        # ── Step 4: Replace new (REF)/(REFS) markers ──
        merged_duplicate_numbers: set[int] = set()
        for i, marker_info in enumerate(markers):
            para = marker_info['paragraph']
            marker_type = marker_info['marker_type']
            marker_text = f"({marker_type})"
            resolved = marker_resolved_map[i]

            if resolved and not is_author_date:
                citation_numbers = []
                for cand in resolved:
                    num = renumber_result.number_for_candidate(cand)
                    if num is not None:
                        citation_numbers.append(num)
                        if not renumber_result.assignments[num].is_new:
                            merged_duplicate_numbers.add(num)

                inner = cite_delim.join(str(n) for n in citation_numbers)
                replacement = f"{cite_prefix}{inner}{cite_suffix}"
                handler.replace_marker_by_regex(para, marker_text, replacement,
                                               superscript=use_superscript)
                stats.resolved_markers += 1
            elif resolved and is_author_date:
                parts = [cand.first_author_year for cand in resolved]
                replacement = f"{cite_prefix}{cite_delim.join(parts)}{cite_suffix}"
                handler.replace_marker_by_regex(para, marker_text, replacement,
                                               superscript=False)
                stats.resolved_markers += 1
            else:
                handler.replace_marker_by_regex(para, marker_text, "[?]")
                stats.unresolved_markers += 1
                sent = marker_sentences[i]
                if sent and sent.id not in stats.unresolved_sentence_ids:
                    stats.unresolved_sentence_ids.append(sent.id)
                logger.warning(f"  Insert-mode marker {i} exported as [?]")

        # ── Step 5: Remove old References section, build merged bibliography ──
        handler.remove_references_section(existing.references_heading_para_idx)
        if is_author_date:
            merged_bib = build_author_date_bibliography(
                existing, renumber_result, style)
        else:
            merged_bib = self._build_merged_bibliography(
                existing, renumber_result, style)
        if merged_bib:
            handler.append_bibliography(merged_bib)

        handler.save(output_path)
        self._project.output_docx_path = output_path
        stats.new_refs_added = sum(
            1 for a in renumber_result.assignments.values() if a.is_new)
        stats.existing_refs_renumbered = sum(
            1 for o, n in renumber_map.items() if o != n)
        stats.duplicates_merged = len(merged_duplicate_numbers)
        stats.bibliography_size = len(merged_bib)
        logger.info(f"Insert-mode export complete: {len(merged_bib)} bibliography entries: {output_path}")
        return stats

    def _renumber_existing_citations(self, handler: DocxHandler,
                                      existing, renumber_map: dict[int, int]):
        """Update existing in-text citation numbers (delegates to pipeline)."""
        apply_renumbering(handler, existing, renumber_map)

    def _build_merged_bibliography(self, existing, renumber_result, style: CitationStyle) -> list[str]:
        """Build the final merged bibliography in correct number order.

        Combines existing entries (with updated numbers) and new entries.
        """
        entries = []
        for num in sorted(renumber_result.assignments.keys()):
            assignment = renumber_result.assignments[num]
            if assignment.is_new and assignment.candidate:
                entry = self._format_bib_entry(assignment.candidate, num, style)
            else:
                # Re-use existing entry text with updated number
                old_entry = existing.bib_entries.get(assignment.original_number)
                if old_entry:
                    # The parser stored the entry body (text after the number);
                    # for older saved projects fall back to stripping the
                    # number prefix, handling "1.", "1)" and "[1]" formats.
                    body = old_entry.body or re.sub(
                        r'^\[?\d+[.\s)\]]*\s*', '', old_entry.raw_text, count=1)
                    entry = f"{num}. {body}"
                else:
                    entry = f"{num}. [Missing reference]"
            entries.append(entry)
        return entries

    def _format_bib_entry(self, citation, number, style: CitationStyle):
        """Format a single bibliography entry (delegates to pipeline)."""
        return format_bib_entry(citation, number, style)

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
                    self._analyze_document(docx_path)
                    self._mark_dirty()
                    message += " — document changed since the project was saved; re-analysed"
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
