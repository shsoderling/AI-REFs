"""Main window for AI REFs application."""

import re
import logging
import hashlib
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
from ..pipeline.renumbering import compute_renumbering, NewMarkerInfo
from ..pipeline.export_slots import (
    ExportStats, collect_marker_slots, slot_for_docx_marker, action_for_unmatched,
    record_action, ACTION_CITE, ACTION_UNRESOLVED,
)
from ..models.markers import MarkerType as _MarkerKind
from ..utils.markers import find_markers
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

        # Compute hash for change detection
        with open(path, 'rb') as f:
            self._project.input_docx_hash = hashlib.sha256(f.read()).hexdigest()

        # Auto-detect insert mode (document with existing citations)
        try:
            handler = DocxHandler(path)
            parser = ExistingCitationParser(handler)
            existing = parser.analyze()
            if existing.has_existing_citations:
                self._project.is_insert_mode = True
                self._project.existing_citations = existing
                n = len(existing.bib_entries)
                self.inputs_tab.set_insert_mode(True, n)
                logger.info(f"Insert mode auto-detected: {n} existing references")
            else:
                self._project.is_insert_mode = False
                self._project.existing_citations = None
                self.inputs_tab.set_insert_mode(False, 0)
        except Exception as e:
            logger.warning(f"Insert mode detection failed: {e}")
            self._project.is_insert_mode = False
            self.inputs_tab.set_insert_mode(False, 0)

        self._mark_dirty()
        self.statusBar().showMessage(f"Loaded: {Path(path).name}")
        logger.info(f"Document loaded: {path}")

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
        settings = self._project.settings
        config = self._project.marker_config
        if not (config.detect_ids or config.detect_author_year):
            return True
        try:
            handler = DocxHandler(self._project.input_docx_path)
            stop_at = -1
            if self._project.is_insert_mode and self._project.existing_citations:
                stop_at = self._project.existing_citations.references_heading_para_idx
            n_search = n_ids = n_author_year = 0
            for idx, para in enumerate(handler.get_paragraphs()):
                if stop_at >= 0 and idx >= stop_at:
                    break
                text = para.text.strip()
                # Same heading rule as DocumentParser: headings are not processed
                if len(text) < 80 and not text.endswith('.') and para.style.name.startswith('Heading'):
                    continue
                for m in find_markers(text, config):
                    if m.kind != _MarkerKind.SUGGESTED:
                        n_search += 1
                    elif any(s.kind.value == "author_year" for s in m.suggestions):
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
            # This run only; the Input tab checkboxes keep the user's preference.
            settings.detect_suggested_ids = False
            settings.detect_author_year = False
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

        try:
            stats = self._do_export(output_path)
            self.statusBar().showMessage(f"Exported: {output_path}")
            QMessageBox.information(self, "Export Complete",
                                  f"Document exported to:\n{output_path}\n\n{stats.summary()}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Export failed:\n{e}")
            logger.error(f"Export error: {e}")

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

    def _do_export(self, output_path: str) -> ExportStats:
        """Route to the appropriate export path based on mode."""
        if self._project.is_insert_mode and self._project.existing_citations:
            return self._do_insert_export(output_path)
        return self._do_fresh_export(output_path)

    def _do_fresh_export(self, output_path: str) -> ExportStats:
        """Export a fresh document (no pre-existing citations).

        Matches DOCX markers to sentence evidence using paragraph_index
        (structural matching) instead of text-content heuristics.  Within a
        paragraph that contains multiple markers, markers are matched to
        sentences in document order.  Each marker owns its own block of the
        sentence's selected citations (see ``export_slots``).
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
        current_num = 1

        slots_by_para = collect_marker_slots(self._project)
        markers = handler.find_markers(self._project.export_marker_config)
        para_marker_counter: dict[int, int] = defaultdict(int)
        same_text_counter: dict[tuple[int, str], int] = defaultdict(int)
        stats = ExportStats()

        total_expanded = sum(len(v) for v in slots_by_para.values())
        logger.info(f"Export: {len(markers)} DOCX markers, "
                     f"{total_expanded} expanded sentence-marker slots")

        # Pass 1 (document order): decide every replacement and number the
        # bibliography.  Pass 2 applies them right-to-left within each
        # paragraph so an inserted citation can never be mistaken for a
        # later marker's text (author-date styles can produce identical text).
        plan: list[tuple] = []  # (para, para_idx, marker_text, occurrence, replacement, superscript)

        for marker_info in markers:
            para = marker_info['paragraph']
            para_idx = marker_info['para_index']
            marker_text = marker_info['text']

            marker_order = para_marker_counter[para_idx]
            para_marker_counter[para_idx] += 1
            occurrence = same_text_counter[(para_idx, marker_text)]
            same_text_counter[(para_idx, marker_text)] += 1

            slot = slot_for_docx_marker(slots_by_para, para_idx, marker_order, marker_text)
            action = slot.action if slot else action_for_unmatched(marker_info['marker_type'])
            record_action(stats, slot, action)

            sent_id = slot.sentence.id if slot else "???"
            if slot and slot.citations:
                ref_titles = "; ".join(s.title[:40] for s in slot.citations)
                logger.info(f"  Marker para={para_idx}[{marker_order}] {marker_text!r} -> {sent_id} "
                            f"action={action} refs=[{ref_titles}]")
            else:
                logger.info(f"  Marker para={para_idx}[{marker_order}] {marker_text!r} -> {sent_id} "
                            f"action={action} (no citations)")

            if action == ACTION_CITE:
                citation_parts = []
                for sel in slot.citations:
                    bib_key = sel.pmid or sel.doi or sel.title[:30]
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
                plan.append((para, para_idx, marker_text, occurrence, replacement, use_superscript))
            elif action == ACTION_UNRESOLVED:
                plan.append((para, para_idx, marker_text, occurrence, "[?]", False))
            # ACTION_LEAVE: nothing to apply

        self._apply_replacement_plan(handler, plan)

        if bib_entries:
            handler.append_bibliography(bib_entries)

        handler.save(output_path)
        self._project.output_docx_path = output_path
        logger.info(f"Exported document with {len(bib_entries)} bibliography entries: {output_path}")
        return stats

    @staticmethod
    def _apply_replacement_plan(handler: DocxHandler, plan: list[tuple]):
        """Apply (para, para_idx, text, occurrence, replacement, superscript) entries.

        Entries are grouped by paragraph and applied from the last marker to
        the first, so every ``occurrence`` index computed on the original text
        stays valid while earlier text is still untouched.
        """
        by_para: dict[int, list[tuple]] = defaultdict(list)
        for entry in plan:
            by_para[entry[1]].append(entry)
        for para_idx in sorted(by_para):
            for para, _idx, marker_text, occurrence, replacement, superscript in reversed(by_para[para_idx]):
                handler.replace_marker_by_regex(
                    para, marker_text, replacement,
                    superscript=superscript, occurrence=occurrence,
                )

    # ── Insert-mode export ────────────────────────────────────────────

    def _do_insert_export(self, output_path: str) -> ExportStats:
        """Export a document in insert mode: resolve new markers + renumber.

        Steps:
        1. Match new markers to their resolved citations
        2. Compute merged renumbering across existing + new citations
        3. Renumber all existing in-text citation numbers (before inserting new ones)
        4. Replace new markers with assigned citation numbers
        5. Remove old References section and append merged bibliography
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

        logger.info(f"Insert-mode export: style={style.value}  "
                     f"existing_refs={len(existing.bib_entries)}  "
                     f"superscript={use_superscript}")

        refs_start = existing.references_heading_para_idx

        # ── Step 1: Build new marker info from DOCX markers + evidence ──
        # Markers inside the existing References section are never processed;
        # that section is rebuilt in step 5.
        markers = [
            m for m in handler.find_markers(self._project.export_marker_config)
            if refs_start < 0 or m['para_index'] < refs_start
        ]

        slots_by_para = collect_marker_slots(self._project)
        para_marker_counter: dict[int, int] = defaultdict(int)
        same_text_counter: dict[tuple[int, str], int] = defaultdict(int)
        stats = ExportStats()

        # Per DOCX marker: (marker_info, action, resolved citations, occurrence)
        new_marker_infos = []
        marker_plan: list[tuple] = []

        for marker_info in markers:
            para_idx = marker_info['para_index']
            char_offset = marker_info['location'][0]
            marker_text = marker_info['text']

            marker_order = para_marker_counter[para_idx]
            para_marker_counter[para_idx] += 1
            occurrence = same_text_counter[(para_idx, marker_text)]
            same_text_counter[(para_idx, marker_text)] += 1

            slot = slot_for_docx_marker(slots_by_para, para_idx, marker_order, marker_text)
            action = slot.action if slot else action_for_unmatched(marker_info['marker_type'])
            record_action(stats, slot, action)
            resolved_citations = list(slot.citations) if (slot and action == ACTION_CITE) else []

            new_marker_infos.append(NewMarkerInfo(
                para_index=para_idx,
                char_offset=char_offset,
                citations=resolved_citations,
            ))
            marker_plan.append((marker_info, action, resolved_citations, occurrence))

        # ── Step 2: Compute renumbering ──
        renumber_result = compute_renumbering(existing, new_marker_infos)
        renumber_map = renumber_result.renumber_map

        logger.info(f"Renumbering: {len(renumber_result.assignments)} total citations, "
                     f"renumber_map has {sum(1 for o, n in renumber_map.items() if o != n)} changes")

        # ── Step 3: Renumber existing in-text citations ──
        # Must happen BEFORE replacing new markers, otherwise the newly-inserted
        # superscript numbers would be caught and double-renumbered.
        if not is_author_date and any(o != n for o, n in renumber_map.items()):
            self._renumber_existing_citations(handler, existing, renumber_map)

        # ── Step 4: Replace new markers (right-to-left within each paragraph) ──
        plan: list[tuple] = []
        for marker_info, action, resolved, occurrence in marker_plan:
            para = marker_info['paragraph']
            para_idx = marker_info['para_index']
            marker_text = marker_info['text']

            if action == ACTION_CITE and resolved and not is_author_date:
                citation_numbers = []
                for cand in resolved:
                    bib_key = cand.pmid or cand.doi or cand.title[:30]
                    for num, assn in renumber_result.assignments.items():
                        if assn.bib_key == bib_key:
                            citation_numbers.append(num)
                            break

                inner = cite_delim.join(str(n) for n in citation_numbers)
                replacement = f"{cite_prefix}{inner}{cite_suffix}"
                plan.append((para, para_idx, marker_text, occurrence, replacement, use_superscript))
            elif action == ACTION_CITE and resolved and is_author_date:
                parts = [cand.first_author_year for cand in resolved]
                replacement = f"{cite_prefix}{cite_delim.join(parts)}{cite_suffix}"
                plan.append((para, para_idx, marker_text, occurrence, replacement, False))
            elif action == ACTION_UNRESOLVED:
                plan.append((para, para_idx, marker_text, occurrence, "[?]", False))
        self._apply_replacement_plan(handler, plan)

        # ── Step 5: Remove old References section, build merged bibliography ──
        handler.remove_references_section(existing.references_heading_para_idx)
        merged_bib = self._build_merged_bibliography(existing, renumber_result, style)
        if merged_bib:
            handler.append_bibliography(merged_bib)

        handler.save(output_path)
        self._project.output_docx_path = output_path
        logger.info(f"Insert-mode export complete: {len(merged_bib)} bibliography entries: {output_path}")
        return stats

    def _renumber_existing_citations(self, handler: DocxHandler,
                                      existing, renumber_map: dict[int, int]):
        """Update all existing in-text citation numbers using the renumber_map.

        Walks superscript runs in body paragraphs (before the References heading)
        and replaces each citation number according to the map.

        Uses a single-pass re.sub with a callback to avoid cascading collisions
        (e.g. 7->10 then a later run containing 10 being re-mapped).
        """
        refs_start = existing.references_heading_para_idx
        cite_runs = handler.find_superscript_citation_runs()

        # Filter to only body paragraphs
        body_runs = [r for r in cite_runs if r['para_index'] < refs_start]

        # Single-pass: re.sub replaces each number via callback — no collisions
        def _replace_num(m):
            old_num = int(m.group())
            return str(renumber_map.get(old_num, old_num))

        for run_info in body_runs:
            run = run_info['run']
            text = run.text
            new_text = re.sub(r'\d+', _replace_num, text)
            if new_text != text:
                run.text = new_text

        if body_runs:
            logger.info(f"Renumbered superscript citations in {len(body_runs)} runs")

        # Also renumber bracketed numeric citations (e.g. [1], [2-4], [1, 3]).
        # We replace each bracket token by exact text match so surrounding
        # paragraph formatting is preserved.
        bracket_pattern = re.compile(r'\[(\d+(?:\s*[,;\-\u2013]\s*\d+)*)\]')
        bracket_updates = 0
        for para_idx, para in enumerate(handler.get_paragraphs()):
            if para_idx >= refs_start:
                break

            para_text = para.text
            if "[" not in para_text:
                continue

            replacements: list[tuple[str, str]] = []
            for match in bracket_pattern.finditer(para_text):
                old_token = match.group(0)
                inner = match.group(1)
                new_inner = self._renumber_bracket_group(inner, renumber_map)
                new_token = f"[{new_inner}]"
                if new_token != old_token:
                    replacements.append((old_token, new_token))

            for old_token, new_token in replacements:
                handler.replace_marker_by_regex(
                    para, old_token, new_token, superscript=False,
                )
                bracket_updates += 1

        if bracket_updates:
            logger.info(f"Renumbered bracket citations in {bracket_updates} locations")

    @staticmethod
    def _renumber_bracket_group(group_text: str, renumber_map: dict[int, int]) -> str:
        """Renumber and normalize a bracket citation group like '1, 3-5'."""
        numbers = MainWindow._expand_bracket_numbers(group_text)
        if not numbers:
            return group_text
        mapped = [renumber_map.get(n, n) for n in numbers]

        # De-duplicate while preserving first occurrence order.
        seen = set()
        ordered = []
        for n in mapped:
            if n not in seen:
                seen.add(n)
                ordered.append(n)

        return MainWindow._format_bracket_numbers(ordered)

    @staticmethod
    def _expand_bracket_numbers(group_text: str) -> list[int]:
        """Expand citation list/range text into explicit numbers."""
        numbers: list[int] = []
        for part in re.split(r'[;,]\s*', group_text):
            token = part.strip()
            if not token:
                continue

            bounds = re.split(r'\s*[-\u2013]\s*', token)
            if len(bounds) == 2 and bounds[0].isdigit() and bounds[1].isdigit():
                start = int(bounds[0])
                end = int(bounds[1])
                if start <= end:
                    numbers.extend(range(start, end + 1))
                else:
                    numbers.extend(range(start, end - 1, -1))
            elif token.isdigit():
                numbers.append(int(token))

        return numbers

    @staticmethod
    def _format_bracket_numbers(numbers: list[int]) -> str:
        """Format a list of citation numbers as compact ranges."""
        if not numbers:
            return ""

        chunks = []
        start = prev = numbers[0]
        for n in numbers[1:]:
            if n == prev + 1:
                prev = n
                continue

            if start == prev:
                chunks.append(str(start))
            elif prev - start >= 2:
                chunks.append(f"{start}-{prev}")
            else:
                chunks.extend([str(start), str(prev)])
            start = prev = n

        if start == prev:
            chunks.append(str(start))
        elif prev - start >= 2:
            chunks.append(f"{start}-{prev}")
        else:
            chunks.extend([str(start), str(prev)])

        return ", ".join(chunks)

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
                    # Replace the leading number prefix
                    entry = re.sub(r'^\d+', str(num), old_entry.raw_text, count=1)
                else:
                    entry = f"{num}. [Missing reference]"
            entries.append(entry)
        return entries

    def _format_bib_entry(self, citation, number, style: CitationStyle):
        """Format a single bibliography entry.

        Uses a generic NLM-like format that works well for most styles.
        The CSL file determines in-text citation formatting; this method
        handles the bibliography list.
        """
        authors = ', '.join(a.display_name for a in citation.authors[:6])
        if len(citation.authors) > 6:
            authors += ' et al.'

        base = f"{authors}. {citation.title}"
        if not base.endswith('.'):
            base += '.'
        base += f" {citation.journal_abbrev or citation.journal}."
        if citation.year:
            base += f" {citation.year}"
        if citation.volume:
            base += f";{citation.volume}"
            if citation.issue:
                base += f"({citation.issue})"
        if citation.pages:
            base += f":{citation.pages}"
        base += "."
        if citation.doi:
            base += f" doi:{citation.doi}"

        # Add PMID / PMCID for NIH grant style (public access policy)
        if style == CitationStyle.NIH_GRANT:
            if citation.pmid:
                base += f" PMID: {citation.pmid}"
            if citation.pmcid:
                base += f"{';' if citation.pmid else ''} PMCID: {citation.pmcid}"

        # Numeric styles get a number prefix
        if style not in AUTHOR_DATE_STYLES:
            return f"{number}. {base}"
        return base

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
            if self._project.input_docx_path:
                self.inputs_tab.drop_zone.set_file(self._project.input_docx_path)
            if self._project.is_insert_mode and self._project.existing_citations:
                self.inputs_tab.set_insert_mode(
                    True, len(self._project.existing_citations.bib_entries),
                )
            else:
                self.inputs_tab.set_insert_mode(False, 0)
            self._update_title()
            self.statusBar().showMessage(f"Opened: {Path(path).name}")
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
        event.accept()

    def keyPressEvent(self, event):
        """Handle keyboard shortcuts."""
        if event.key() == Qt.Key.Key_Return and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            # Ctrl+Enter: Accept current in review tab
            if self.tabs.currentIndex() == 3:
                self.review_tab._on_accept()
        super().keyPressEvent(event)
