"""Review tab: interactive review of citation assignments."""

import html
import logging
from typing import Optional
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QTextEdit, QGroupBox,
    QSplitter, QFrame, QComboBox, QLineEdit, QScrollArea,
    QMessageBox
)
from PySide6.QtCore import Signal, Slot, Qt, QThread, QTimer, QUrl
from PySide6.QtGui import QColor, QFont, QDesktopServices

from ..models.embedded import DocumentTier
from ..models.project import ProjectState
from ..models.sentence import SentenceRecord, MarkerType
from ..pipeline.claim_context import build_claim_context
from ..models.evidence import (
    EvidenceRecord, ConfidenceLevel, ReviewDecision, VerificationStatus, CitationVerdict, Verdict,
)
from ..models.citation import (
    CitationCandidate, is_valid_citation, make_placeholder_citation,
)
from ..services.pubmed_client import PubMedClient
from ..services.europepmc_client import EuropePMCClient
from ..services.biorxiv_client import BioRxivClient
from ..services.ref_library import ReferenceLibrary
from ..storage.cache_db import CacheDB
from .styles import (
    COLOR_HIGH, COLOR_MEDIUM, COLOR_LOW, COLOR_UNRESOLVED, COLOR_ACCEPTED, COLOR_SKIPPED,
)
from .widgets.chat_panel import ChatPanel

logger = logging.getLogger(__name__)


def _came_from_a_document(citation) -> bool:
    """True when this record was read back out of a document.

    Such a record is thinner than a fresh search result (no abstract, no MeSH
    terms), so the library merges it rather than overwriting the row.  The
    record uuid used to be the only signal; since candidates for one paper are
    now shared across its citations, a copy can reach here without one, so the
    record's own origin is checked too.
    """
    return bool(getattr(citation, "record_uuid", "")) or \
        (getattr(citation, "source", "") or "").lower() in ("embedded", "document")


def _follow_wrapped_height(widget: QWidget):
    """Keep a widget's minimum height equal to what its word-wrapped content
    needs at the current width.

    A QScrollArea sizes its content widget from minimum size hints, and the
    minimum hint of word-wrapped labels is a single line; without this the
    reference cards overlap when the panel is narrow. The check runs after
    the pending layout pass, when the labels know their new width."""
    def sync():
        try:
            height = widget.sizeHint().height()
        except RuntimeError:            # widget already deleted
            return
        if height > 0 and height != widget.minimumHeight():
            widget.setMinimumHeight(height)
    QTimer.singleShot(0, sync)


class WrappedLabel(QLabel):
    """A word-wrapping label whose minimum height follows its wrapped text."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        _follow_wrapped_height(self)


class SingleRefWidget(QFrame):
    """Widget for reviewing one reference of a sentence: (REF) sentences show
    one, (REFS) and multi-marker sentences show one per reference.

    Shows citation info with individual Keep / View / Replace controls, plus
    Remove when the sentence has more than one slot.  ``slot`` is the marker
    the reference belongs to (a sentence handled marker by marker keeps one
    block of references per marker); ``slot_label`` is that marker's text,
    shown when the sentence has several markers.
    """
    ref_accepted = Signal(int)              # index of accepted ref
    ref_replace_requested = Signal(int, str)  # index, PMID/DOI string
    ref_chat_requested = Signal(int)         # index — open chat panel for this ref
    ref_removed = Signal(int)               # index of removed ref

    ABSTRACT_EXCERPT_CHARS = 220

    def __init__(self, index: int, citation: CitationCandidate, accepted: bool = False,
                 parent=None, allow_remove: bool = True,
                 verdict: Optional[CitationVerdict] = None,
                 slot: int = 0, slot_label: str = ""):
        super().__init__(parent)
        self.index = index
        self.citation = citation
        self.verdict = verdict
        self.slot = slot
        self.slot_label = slot_label
        self._accepted = accepted
        self._removed = False
        self._allow_remove = allow_remove
        self._setup_ui()

    def _setup_ui(self):
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Raised)
        self.setLineWidth(1)
        self._update_frame_style()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        # Row 1: Reference number (+ marker it belongs to) + title
        slot_html = ""
        if self.slot_label:
            slot_html = (
                f'<span style="color: #856404; background-color: #fff3cd; '
                f'padding: 0 4px; border-radius: 3px;">{html.escape(self.slot_label)}</span> '
            )
        title_label = QLabel(f"<b>[{self.index + 1}]</b> {slot_html}{html.escape(self.citation.title)}")
        title_label.setWordWrap(True)
        title_label.setStyleSheet("font-size: 12px;")
        layout.addWidget(title_label)

        # Row 2: Author, journal, year
        meta = f"{self.citation.first_author_year} | {self.citation.journal_abbrev or self.citation.journal}"
        if self.citation.pmid:
            meta += f" | PMID: {self.citation.pmid}"
        if self.citation.pmcid:
            meta += f" | PMCID: {self.citation.pmcid}"
        if self.citation.doi:
            meta += f" | DOI: {self.citation.doi}"
        meta_label = QLabel(meta)
        meta_label.setWordWrap(True)
        meta_label.setStyleSheet("font-size: 11px; color: #555;")
        layout.addWidget(meta_label)

        # Row 3: Score
        score_label = QLabel(f"Score: {self.citation.composite_score:.0f} | {self.citation.score_rationale}")
        score_label.setWordWrap(True)
        score_label.setStyleSheet("font-size: 11px; color: #777;")
        layout.addWidget(score_label)

        # Row 3b: Abstract excerpt (when the record carries one)
        self.abstract_label = QLabel("")
        self.abstract_label.setWordWrap(True)
        self.abstract_label.setStyleSheet("font-size: 11px; color: #666; font-style: italic;")
        layout.addWidget(self.abstract_label)
        self._set_abstract(self.citation.abstract)

        # Row 3c: Independent verification verdict
        self.verdict_label = QLabel("")
        self.verdict_label.setWordWrap(True)
        layout.addWidget(self.verdict_label)
        self._set_verdict(self.verdict)

        # Row 4: Action buttons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(6)

        self.accept_btn = QPushButton("✓ Keep")
        self.accept_btn.setStyleSheet(
            "background-color: #27ae60; color: white; padding: 4px 12px; "
            "border-radius: 3px; font-size: 11px; font-weight: bold;"
        )
        self.accept_btn.clicked.connect(self._on_accept)
        # A placeholder ("No citation found") cannot be kept: replace it instead
        self.accept_btn.setEnabled(is_valid_citation(self.citation))
        btn_layout.addWidget(self.accept_btn)

        self.view_btn = QPushButton("👁 View It")
        self.view_btn.setStyleSheet(
            "background-color: #3498db; color: white; padding: 4px 12px; "
            "border-radius: 3px; font-size: 11px; font-weight: bold;"
        )
        self.view_btn.clicked.connect(self._on_view)
        self.view_btn.setEnabled(bool(self.citation.doi or self.citation.pmid))
        btn_layout.addWidget(self.view_btn)

        self.replace_btn = QPushButton("Replace")
        self.replace_btn.setStyleSheet(
            "background-color: #e67e22; color: white; padding: 4px 12px; "
            "border-radius: 3px; font-size: 11px; font-weight: bold;"
        )
        self.replace_btn.clicked.connect(self._on_replace_clicked)
        btn_layout.addWidget(self.replace_btn)

        self.remove_btn = QPushButton("✕ Remove")
        self.remove_btn.setStyleSheet(
            "background-color: #c0392b; color: white; padding: 4px 12px; "
            "border-radius: 3px; font-size: 11px; font-weight: bold;"
        )
        self.remove_btn.clicked.connect(self._on_remove)
        self.remove_btn.setVisible(self._allow_remove)
        btn_layout.addWidget(self.remove_btn)

        self.pmid_input = QLineEdit()
        self.pmid_input.setPlaceholderText("PMID / DOI / URL")
        self.pmid_input.setMaximumWidth(220)
        self.pmid_input.setStyleSheet("font-size: 11px;")
        self.pmid_input.setVisible(False)
        btn_layout.addWidget(self.pmid_input)

        self.fetch_btn = QPushButton("Fetch")
        self.fetch_btn.setStyleSheet(
            "background-color: #3498db; color: white; padding: 4px 10px; "
            "border-radius: 3px; font-size: 11px;"
        )
        self.fetch_btn.setVisible(False)
        self.fetch_btn.clicked.connect(self._on_fetch)
        btn_layout.addWidget(self.fetch_btn)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("font-size: 11px; font-weight: bold;")
        btn_layout.addWidget(self.status_label)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # Apply initial state
        if self._accepted:
            self._mark_accepted()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        _follow_wrapped_height(self)

    _VERDICT_COLORS = {
        Verdict.SUPPORTS: COLOR_HIGH,
        Verdict.PARTIAL: COLOR_MEDIUM,
        Verdict.NOT_SUPPORTED: COLOR_LOW,
        Verdict.UNVERIFIED: COLOR_UNRESOLVED,
    }

    def _set_verdict(self, verdict: Optional[CitationVerdict]):
        self.verdict = verdict
        if verdict is None:
            self.verdict_label.setText("")
            self.verdict_label.setVisible(False)
            return
        label = verdict.verdict.value.replace("_", " ")
        text = f"Verification: {label}"
        if verdict.quote:
            quote = verdict.quote[:140].rstrip() + ("…" if len(verdict.quote) > 140 else "")
            text += f" — “{quote}”"
            if not verdict.quote_found:
                text += " (quote not found in the paper)"
        if verdict.source:
            text += f" ({verdict.source.replace('_', ' ')})"
        elif verdict.reason:
            text += f" — {verdict.reason}"
        color = self._VERDICT_COLORS.get(verdict.verdict, COLOR_UNRESOLVED)
        self.verdict_label.setText(text)
        self.verdict_label.setStyleSheet(f"font-size: 11px; color: {color}; font-weight: bold;")
        self.verdict_label.setVisible(True)

    def _set_abstract(self, abstract: str):
        text = (abstract or "").strip()
        if text:
            excerpt = text[:self.ABSTRACT_EXCERPT_CHARS].rstrip()
            if len(text) > self.ABSTRACT_EXCERPT_CHARS:
                excerpt += "…"
            self.abstract_label.setText(f"Abstract: {excerpt}")
            self.abstract_label.setVisible(True)
        else:
            self.abstract_label.setText("")
            self.abstract_label.setVisible(False)

    def _update_frame_style(self):
        if self._removed:
            self.setStyleSheet(
                "SingleRefWidget { background-color: #fdecea; border: 1px solid #c0392b; "
                "border-radius: 4px; opacity: 0.7; }"
            )
        elif self._accepted:
            self.setStyleSheet(
                "SingleRefWidget { background-color: #eafaf1; border: 1px solid #27ae60; "
                "border-radius: 4px; }"
            )
        else:
            self.setStyleSheet(
                "SingleRefWidget { background-color: #fefefe; border: 1px solid #ddd; "
                "border-radius: 4px; }"
            )

    def _on_accept(self):
        self._accepted = True
        self._mark_accepted()
        self.ref_accepted.emit(self.index)

    def _on_view(self):
        """Open the article in the default browser."""
        if self.citation.doi:
            url = f"https://doi.org/{self.citation.doi}"
        elif self.citation.pmid:
            url = f"https://pubmed.ncbi.nlm.nih.gov/{self.citation.pmid}"
        else:
            return
        QDesktopServices.openUrl(QUrl(url))

    def _mark_accepted(self):
        self._accepted = True
        self._update_frame_style()
        self.accept_btn.setEnabled(False)
        self.accept_btn.setText("✓ Kept")
        self.replace_btn.setVisible(False)
        self.remove_btn.setVisible(False)
        self.pmid_input.setVisible(False)
        self.fetch_btn.setVisible(False)
        self.status_label.setText("✓ Accepted")
        self.status_label.setStyleSheet("font-size: 11px; font-weight: bold; color: #27ae60;")

    def _on_remove(self):
        self._removed = True
        self._accepted = False
        self._update_frame_style()
        self.accept_btn.setEnabled(False)
        self.accept_btn.setVisible(False)
        self.replace_btn.setVisible(False)
        self.remove_btn.setEnabled(False)
        self.remove_btn.setText("✕ Removed")
        self.pmid_input.setVisible(False)
        self.fetch_btn.setVisible(False)
        self.status_label.setText("✕ Removed")
        self.status_label.setStyleSheet("font-size: 11px; font-weight: bold; color: #c0392b;")
        self.ref_removed.emit(self.index)

    def _on_replace_clicked(self):
        """Request the chat panel to open for this reference."""
        self.ref_chat_requested.emit(self.index)

    def _on_fetch(self):
        identifier = self.pmid_input.text().strip()
        if not identifier:
            return
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("Fetching...")
        self.replace_btn.setEnabled(False)
        self.ref_replace_requested.emit(self.index, identifier)

    def mark_replaced(self, new_citation: CitationCandidate):
        """Called after successful fetch — update display for the replaced ref."""
        self.citation = new_citation
        self._accepted = True
        self._set_abstract(new_citation.abstract)
        self._set_verdict(None)          # a user-chosen paper has not been verified
        self._update_frame_style()
        self.accept_btn.setEnabled(False)
        self.accept_btn.setText("✓ Replaced")
        self.replace_btn.setVisible(False)
        self.remove_btn.setVisible(False)
        self.pmid_input.setVisible(False)
        self.fetch_btn.setVisible(False)
        self.view_btn.setEnabled(bool(new_citation.doi or new_citation.pmid))
        self.status_label.setText(f"✓ Replaced → {new_citation.title[:50]}...")
        self.status_label.setStyleSheet("font-size: 11px; font-weight: bold; color: #2980b9;")

    def mark_fetch_error(self, msg: str):
        """Called on fetch failure — re-enable controls."""
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("Fetch")
        self.replace_btn.setEnabled(True)
        self.status_label.setText(f"Error: {msg[:40]}")
        self.status_label.setStyleSheet("font-size: 11px; font-weight: bold; color: #e74c3c;")


class PMIDFetchWorker(QThread):
    """Background worker to fetch an article by PMID, DOI, or DOI URL."""
    finished = Signal(object)   # CitationCandidate or None
    error = Signal(str)         # error message

    def __init__(self, identifier: str, email: str, api_key: str = "", parent=None):
        super().__init__(parent)
        self.identifier = identifier.strip()
        self.email = email
        self.api_key = api_key

    @staticmethod
    def _extract_doi(text: str) -> str:
        """Extract a DOI from a URL or raw DOI string."""
        text = text.strip()
        # Handle DOI URLs: https://doi.org/10.xxx, http://dx.doi.org/10.xxx
        import re
        url_match = re.match(r'https?://(?:dx\.)?doi\.org/(.+)', text)
        if url_match:
            return url_match.group(1)
        # Raw DOI starting with 10.
        if text.startswith("10."):
            return text
        return ""

    def run(self):
        try:
            cache = CacheDB()
            client = PubMedClient(
                email=self.email,
                api_key=self.api_key,
                cache_db=cache,
            )

            doi = self._extract_doi(self.identifier)

            if doi:
                # It's a DOI — try PubMed first, then bioRxiv
                # Search PubMed by DOI
                pmids, _ = client.search(f"{doi}[doi]", max_results=1)
                if pmids:
                    article = client.fetch_article(pmids[0])
                    if article:
                        self.finished.emit(article)
                        return

                # Not in PubMed — try bioRxiv/medRxiv
                from ..services.biorxiv_client import BioRxivClient
                for server in ("biorxiv", "medrxiv"):
                    try:
                        biorxiv = BioRxivClient(cache_db=cache, server=server)
                        preprint = biorxiv.fetch_preprint(doi)
                        if preprint:
                            self.finished.emit(preprint)
                            return
                    except Exception:
                        pass

                self.error.emit(
                    f"DOI '{doi}' not found in PubMed, bioRxiv, or medRxiv.\n"
                    "Check that the DOI is correct."
                )
            else:
                # Treat as PMID
                article = client.fetch_article(self.identifier)
                if article:
                    self.finished.emit(article)
                else:
                    self.error.emit(f"PMID {self.identifier} not found on PubMed.")
        except Exception as e:
            self.error.emit(str(e))


class SentenceListItem(QListWidgetItem):
    """Custom list item for sentences with color-coded confidence."""

    def __init__(self, sentence: SentenceRecord, evidence: EvidenceRecord = None):
        super().__init__()
        self.sentence_id = sentence.id
        self.sentence = sentence
        self.evidence = evidence

        # Build display text
        marker_label = sentence.marker_label()
        section = f"[{sentence.section}] " if sentence.section else ""
        display = f"{sentence.id} {marker_label} {section}{sentence.clean_text[:80]}..."
        self.setText(display)

        self._update_color()

    def _update_color(self):
        if self.evidence and self.evidence.review_decision in (ReviewDecision.ACCEPTED, ReviewDecision.MODIFIED):
            self.setForeground(QColor(COLOR_ACCEPTED))
            return
        if self.evidence and self.evidence.review_decision == ReviewDecision.SKIPPED:
            self.setForeground(QColor(COLOR_SKIPPED))
            return

        if self.evidence:
            color_map = {
                ConfidenceLevel.HIGH: COLOR_HIGH,
                ConfidenceLevel.MEDIUM: COLOR_MEDIUM,
                ConfidenceLevel.LOW: COLOR_LOW,
                ConfidenceLevel.UNRESOLVED: COLOR_UNRESOLVED,
            }
            color = color_map.get(self.evidence.confidence_level, COLOR_UNRESOLVED)
            self.setForeground(QColor(color))
        else:
            self.setForeground(QColor(COLOR_UNRESOLVED))

    def update_evidence(self, evidence: EvidenceRecord):
        self.evidence = evidence
        self._update_color()


class ReviewTab(QWidget):
    """Tab for reviewing and accepting/modifying citation assignments."""
    export_requested = Signal()
    preview_requested = Signal()  # Emitted to preview insert-mode final numbering
    project_modified = Signal()  # Emitted when a review decision changes
    library_updated = Signal()   # Emitted after refs are added to the active library

    def __init__(self, parent=None):
        super().__init__(parent)
        self._project = None
        self._current_sentence_id = None
        self._ref_library: Optional[ReferenceLibrary] = None
        self._per_ref_widgets: list[SingleRefWidget] = []
        self._per_ref_accepted: dict[int, bool] = {}  # index -> accepted
        self._per_ref_removed: dict[int, bool] = {}   # index -> removed
        self._chat_replace_index = None   # per-ref index when chat replaces one ref (int or None)
        self._fetch_worker = None
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Header + filter + export
        top_layout = QHBoxLayout()

        header = QLabel("Review Citations")
        header.setObjectName("sectionHeader")
        top_layout.addWidget(header)

        top_layout.addStretch()

        # Filter
        self.filter_combo = QComboBox()
        self.filter_combo.addItems([
            "Show All", "Low Confidence Only", "Unresolved Only", "Warnings Only",
            "Author-Suggested Only",
        ])
        self.filter_combo.currentIndexChanged.connect(self._apply_filter)
        top_layout.addWidget(QLabel("Filter:"))
        top_layout.addWidget(self.filter_combo)

        # Export + library actions
        self.export_btn = QPushButton("Export")
        self.export_btn.setObjectName("successButton")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(lambda _checked=False: self.export_requested.emit())

        self.add_to_lib_btn = QPushButton("Add New Ref to Lib")
        self.add_to_lib_btn.setEnabled(False)
        self.add_to_lib_btn.clicked.connect(self._on_add_new_refs_to_library)

        # Insert-mode only: preview the merged final numbering before export.
        self.preview_btn = QPushButton("Preview Numbering")
        self.preview_btn.setVisible(False)
        self.preview_btn.clicked.connect(
            lambda _checked=False: self.preview_requested.emit())

        top_actions = QVBoxLayout()
        top_actions.setSpacing(6)
        top_actions.addWidget(self.export_btn)
        top_actions.addWidget(self.preview_btn)
        top_actions.addWidget(self.add_to_lib_btn)
        top_layout.addLayout(top_actions)

        layout.addLayout(top_layout)

        # Status summary
        self.status_label = QLabel("No document loaded")
        self.status_label.setObjectName("subHeader")
        layout.addWidget(self.status_label)

        # Main splitter: sentence list (left) | detail panel (center) | chat panel (right)
        self.splitter = QSplitter(Qt.Horizontal)

        # Left: Sentence list
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.sentence_list = QListWidget()
        self.sentence_list.currentItemChanged.connect(self._on_sentence_selected)
        left_layout.addWidget(self.sentence_list)

        self.splitter.addWidget(left_widget)

        # Right: Detail panel
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(12)

        # Sentence text
        self.sentence_text = QTextEdit()
        self.sentence_text.setReadOnly(True)
        self.sentence_text.setMaximumHeight(80)
        self.sentence_text.setStyleSheet("background-color: #f8f9fa; border: 1px solid #ddd; border-radius: 4px; padding: 8px;")
        right_layout.addWidget(self.sentence_text)

        # Confidence badge
        self.confidence_label = QLabel("")
        self.confidence_label.setStyleSheet("font-size: 14px; font-weight: bold; padding: 4px 12px; border-radius: 4px;")
        right_layout.addWidget(self.confidence_label)

        # Selected references — container that switches between text view and per-ref widgets
        self.ref_group = QGroupBox("Selected References")
        self.ref_group_layout = QVBoxLayout(self.ref_group)

        # Text view for single-ref (REF) sentences
        self.ref_detail = QTextEdit()
        self.ref_detail.setReadOnly(True)
        self.ref_detail.setMinimumHeight(150)
        self.ref_group_layout.addWidget(self.ref_detail)

        # Scroll area for per-ref widgets (REFS sentences with multiple refs)
        self.per_ref_scroll = QScrollArea()
        self.per_ref_scroll.setWidgetResizable(True)
        self.per_ref_scroll.setMinimumHeight(150)
        self.per_ref_scroll.setStyleSheet("QScrollArea { border: none; }")
        self.per_ref_container = QWidget()
        self.per_ref_layout = QVBoxLayout(self.per_ref_container)
        self.per_ref_layout.setContentsMargins(0, 0, 0, 0)
        self.per_ref_layout.setSpacing(6)
        self.per_ref_layout.addStretch()
        self.per_ref_scroll.setWidget(self.per_ref_container)
        self.per_ref_scroll.setVisible(False)
        self.ref_group_layout.addWidget(self.per_ref_scroll)

        right_layout.addWidget(self.ref_group)

        # Warnings
        self.warnings_label = QLabel("")
        self.warnings_label.setWordWrap(True)
        self.warnings_label.setStyleSheet("color: #e67e22; font-size: 12px;")
        right_layout.addWidget(self.warnings_label)

        # Action buttons
        action_layout = QHBoxLayout()

        self.accept_btn = QPushButton("Accept")
        self.accept_btn.setObjectName("successButton")
        self.accept_btn.clicked.connect(self._on_accept)
        self.accept_btn.setEnabled(False)
        action_layout.addWidget(self.accept_btn)

        self.modify_btn = QPushButton("Modify / Search")
        self.modify_btn.clicked.connect(self._on_modify)
        self.modify_btn.setEnabled(False)
        action_layout.addWidget(self.modify_btn)

        self.skip_btn = QPushButton("Leave unchanged")
        self.skip_btn.setToolTip(
            "Keep this sentence's marker(s) exactly as written on export (for example "
            "when a parenthetical is not really a citation, or you want to handle it yourself)"
        )
        self.skip_btn.clicked.connect(self._on_skip)
        self.skip_btn.setEnabled(False)
        action_layout.addWidget(self.skip_btn)

        self.pmid_edit = QLineEdit()
        self.pmid_edit.setPlaceholderText("PMID, DOI, or https://doi.org/...")
        self.pmid_edit.setMaximumWidth(300)
        self.pmid_edit.setVisible(False)  # Replaced by chat panel
        action_layout.addWidget(self.pmid_edit)

        action_layout.addStretch()
        right_layout.addLayout(action_layout)

        right_layout.addStretch()
        self.splitter.addWidget(right_widget)

        # Chat panel (hidden by default, shown when Modify / Search is clicked)
        self.chat_panel = ChatPanel()
        self.chat_panel.setVisible(False)
        self.chat_panel.citation_selected.connect(self._on_chat_citation_selected)
        self.chat_panel.close_requested.connect(self._close_chat_panel)
        self.splitter.addWidget(self.chat_panel)

        self.splitter.setSizes([350, 550, 0])
        layout.addWidget(self.splitter, stretch=1)

    def load_project(self, project: ProjectState):
        """Load project data into the review tab."""
        self._configure_reference_library(project)
        self._project = project
        self.preview_btn.setVisible(bool(project and project.is_insert_mode))
        self._refresh_sentence_list()
        self._update_status()

    def _configure_reference_library(self, project: Optional[ProjectState]):
        """Open/close the active reference library for acceptance sync."""
        if self._ref_library:
            try:
                self._ref_library.close()
            except Exception:
                pass
            self._ref_library = None

        if not project:
            return

        library_path = (project.settings.reference_library_path or "").strip()
        if not library_path:
            return

        try:
            self._ref_library = ReferenceLibrary(library_path)
        except Exception as exc:
            self._ref_library = None
            logger.warning(f"Failed to open reference library '{library_path}': {exc}")

    def _sync_selected_to_library(self, evidence: EvidenceRecord):
        """Persist accepted/modified literature citations into the user library."""
        if not self._ref_library or not evidence.selected:
            return

        imported = 0
        updated = 0
        for citation in evidence.selected:
            if not citation.title and not citation.pmid and not citation.doi:
                continue
            source = (citation.source or "literature").lower()
            if source == "user_library":
                continue
            ins, upd = self._ref_library.upsert_candidate(
                citation, source=f"accepted_{source}",
                merge=_came_from_a_document(citation),   # never degrade a library row
            )
            if ins:
                imported += 1
                citation.source = "user_library"
            elif upd:
                updated += 1
                citation.source = "user_library"

        if imported or updated:
            logger.info(
                f"Synced to user library: +{imported} new, {updated} updated "
                f"({evidence.sentence_id})"
            )

    def _refresh_sentence_list(self):
        """Rebuild the sentence list."""
        self.sentence_list.clear()
        if not self._project:
            return

        for sentence in self._project.sentences:
            if sentence.marker_type is None:
                continue  # Only show sentences with markers

            evidence = self._project.evidence_map.get(sentence.id)
            item = SentenceListItem(sentence, evidence)
            self.sentence_list.addItem(item)

    def _update_status(self):
        """Update the status summary."""
        if not self._project:
            self.status_label.setText("No document loaded")
            self.export_btn.setEnabled(False)
            self.add_to_lib_btn.setEnabled(False)
            self.add_to_lib_btn.setText("Add New Ref to Lib")
            return

        total = self._project.total_markers
        resolved = self._project.resolved_count
        all_done = self._project.all_resolved

        self.status_label.setText(
            f"Resolved: {resolved}/{total} sentences"
            + (" — Ready to export!" if all_done and total > 0 else "")
        )

        # A tracked document can be exported with no new markers at all:
        # that renumbers it after edits made in Word.
        existing = self._project.existing_citations
        tracking = existing.tracking if existing is not None else None
        tracked = tracking is not None and tracking.tier == DocumentTier.TRACKED
        read_only = tracking is not None and (
            tracking.tier in (DocumentTier.FAILED, DocumentTier.NEWER_VERSION)
            or tracking.foreign_field_count > 0)
        exportable = ((all_done and total > 0) or (tracked and all_done)) and not read_only
        self.export_btn.setEnabled(exportable)
        if exportable:
            self.export_btn.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")
        else:
            self.export_btn.setStyleSheet("background-color: #ccc; color: #666;")

        addable = self._count_addable_library_refs()
        self.add_to_lib_btn.setEnabled(self._ref_library is not None and addable > 0)
        self.add_to_lib_btn.setText(f"Add New Ref to Lib ({addable})")

    def _count_addable_library_refs(self) -> int:
        """Count selected accepted/modified refs not yet marked as user-library."""
        if not self._project:
            return 0

        keys = set()
        for evidence in self._project.evidence_map.values():
            if evidence.review_decision not in (ReviewDecision.ACCEPTED, ReviewDecision.MODIFIED):
                continue
            for citation in evidence.selected:
                if not self._is_valid_citation(citation):
                    continue
                if (citation.source or "literature").lower() == "user_library":
                    continue
                keys.add(self._citation_key(citation))

        return len(keys)

    @staticmethod
    def _citation_key(citation: CitationCandidate) -> str:
        if citation.pmid:
            return f"pmid:{citation.pmid.strip()}"
        if citation.doi:
            return f"doi:{citation.doi.strip().lower()}"
        return f"title:{citation.title.strip().lower()}"

    @staticmethod
    def _is_valid_citation(citation: CitationCandidate) -> bool:
        return is_valid_citation(citation)

    def _get_current_sentence(self) -> Optional[SentenceRecord]:
        """The SentenceRecord for the currently selected sentence, if any."""
        return self._sentence_by_id(self._current_sentence_id)

    def _sentence_by_id(self, sentence_id: Optional[str]) -> Optional[SentenceRecord]:
        if not self._project or not sentence_id:
            return None
        for s in self._project.sentences:
            if s.id == sentence_id:
                return s
        return None

    @staticmethod
    def _n_slots(sentence: Optional[SentenceRecord]) -> int:
        """Citation slots of a sentence: one per marker when it was handled
        marker by marker, else one shared by every marker."""
        if sentence is None:
            return 1
        return max(sentence.slot_count, 1)

    @staticmethod
    def _has_full_style_fields(citation: CitationCandidate) -> bool:
        """All fields needed for robust bibliography output across all citation styles.

        Checks title, authors, journal, year, volume, pages, and at least one
        identifier (PMID or DOI).  These are used by _format_bib_entry for
        every supported CSL style.
        """
        return bool(
            citation.title
            and citation.authors
            and (citation.journal or citation.journal_abbrev)
            and citation.year
            and citation.volume
            and citation.pages
            and (citation.pmid or citation.doi)
        )

    @staticmethod
    def _merge_citation_fields(
        original: CitationCandidate, fetched: CitationCandidate
    ) -> CitationCandidate:
        """Fill in missing fields on *original* from *fetched* without overwriting
        data the original already has.  Returns a new CitationCandidate."""
        merged = original.model_copy()
        # Text / identifier fields — keep original when non-empty
        for field_name in (
            "pmid", "doi", "title", "journal", "journal_abbrev",
            "volume", "issue", "pages", "abstract",
        ):
            if not getattr(merged, field_name) and getattr(fetched, field_name, ""):
                setattr(merged, field_name, getattr(fetched, field_name))
        # Numeric / boolean fields
        if not merged.year and fetched.year:
            merged.year = fetched.year
        # Authors — only replace if original has none
        if not merged.authors and fetched.authors:
            merged.authors = fetched.authors
        # List fields — extend
        if not merged.mesh_terms and fetched.mesh_terms:
            merged.mesh_terms = fetched.mesh_terms
        if not merged.publication_types and fetched.publication_types:
            merged.publication_types = fetched.publication_types
        if fetched.is_retracted:
            merged.is_retracted = True
        if fetched.is_review:
            merged.is_review = True
        return merged

    def _enrich_citation_for_library(self, citation: CitationCandidate) -> CitationCandidate:
        """Backfill missing fields via PubMed/EuropePMC/bioRxiv when needed.

        Tries multiple sources and merges any retrieved data into the original
        citation so that all bibliography-relevant fields (title, authors,
        journal, year, volume, issue, pages, DOI, PMID) are as complete as
        possible for every supported citation style.
        """
        if not self._project:
            return citation
        if self._has_full_style_fields(citation):
            return citation

        email = self._project.settings.ncbi_email or "user@example.com"
        api_key = self._project.settings.ncbi_api_key or ""
        cache = CacheDB()
        pubmed = PubMedClient(email=email, api_key=api_key, cache_db=cache)

        best = citation
        try:
            fetched = None
            if citation.pmid:
                fetched = pubmed.fetch_article(citation.pmid)

            if not fetched and citation.doi:
                pmids, _ = pubmed.search(f"{citation.doi}[doi]", max_results=1)
                if pmids:
                    fetched = pubmed.fetch_article(pmids[0])

            if fetched:
                best = self._merge_citation_fields(citation, fetched)
                if self._has_full_style_fields(best):
                    best.source = citation.source or best.source
                    return best

            # Try Europe PMC
            if citation.doi:
                europepmc = EuropePMCClient(cache_db=cache)
                fetched = europepmc.fetch_by_doi(citation.doi)
                if fetched:
                    best = self._merge_citation_fields(best, fetched)
                    if self._has_full_style_fields(best):
                        best.source = citation.source or best.source
                        return best

            # Try bioRxiv/medRxiv
            if citation.doi:
                for server in ("biorxiv", "medrxiv"):
                    try:
                        biorxiv = BioRxivClient(cache_db=cache, server=server)
                        fetched = biorxiv.fetch_preprint(citation.doi)
                    except Exception:
                        continue
                    if fetched:
                        best = self._merge_citation_fields(best, fetched)
                        break
        except Exception as exc:
            logger.debug(f"Citation enrichment failed for '{citation.title[:60]}': {exc}")

        best.source = citation.source or best.source
        return best

    def _on_add_new_refs_to_library(self):
        """Manual one-shot import of accepted non-library refs into active library."""
        if not self._project:
            return

        if not self._ref_library:
            QMessageBox.warning(
                self,
                "No Active Library",
                "No active reference library is configured. Set one in the REF Library tab first.",
            )
            return

        to_process = []
        seen = set()
        for evidence in self._project.evidence_map.values():
            if evidence.review_decision not in (ReviewDecision.ACCEPTED, ReviewDecision.MODIFIED):
                continue
            for citation in evidence.selected:
                if not self._is_valid_citation(citation):
                    continue
                if (citation.source or "literature").lower() == "user_library":
                    continue

                key = self._citation_key(citation)
                if key in seen:
                    continue
                seen.add(key)
                to_process.append(citation)

        if not to_process:
            QMessageBox.information(
                self,
                "Library Up To Date",
                "No new non-library references were found in accepted selections.",
            )
            self._update_status()
            return

        imported = 0
        already_present = 0
        metadata_incomplete = 0

        for citation in to_process:
            enriched = self._enrich_citation_for_library(citation)
            if not self._has_full_style_fields(enriched):
                metadata_incomplete += 1

            source = (citation.source or "literature").lower()
            ins, upd = self._ref_library.upsert_candidate(
                enriched,
                source=f"manual_add_{source}",
                merge=_came_from_a_document(citation),
            )
            if ins:
                imported += 1
            elif upd:
                already_present += 1

            citation.source = "user_library"

        self.project_modified.emit()
        self.library_updated.emit()
        self._update_status()

        total_processed = imported + already_present
        ref_word = "reference" if total_processed == 1 else "references"
        summary = f"Processed {total_processed} {ref_word}:\n"
        if imported:
            summary += f"  • {imported} newly added to the library\n"
        if already_present:
            summary += f"  • {already_present} already in the library (metadata refreshed)\n"
        if metadata_incomplete:
            inc_word = "reference" if metadata_incomplete == 1 else "references"
            summary += (
                f"\nNote: {metadata_incomplete} {inc_word} still "
                f"{'has' if metadata_incomplete == 1 else 'have'} partial metadata "
                "(e.g. missing volume/pages). Use PMID/DOI replacement in Review "
                "to force a full metadata lookup when needed."
            )
        QMessageBox.information(self, "Library Updated", summary)

    def _clear_per_ref_widgets(self):
        """Remove all per-reference widgets from the scroll area."""
        for w in self._per_ref_widgets:
            w.setParent(None)
            w.deleteLater()
        self._per_ref_widgets.clear()
        self._per_ref_accepted.clear()
        self._per_ref_removed.clear()

    def _ref_widgets(self) -> list[SingleRefWidget]:
        return [w for w in self._per_ref_widgets if isinstance(w, SingleRefWidget)]

    def _connect_ref_widget(self, widget: SingleRefWidget):
        widget.ref_accepted.connect(self._on_single_ref_accepted)
        widget.ref_replace_requested.connect(self._on_single_ref_replace)
        widget.ref_chat_requested.connect(self._on_single_ref_chat)
        widget.ref_removed.connect(self._on_single_ref_removed)

    def _show_per_ref_mode(self, evidence: EvidenceRecord, sentence: SentenceRecord = None):
        """Build one reference widget per citation, grouped by marker slot.

        A (REF) sentence has one slot, a (REFS) sentence one slot holding
        every selected reference, a sentence handled marker by marker one
        slot per marker (``EvidenceRecord.slot_sizes``). A slot without a
        citation shows a placeholder whose Replace button opens the search.
        The widgets are the source of truth while the user decides;
        ``evidence.selected`` and ``slot_sizes`` are rebuilt from them in
        ``_check_all_refs_reviewed``.
        """
        self._clear_per_ref_widgets()

        # Hide text view, show per-ref scroll
        self.ref_detail.setVisible(False)
        self.per_ref_scroll.setVisible(True)

        # Hide the global accept/modify controls — per-ref widgets have their
        # own; "Leave unchanged" applies to the whole sentence and stays.
        self.accept_btn.setVisible(False)
        self.modify_btn.setVisible(False)
        self.pmid_edit.setVisible(False)
        self.skip_btn.setVisible(True)

        n_slots = self._n_slots(sentence)
        if n_slots > 1:
            evidence.normalize_slots(n_slots)
        else:
            evidence.slot_sizes = [len(evidence.selected)]
        ranges = evidence.slot_ranges(n_slots)
        markers = sentence.markers if sentence else []
        show_slot_labels = n_slots > 1

        # (slot, citation or None, index into selected or -1)
        rows: list[tuple[int, Optional[CitationCandidate], int]] = []
        for slot, (start, end) in enumerate(ranges):
            if start == end:
                rows.append((slot, None, -1))
                continue
            for i in range(start, end):
                rows.append((slot, evidence.selected[i], i))
        if not rows:
            rows.append((0, None, -1))

        several = len(rows) > 1
        offset = 0
        if several:
            # "Accept All" only makes sense for several references
            accept_all_btn = QPushButton("✓ Accept All References")
            accept_all_btn.setStyleSheet(
                "background-color: #27ae60; color: white; padding: 6px 16px; "
                "border-radius: 4px; font-size: 12px; font-weight: bold;"
            )
            accept_all_btn.clicked.connect(self._on_accept_all_refs)
            self.per_ref_layout.insertWidget(0, accept_all_btn)
            self._per_ref_widgets.append(accept_all_btn)  # track for cleanup
            offset = 1

        info_lines = []
        if evidence.review_decision == ReviewDecision.SKIPPED:
            info_lines.append("Left unchanged: this sentence's marker(s) keep their original "
                              "text on export (keep or replace a reference to change that).")
        if sentence is not None:
            summary = self._suggested_summary(sentence)
            if summary:
                info_lines.append(summary)
        if evidence.confidence_rationale:
            info_lines.append(f"AI assessment: {evidence.confidence_rationale}")
        if info_lines:
            info = WrappedLabel("\n".join(info_lines))
            info.setStyleSheet("font-size: 11px; color: #34495e; padding: 4px 2px;")
            self.per_ref_layout.insertWidget(offset, info)
            self._per_ref_widgets.append(info)
            offset += 1

        already_resolved = evidence.review_decision in (ReviewDecision.ACCEPTED, ReviewDecision.MODIFIED)

        for widget_index, (slot, citation, sel_index) in enumerate(rows):
            slot_label = ""
            if show_slot_labels:
                slot_label = markers[slot].text if slot < len(markers) else f"marker {slot + 1}"
            if citation is not None and is_valid_citation(citation):
                verdict = evidence.verdicts[sel_index] if 0 <= sel_index < len(evidence.verdicts) else None
                widget = SingleRefWidget(widget_index, citation, accepted=already_resolved,
                                         parent=self.per_ref_container, allow_remove=several,
                                         verdict=verdict, slot=slot, slot_label=slot_label)
            else:
                # Missing citation for this slot — show a placeholder widget.
                # The placeholder lives only in the widget; evidence.selected
                # is rebuilt from widget decisions in _check_all_refs_reviewed,
                # so it can never leak into the exported bibliography.
                widget = SingleRefWidget(widget_index, make_placeholder_citation(), accepted=False,
                                         parent=self.per_ref_container, allow_remove=several,
                                         slot=slot, slot_label=slot_label)
            self._connect_ref_widget(widget)
            # Insert before the stretch
            self.per_ref_layout.insertWidget(offset + widget_index, widget)
            self._per_ref_widgets.append(widget)
            if widget._accepted:
                self._per_ref_accepted[widget_index] = True

        if evidence.abstract_snippets:
            snippets = WrappedLabel("Supporting snippets:\n" + "\n".join(
                f'  “{snip[:150]}…”' for snip in evidence.abstract_snippets))
            snippets.setStyleSheet("font-size: 11px; color: #555; padding: 4px 2px;")
            self.per_ref_layout.insertWidget(offset + len(rows), snippets)
            self._per_ref_widgets.append(snippets)

    def _show_text_mode(self):
        """Switch back to the simple text view for single-ref sentences."""
        self._clear_per_ref_widgets()
        self.ref_detail.setVisible(True)
        self.per_ref_scroll.setVisible(False)

        # Restore global action buttons
        self.accept_btn.setVisible(True)
        self.modify_btn.setVisible(True)
        self.skip_btn.setVisible(True)

    @staticmethod
    def _highlight_markers(sentence: SentenceRecord) -> str:
        """HTML of the sentence with each marker highlighted (suggested ones in blue)."""
        raw = sentence.raw_text
        spans = []
        last_end = 0
        for m in sentence.markers:
            valid = (
                last_end <= m.start < m.end <= len(raw)
                and (not m.text or raw[m.start:m.end] == m.text)
            )
            if not valid:
                spans = []   # stale spans (old project data): fall back to regex
                break
            spans.append((m.start, m.end, m.kind == MarkerType.SUGGESTED))
            last_end = m.end
        if not spans:
            # Legacy record without spans: highlight (REF)/(REFS) textually
            import re
            return re.sub(
                r'\((REFS?)\)',
                r'<span style="background-color: #fff3cd; color: #856404; font-weight: bold; '
                r'padding: 1px 4px; border-radius: 3px; border: 1px solid #ffc107;">'
                r'(\1)</span>',
                html.escape(raw),
            )
        out = []
        pos = 0
        for start, end, suggested in spans:
            out.append(html.escape(raw[pos:start]))
            if suggested:
                style = ("background-color: #e3f2fd; color: #0d47a1; font-weight: bold; "
                         "padding: 1px 4px; border-radius: 3px; border: 1px solid #64b5f6;")
            else:
                style = ("background-color: #fff3cd; color: #856404; font-weight: bold; "
                         "padding: 1px 4px; border-radius: 3px; border: 1px solid #ffc107;")
            out.append(f'<span style="{style}">{html.escape(raw[start:end])}</span>')
            pos = end
        out.append(html.escape(raw[pos:]))
        return "".join(out)

    @staticmethod
    def _suggested_summary(sentence: SentenceRecord) -> str:
        """One line per author-suggested marker for the detail view."""
        lines = []
        for m in sentence.markers:
            if m.kind != MarkerType.SUGGESTED:
                continue
            labels = ", ".join(s.label for s in m.suggestions) or "(none)"
            extra = ""
            if m.extra_search:
                extra = " + additional reference(s) to find" if m.extra_search != 1 else " + 1 additional reference to find"
            lines.append(f"Author-suggested {m.text}: {labels}{extra}")
        return "\n".join(lines)

    @Slot(object, object)
    def _on_sentence_selected(self, current, previous):
        """Handle sentence selection in the list."""
        item = current
        if not item or not isinstance(item, SentenceListItem):
            return

        # Close chat panel if open when switching sentences
        if self.chat_panel.isVisible():
            self._close_chat_panel()

        self._current_sentence_id = item.sentence_id
        sentence = item.sentence
        evidence = item.evidence

        # Show sentence text with markers highlighted
        highlighted = self._highlight_markers(sentence)
        self.sentence_text.setHtml(
            f'<div style="font-size: 13px; line-height: 1.4;">{highlighted}</div>'
        )
        self.skip_btn.setEnabled(True)

        # Show confidence
        if evidence and evidence.selected:
            level = evidence.confidence_level
            score = evidence.confidence_score
            color_map = {
                ConfidenceLevel.HIGH: COLOR_HIGH,
                ConfidenceLevel.MEDIUM: COLOR_MEDIUM,
                ConfidenceLevel.LOW: COLOR_LOW,
                ConfidenceLevel.UNRESOLVED: COLOR_UNRESOLVED,
            }
            color = color_map.get(level, COLOR_UNRESOLVED)
            badge = f"{level.value} ({score:.0f}/100)"
            if evidence.verification_status != VerificationStatus.NOT_CHECKED:
                badge += f" · {evidence.verification_status.value.replace('_', ' ')}"
            self.confidence_label.setText(badge)
            self.confidence_label.setStyleSheet(
                f"color: white; background-color: {color}; font-size: 14px; "
                f"font-weight: bold; padding: 4px 12px; border-radius: 4px;"
            )

            # Every sentence with candidates gets per-reference widgets
            # (Keep / View / Replace, plus Remove when there are several),
            # whether it has one (REF) or many (REFS) references.
            self._show_per_ref_mode(evidence, sentence)

            # Warnings
            if evidence.warnings:
                warn_text = "Warnings: " + "; ".join(w.message for w in evidence.warnings)
                self.warnings_label.setText(warn_text)
            else:
                self.warnings_label.setText("")

        else:
            self.confidence_label.setText("No candidates found")
            self.confidence_label.setStyleSheet(
                "color: white; background-color: #999; font-size: 14px; "
                "font-weight: bold; padding: 4px 12px; border-radius: 4px;"
            )
            if evidence is not None and evidence.warnings:
                self.warnings_label.setText(
                    "Warnings: " + "; ".join(w.message for w in evidence.warnings))
            else:
                self.warnings_label.setText("")
            if evidence is not None:
                # The pipeline ran and found nothing: a placeholder slot per
                # marker whose Replace button opens the search.
                self._show_per_ref_mode(evidence, sentence)
            else:
                self._show_text_mode()
                text = ""
                if sentence.has_suggested_marker:
                    text = self._suggested_summary(sentence) + "\n\n"
                text += ("No references were found for this sentence.\n"
                         "Use 'Modify / Search' to find one or enter a PMID, or "
                         "'Leave unchanged' to keep the text as written.")
                self.ref_detail.setPlainText(text)
                self.accept_btn.setEnabled(False)
                self.modify_btn.setEnabled(True)

    def _on_accept(self):
        """Accept the current citation assignment (single-ref or whole-sentence accept)."""
        if not self._current_sentence_id or not self._project:
            return

        evidence = self._project.evidence_map.get(self._current_sentence_id)
        if evidence:
            evidence.review_decision = ReviewDecision.ACCEPTED
            self.project_modified.emit()
            logger.info(f"Accepted citations for {self._current_sentence_id}")

            # Update list item
            item = self.sentence_list.currentItem()
            if isinstance(item, SentenceListItem):
                item.update_evidence(evidence)

            self._update_status()
            self._advance_to_next_unresolved()

    def _on_skip(self):
        """Leave the current sentence's marker(s) unchanged in the exported document."""
        if not self._current_sentence_id or not self._project:
            return

        evidence = self._project.evidence_map.get(self._current_sentence_id)
        if evidence is None:
            evidence = EvidenceRecord(sentence_id=self._current_sentence_id)
            self._project.evidence_map[self._current_sentence_id] = evidence

        evidence.review_decision = ReviewDecision.SKIPPED
        self.project_modified.emit()
        logger.info(f"Marker left unchanged for {self._current_sentence_id}")

        item = self.sentence_list.currentItem()
        if isinstance(item, SentenceListItem):
            item.update_evidence(evidence)

        self._update_status()
        self._advance_to_next_unresolved()

    # ── Per-reference review handlers (REFS with multiple refs) ──────────

    @Slot(int)
    def _on_single_ref_accepted(self, index: int):
        """A single reference within a REFS sentence was accepted."""
        self._per_ref_accepted[index] = True
        self._check_all_refs_reviewed()

    @Slot(int)
    def _on_single_ref_removed(self, index: int):
        """User chose to remove a single reference at `index` from the citation list."""
        if not self._current_sentence_id or not self._project:
            return

        evidence = self._project.evidence_map.get(self._current_sentence_id)
        if not evidence:
            return

        self._per_ref_removed[index] = True

        # Do NOT mutate evidence.selected here — popping would shift every
        # later widget's index onto the wrong citation.  The widgets are the
        # source of truth; evidence.selected is rebuilt from their decisions
        # in _check_all_refs_reviewed once every slot has one.
        logger.info(f"Removed ref [{index+1}] for {self._current_sentence_id}")

        self._check_all_refs_reviewed()

    @Slot(int, str)
    def _on_single_ref_replace(self, index: int, identifier: str):
        """User wants to replace a single reference at `index` with a new PMID/DOI."""
        if not self._current_sentence_id or not self._project:
            return

        email = self._project.settings.ncbi_email or ""
        api_key = self._project.settings.ncbi_api_key or ""

        # Store the index being replaced so the callback knows where to put it
        self._replace_ref_index = index
        self._replace_ref_sentence_id = self._current_sentence_id

        self._fetch_worker = PMIDFetchWorker(identifier, email, api_key, parent=self)
        self._fetch_worker.finished.connect(self._on_single_ref_fetched)
        self._fetch_worker.error.connect(self._on_single_ref_fetch_error)
        self._fetch_worker.start()

    def _open_chat_for_current(self):
        """Show the chat panel for the current sentence."""
        sentence = self._get_current_sentence()
        if not sentence:
            return False

        self.chat_panel.setVisible(True)

        # Resize splitter: sentence list | detail | chat = 25% | 40% | 35%
        total = self.splitter.width() or 900
        self.splitter.setSizes([
            int(total * 0.25),
            int(total * 0.40),
            int(total * 0.35),
        ])

        # Initialize the chat panel for this sentence
        self.chat_panel.open_for_sentence(
            sentence_id=self._current_sentence_id,
            claim_text=sentence.clean_text,
            settings=self._project.settings,
            context_text=build_claim_context(self._project.sentences, sentence).to_context_block(),
        )
        return True

    @Slot(int)
    def _on_single_ref_chat(self, index: int):
        """Open the chat panel to replace a single reference at `index`."""
        if not self._current_sentence_id or not self._project:
            return
        # Track which per-ref index the chat should replace
        self._chat_replace_index = index
        if not self._open_chat_for_current():
            self._chat_replace_index = None

    @Slot(object)
    def _on_single_ref_fetched(self, article: CitationCandidate):
        """Handle successful fetch for a per-ref replacement."""
        index = self._replace_ref_index
        sentence_id = self._replace_ref_sentence_id

        # Stale callback guard: the user may have switched sentences while
        # the fetch was in flight — the widgets now belong to another sentence.
        if sentence_id != self._current_sentence_id:
            logger.info(f"Discarding stale fetch result for {sentence_id}")
            return

        evidence = self._project.evidence_map.get(sentence_id)
        if not evidence:
            return

        # The widget holds the replacement; evidence.selected is rebuilt from
        # widget decisions in _check_all_refs_reviewed.
        article.composite_score = 100.0
        article.score_rationale = "User-specified replacement"
        evidence.candidates = [article] + evidence.candidates

        logger.info(f"Replaced ref [{index+1}] for {sentence_id} with: {article.title[:60]}")

        # Update the widget
        for w in self._ref_widgets():
            if w.index == index:
                w.mark_replaced(article)
                break

        self._per_ref_accepted[index] = True
        self._check_all_refs_reviewed()

    @Slot(str)
    def _on_single_ref_fetch_error(self, error_msg: str):
        """Handle fetch failure for a per-ref replacement."""
        index = self._replace_ref_index

        for w in self._ref_widgets():
            if w.index == index:
                w.mark_fetch_error(error_msg)
                break

        QMessageBox.warning(
            self, "Lookup Failed",
            f"Could not fetch reference:\n{error_msg}\n\n"
            "Check that the identifier is correct and you have internet access."
        )

    def _on_accept_all_refs(self):
        """Accept all non-removed references for the current REFS sentence at once."""
        if not self._current_sentence_id or not self._project:
            return

        evidence = self._project.evidence_map.get(self._current_sentence_id)
        if not evidence:
            return

        # Mark all non-removed per-ref widgets as accepted.  Placeholder slots
        # ("No citation found") are skipped — the user must explicitly replace
        # or remove them, otherwise Accept All would silently bless an
        # unresolved marker.
        for w in self._ref_widgets():
            if not w._removed and not w._accepted and is_valid_citation(w.citation):
                w._on_accept()

        # This triggers _check_all_refs_reviewed via each widget's signal

    def _check_all_refs_reviewed(self):
        """Check if all individual references have been reviewed; if so, mark sentence resolved.

        A reference is "reviewed" when it has been accepted, replaced, or removed.
        The sentence is resolved once every SingleRefWidget has a decision.
        """
        if not self._current_sentence_id or not self._project:
            return

        evidence = self._project.evidence_map.get(self._current_sentence_id)
        if not evidence:
            return

        widgets = self._ref_widgets()
        all_removed_or_accepted = all(w._accepted or w._removed for w in widgets)
        if not all_removed_or_accepted:
            return  # Still have undecided refs

        # All refs have a decision — rebuild evidence.selected from the
        # widgets (the single write point; avoids stale-index corruption).
        # Each kept reference goes back into the marker slot its widget
        # belongs to, so a sentence handled marker by marker keeps one block
        # of references per marker (slot_sizes); an emptied slot exports as
        # [?] for a (REF)/(REFS) marker and keeps its text for a suggested one.
        sentence = self._get_current_sentence()
        n_slots = self._n_slots(sentence)
        rebuilt = []
        rebuilt_verdicts = []
        sizes = [0] * n_slots
        # Grouped by slot (a stable sort keeps the card order within a slot),
        # since slot_sizes describes contiguous blocks of ``selected``.
        for w in sorted(widgets, key=lambda w: w.slot):
            if w._removed or not is_valid_citation(w.citation):
                continue
            rebuilt.append(w.citation)
            sizes[min(max(w.slot, 0), n_slots - 1)] += 1
            rebuilt_verdicts.append(w.verdict or CitationVerdict(
                key=w.citation.pmid or w.citation.doi or w.citation.title,
                reason="chosen by the user"))
        evidence.selected = rebuilt
        evidence.slot_sizes = sizes
        evidence.verdicts = rebuilt_verdicts if evidence.verdicts else []

        # All refs have a decision — mark sentence as resolved
        has_removal = any(w._removed for w in widgets)
        has_replacement = any(
            w.citation.score_rationale in ("User-specified replacement", "User-selected via chat")
            for w in widgets
        )

        if evidence.review_decision in (ReviewDecision.PENDING, ReviewDecision.SKIPPED,
                                        ReviewDecision.REJECTED):
            if has_removal or has_replacement:
                evidence.review_decision = ReviewDecision.MODIFIED
            else:
                evidence.review_decision = ReviewDecision.ACCEPTED

        self.project_modified.emit()
        logger.info(
            f"All refs reviewed for {self._current_sentence_id}: "
            f"{len(rebuilt)} kept, {sum(1 for w in widgets if w._removed)} removed"
        )

        # Update list item
        item = self.sentence_list.currentItem()
        if isinstance(item, SentenceListItem):
            item.update_evidence(evidence)

        self._update_status()
        self._advance_to_next_unresolved()

    def _on_modify(self):
        """Open the chat panel for interactive citation search."""
        if not self._current_sentence_id or not self._project:
            return
        # Track that this is a whole-sentence modify (not a per-ref replace)
        self._chat_replace_index = None
        self._open_chat_for_current()

    # ── Chat panel integration ───────────────────────────────────

    @Slot(list)
    def _on_chat_citation_selected(self, citations: list):
        """Handle citation selection from the chat panel."""
        if not self._current_sentence_id or not self._project:
            return

        evidence = self._project.evidence_map.get(self._current_sentence_id)
        if not evidence:
            evidence = EvidenceRecord(sentence_id=self._current_sentence_id)
            self._project.evidence_map[self._current_sentence_id] = evidence

        # Per-reference replacement (the Replace button on one card)
        if self._chat_replace_index is not None and citations:
            index = self._chat_replace_index
            for article in citations:
                article.composite_score = 100.0
                article.score_rationale = "User-selected via chat"
            first, extra = citations[0], list(citations[1:])

            # A marker may cite several papers, even when it was (REF): the
            # extra picks become new cards in the same marker slot.
            target = next((w for w in self._ref_widgets() if w.index == index), None)
            slot = target.slot if target is not None else 0

            # The widgets hold the replacement; evidence.selected is rebuilt
            # from widget decisions in _check_all_refs_reviewed.
            evidence.candidates = citations + evidence.candidates
            logger.info(
                f"Chat replaced ref [{index+1}] for {self._current_sentence_id}: "
                f"{first.title[:60]}" + (f" (+{len(extra)} more)" if extra else "")
            )

            if target is not None:
                target.mark_replaced(first)
            self._per_ref_accepted[index] = True
            if extra:
                self._append_ref_widgets(extra, slot=slot, after=target)
            self._check_all_refs_reviewed()
        else:
            # Whole-sentence selection
            for article in citations:
                article.composite_score = 100.0
                article.score_rationale = "User-selected via chat"

            # On multi-ref sentences, merge with already-selected refs instead
            # of overwriting — the user may have accepted some of them already.
            sentence = self._get_current_sentence()
            n_slots = self._n_slots(sentence)
            is_multi = bool(sentence and (
                n_slots > 1
                or sentence.marker_type == MarkerType.REFS
            ))
            existing_valid = [c for c in evidence.selected if is_valid_citation(c)]
            if n_slots > 1:
                # One block per marker: kept references stay where they are
                # and each new paper goes to the first marker without one
                # (so three picks for three empty markers fill them in
                # order), else to the last marker.
                ranges = evidence.slot_ranges(n_slots)
                blocks = [[c for c in evidence.selected[s:e] if is_valid_citation(c)]
                          for s, e in ranges]
                seen = {self._citation_key(c) for c in existing_valid}
                for article in citations:
                    if self._citation_key(article) in seen:
                        continue
                    seen.add(self._citation_key(article))
                    target = next((i for i, b in enumerate(blocks) if not b), len(blocks) - 1)
                    blocks[target].append(article)
                evidence.selected = [c for b in blocks for c in b]
                evidence.slot_sizes = [len(b) for b in blocks]
            elif is_multi and existing_valid:
                # A (REFS) sentence: merge with the references already kept
                seen = {self._citation_key(c) for c in existing_valid}
                merged = list(existing_valid)
                for article in citations:
                    if self._citation_key(article) not in seen:
                        seen.add(self._citation_key(article))
                        merged.append(article)
                evidence.selected = merged
                evidence.slot_sizes = [len(merged)]
            else:
                evidence.selected = list(citations)
                evidence.slot_sizes = [len(citations)]
            evidence.candidates = citations + evidence.candidates
            evidence.review_decision = ReviewDecision.MODIFIED
            self.project_modified.emit()
            evidence.confidence_level = ConfidenceLevel.HIGH
            evidence.confidence_score = 100.0
            if citations:
                evidence.confidence_rationale = f"User-selected: {citations[0].title[:60]}"

            logger.info(
                f"Chat selection for {self._current_sentence_id}: "
                f"{len(citations)} citation(s)"
            )

            # Update UI
            item = self.sentence_list.currentItem()
            if isinstance(item, SentenceListItem):
                item.update_evidence(evidence)

            self._on_sentence_selected(item, None)
            self._update_status()
            self._advance_to_next_unresolved()

        # Close chat panel
        self._close_chat_panel()

    def _append_ref_widgets(self, citations: list, slot: int = 0, after: Optional[SingleRefWidget] = None):
        """Add already-chosen papers as new reference cards in marker *slot*,
        right after *after* (else after the last card).

        Used when the user picks several papers for one marker: the cards
        are marked replaced, so they count as decided.
        """
        ref_widgets = self._ref_widgets()
        next_index = (max(w.index for w in ref_widgets) + 1) if ref_widgets else 0
        anchor = after if after is not None else (ref_widgets[-1] if ref_widgets else None)
        position = (self.per_ref_layout.indexOf(anchor) + 1) if anchor is not None else 0
        slot_label = anchor.slot_label if anchor is not None else ""
        # Keep the bookkeeping list in display order (the rebuild groups by
        # slot, and cards of one slot keep this order).
        insert_at = len(self._per_ref_widgets)
        if anchor is not None and any(w is anchor for w in self._per_ref_widgets):
            insert_at = next(i for i, w in enumerate(self._per_ref_widgets) if w is anchor) + 1
        for offset, citation in enumerate(citations):
            widget = SingleRefWidget(next_index + offset, citation, accepted=False,
                                     parent=self.per_ref_container, allow_remove=True,
                                     slot=slot, slot_label=slot_label)
            self._connect_ref_widget(widget)
            widget.mark_replaced(citation)
            self.per_ref_layout.insertWidget(position + offset, widget)
            self._per_ref_widgets.insert(insert_at + offset, widget)
            self._per_ref_accepted[widget.index] = True

    def _close_chat_panel(self):
        """Hide the chat panel and restore splitter sizes."""
        self.chat_panel.cancel_active_search()
        self.chat_panel.setVisible(False)
        self._chat_replace_index = None
        self.splitter.setSizes([350, 550, 0])

    def shutdown_workers(self, wait_ms: int = 3000):
        """Cancel and wait for all background workers — call before app close."""
        self.chat_panel.shutdown_workers(wait_ms)
        if self._fetch_worker and self._fetch_worker.isRunning():
            if not self._fetch_worker.wait(wait_ms):
                logger.warning("Fetch worker did not stop in time; terminating")
                self._fetch_worker.terminate()
                self._fetch_worker.wait(1000)

    def _advance_to_next_unresolved(self):
        """Move selection to the next unresolved sentence."""
        current_row = self.sentence_list.currentRow()
        for i in range(current_row + 1, self.sentence_list.count()):
            item = self.sentence_list.item(i)
            if isinstance(item, SentenceListItem) and item.evidence:
                if not item.evidence.is_resolved:
                    self.sentence_list.setCurrentRow(i)
                    return

    def _apply_filter(self, index: int):
        """Apply filter to the sentence list."""
        for i in range(self.sentence_list.count()):
            item = self.sentence_list.item(i)
            if not isinstance(item, SentenceListItem):
                continue

            show = True
            ev = item.evidence

            if index == 1:  # Low confidence only
                show = ev and ev.confidence_level == ConfidenceLevel.LOW
            elif index == 2:  # Unresolved only
                show = not ev or not ev.is_resolved
            elif index == 3:  # Warnings only
                show = ev and len(ev.warnings) > 0
            elif index == 4:  # Author-suggested markers only
                show = item.sentence.has_suggested_marker

            item.setHidden(not show)
