"""Tab 1: Document loading and settings."""

import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QGroupBox, QFormLayout, QLineEdit, QSpinBox,
    QComboBox, QCheckBox, QFrame, QSplitter, QTextEdit, QScrollArea
)
from PySide6.QtCore import Signal, Qt, QSettings, QThread
from PySide6.QtGui import QDragEnterEvent, QDropEvent

from ..models.project import ProjectSettings, CitationStyle
from ..services.model_catalog import (
    NEWEST_MODEL, ModelInfo, ModelSource, cache_is_stale, load_cached_models,
    load_models, refresh_models,
)

logger = logging.getLogger(__name__)


# ── Help text content for each "?" button ────────────────────────────
HELP_TEXTS = {
    "ncbi_email": (
        "<h3>NCBI Email</h3>"
        "<p>NCBI (National Center for Biotechnology Information) requires an email address "
        "for all programmatic access to PubMed. This is used to identify you so NCBI can "
        "contact you if there is a problem with your queries.</p>"
        "<p><b>What it does:</b> Your email is sent with every PubMed search request. "
        "Without it, PubMed queries will fail.</p>"
        "<p><b>How to set it up:</b> Simply enter the email address you use for academic "
        "or professional correspondence. Any valid email will work &mdash; no registration "
        "is needed.</p>"
    ),
    "ncbi_api_key": (
        "<h3>NCBI API Key</h3>"
        "<p>An optional API key that increases your PubMed request rate from "
        "<b>3 requests/second</b> to <b>10 requests/second</b>. This makes the citation "
        "search pipeline significantly faster, especially for documents with many markers.</p>"
        "<p><b>What it does:</b> Authenticates your requests with NCBI, unlocking higher "
        "throughput and priority access to the E-utilities API.</p>"
        "<p><b>How to get one:</b></p>"
        "<ol>"
        "<li>Go to <b>https://www.ncbi.nlm.nih.gov/account/</b></li>"
        "<li>Sign in or create a free NCBI account</li>"
        "<li>Once logged in, click your username in the top-right corner</li>"
        "<li>Go to <b>Account Settings</b></li>"
        "<li>Scroll down to the <b>API Key Management</b> section</li>"
        "<li>Click <b>\"Create an API Key\"</b></li>"
        "<li>Copy the generated key and paste it here</li>"
        "</ol>"
        "<p><i>This is optional &mdash; the app works without it, just at a slower rate.</i></p>"
    ),
    "orcid": (
        "<h3>ORCID</h3>"
        "<p>ORCID (Open Researcher and Contributor ID) is a unique persistent identifier "
        "for researchers. Providing your ORCID helps the AI detect <b>self-citations</b> "
        "&mdash; places in your manuscript where you reference your own prior work.</p>"
        "<p><b>What it does:</b> When the AI encounters language suggesting self-citation "
        "(e.g., \"we previously showed\", \"our lab demonstrated\"), it uses your ORCID to "
        "search PubMed for your publications, ensuring the correct paper is matched.</p>"
        "<p><b>How to find yours:</b></p>"
        "<ol>"
        "<li>Go to <b>https://orcid.org</b></li>"
        "<li>Sign in to your existing account, or register for free</li>"
        "<li>Your ORCID is displayed on your profile page in the format "
        "<b>0000-0000-0000-0000</b></li>"
        "<li>Copy and paste it here</li>"
        "</ol>"
        "<p><i>This is optional &mdash; if not provided, self-citation detection relies "
        "on author name matching, which is less precise.</i></p>"
    ),
    "anthropic_api_key": (
        "<h3>Anthropic API Key</h3>"
        "<p>This key is <b>required</b> to power the AI citation search. AI REFs uses "
        "Anthropic's Claude model to intelligently read your sentences, formulate PubMed "
        "search queries, evaluate results, and select the best-matching references.</p>"
        "<p><b>What it does:</b> Authenticates your requests with the Anthropic API. "
        "Usage is billed to your Anthropic account based on the number of tokens processed. "
        "A typical document costs a few cents with Haiku, or a few dollars with Opus.</p>"
        "<p><b>How to get one:</b></p>"
        "<ol>"
        "<li>Go to <b>https://console.anthropic.com/</b></li>"
        "<li>Sign up or log in to your Anthropic account</li>"
        "<li>Navigate to <b>API Keys</b> in the left sidebar</li>"
        "<li>Click <b>\"Create Key\"</b></li>"
        "<li>Give it a name (e.g., \"AI REFs\") and click create</li>"
        "<li>Copy the key (starts with <code>sk-ant-...</code>) and paste it here</li>"
        "</ol>"
        "<p><b>Important:</b> You need billing set up on your Anthropic account. "
        "New accounts may include free credits.</p>"
    ),
    "claude_model": (
        "<h3>Claude Model</h3>"
        "<p>Choose which Claude model performs the citation search. The list is fetched "
        "from Anthropic for your API key, so new models appear here as soon as they are "
        "released (newest first); click the refresh button to update it. Your selection "
        "is never changed for you.</p>"
        "<p><b>What it does:</b> The selected model reads each marked sentence, decides "
        "what to search for, evaluates candidate papers, and picks the best match. "
        "A smarter model finds better references but costs more and runs slower.</p>"
        "<p><b>Tiers:</b></p>"
        "<ul>"
        "<li><b>Haiku</b> &mdash; Fastest and cheapest. Good for straightforward claims "
        "with obvious keywords, quick drafts, or budget-conscious use.</li>"
        "<li><b>Sonnet</b> &mdash; Balanced speed, cost and quality; a good everyday choice.</li>"
        "<li><b>Opus</b> &mdash; Strongest reasoning in the standard tier; best for nuanced "
        "or interdisciplinary claims where the right paper is not obvious.</li>"
        "<li><b>Fable</b> &mdash; Anthropic's most capable tier, at the highest price.</li>"
        "</ul>"
        "<p>New projects default to the newest model on the list; pick a cheaper tier "
        "here if cost matters more than nuance.</p>"
    ),
    "input_format": (
        "<h3>Input Document Format</h3>"
        "<p>AI REFs works with <b>.docx</b> (Microsoft Word) documents. Place special "
        "markers in your text where you want citations to be inserted.</p>"
        "<h3>Markers</h3>"
        "<ul>"
        "<li><b>(REF)</b> &mdash; Insert a <b>single</b> citation. The AI will find "
        "the one best-matching reference for the claim.</li>"
        "<li><b>(REFS)</b> &mdash; Insert <b>multiple</b> citations (2&ndash;10, "
        "controlled by the <i>Max refs for (REFS)</i> setting). Use this when a claim "
        "needs several supporting references.</li>"
        "</ul>"
        "<h3>Marker Placement</h3>"
        "<p>Markers can appear anywhere in a sentence:</p>"
        "<ul>"
        "<li><b>End of sentence:</b> <i>\"Synaptic plasticity is crucial for learning "
        "<b>(REF)</b>.\"</i></li>"
        "<li><b>Inline:</b> <i>\"AlphaMissense<b>(REF)</b> predicts pathogenic variants, "
        "while PepPrCLIP<b>(REF)</b> models peptide interactions.\"</i></li>"
        "<li><b>After a clause:</b> <i>\"Using BioID proximity proteomics "
        "<b>(REFS)</b>, we identified novel interactors.\"</i></li>"
        "</ul>"
        "<p>When multiple <b>(REF)</b> markers appear in the same sentence, each one is "
        "resolved independently based on the surrounding context.</p>"
        "<h3>Author-Suggested Citations</h3>"
        "<p>If you already know which paper you mean, write it in parentheses and the "
        "app will look it up, score how well it supports the sentence, and let you "
        "confirm or replace it in the Review tab:</p>"
        "<ul>"
        "<li><b>(PMID: 32879322)</b> &mdash; PubMed ID</li>"
        "<li><b>(PMC11413553)</b> &mdash; PubMed Central ID</li>"
        "<li><b>(doi: 10.1101/2024.01.03.574066)</b> &mdash; DOI (journal article or preprint)</li>"
        "<li><b>(Battison et al. 2024)</b>, <b>(Smith and Jones, 2020)</b>, "
        "<b>(Smith 2019a)</b> &mdash; first author and year; the AI picks the matching "
        "paper when the author published more than once that year</li>"
        "</ul>"
        "<p>Several citations can share one pair of parentheses, separated by commas or "
        "semicolons, and the kinds can be mixed: <i>(PMC11413553, PMC3159129)</i>, "
        "<i>(PMID: 32879322; Battison et al. 2024)</i>. Adding <b>REF</b> inside the "
        "same parentheses, e.g. <i>(REF, PMID: 32879322)</i>, verifies the suggestion "
        "and searches for one more.</p>"
        "<p>A parenthetical only counts when its whole content is citations, so "
        "<i>(n = 12)</i>, <i>(Fig. 2B)</i>, or <i>(December 2024)</i> are left alone. "
        "Use the two <i>Suggested citations</i> checkboxes below to switch detection "
        "off. Suggested citations you do not confirm keep their original text on export.</p>"
        "<h3>Insert Mode</h3>"
        "<p>If your document already has numbered citations and a References section, "
        "AI REFs automatically enters <b>insert mode</b>. In this mode:</p>"
        "<ul>"
        "<li>Only new markers (<b>(REF)</b>, <b>(REFS)</b>, and author-suggested "
        "citations) are processed</li>"
        "<li>Existing citations are preserved and renumbered as needed</li>"
        "<li>The bibliography is merged automatically</li>"
        "</ul>"
        "<h3>Tips</h3>"
        "<ul>"
        "<li>Document formatting (bold, italic, etc.) is preserved in the output</li>"
        "<li>Write your claims in clear, specific language for best citation matching</li>"
        "<li>The AI uses the surrounding sentence context to find relevant papers, "
        "so more descriptive text leads to better results</li>"
        "<li>You can review and replace any AI-suggested citation before exporting</li>"
        "</ul>"
    ),
}


class HelpButton(QPushButton):
    """Small circular '?' button that emits its help key when clicked."""
    help_requested = Signal(str)  # emits the help key

    def __init__(self, help_key: str, parent=None):
        super().__init__("?", parent)
        self.help_key = help_key
        self.setFixedSize(20, 20)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QPushButton {"
            "  background-color: #4a90d9;"
            "  color: white;"
            "  border-radius: 10px;"
            "  font-size: 12px;"
            "  font-weight: bold;"
            "  border: none;"
            "  padding: 0px;"
            "}"
            "QPushButton:hover {"
            "  background-color: #357abd;"
            "}"
            "QPushButton:pressed {"
            "  background-color: #2a5f9e;"
            "}"
        )
        self.clicked.connect(lambda: self.help_requested.emit(self.help_key))


class DropZone(QFrame):
    """Drag-and-drop zone for DOCX files."""
    file_dropped = Signal(str)

    _STYLESHEET = """
        QFrame[dragActive="false"] {
            border: 2px dashed #aaa;
            border-radius: 12px;
            background-color: #fafafa;
        }
        QFrame[dragActive="false"]:hover {
            border-color: #4a90d9;
            background-color: #f0f7ff;
        }
        QFrame[dragActive="true"] {
            border: 2px dashed #4a90d9;
            border-radius: 12px;
            background-color: #e3f2fd;
        }
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(120)
        self.setProperty("dragActive", "false")
        self.setStyleSheet(self._STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)

        self.label = QLabel("Drop a .docx file here\nor click Browse below")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet("color: #888; font-size: 14px; border: none;")
        layout.addWidget(self.label)

        self.file_label = QLabel("")
        self.file_label.setAlignment(Qt.AlignCenter)
        self.file_label.setStyleSheet("color: #2c3e50; font-size: 13px; font-weight: bold; border: none;")
        layout.addWidget(self.file_label)

    def _set_drag_active(self, active: bool):
        self.setProperty("dragActive", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls and urls[0].toLocalFile().endswith('.docx'):
                event.acceptProposedAction()
                self._set_drag_active(True)

    def dragLeaveEvent(self, event):
        self._set_drag_active(False)

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if path.endswith('.docx'):
                self.set_file(path)
                self.file_dropped.emit(path)
        self._set_drag_active(False)

    def set_file(self, path: str):
        name = Path(path).name
        self.file_label.setText(f"Loaded: {name}")
        self.label.setText("File loaded successfully")
        self.label.setStyleSheet("color: #4CAF50; font-size: 14px; border: none;")


class _ModelRefreshThread(QThread):
    """Fetch the model list off the GUI thread; results come back as signals."""
    succeeded = Signal(object)      # list[ModelInfo]
    failed = Signal(str)

    def __init__(self, api_key: str, parent=None):
        super().__init__(parent)
        self._api_key = api_key

    def run(self):
        try:
            self.succeeded.emit(refresh_models(self._api_key))
        except Exception as exc:                        # network, auth, SDK: all reported
            logger.warning(f"Model list refresh failed: {exc}")
            self.failed.emit(str(exc)[:160] or type(exc).__name__)


class InputsTab(QWidget):
    """Tab for document loading and pipeline settings."""
    file_selected = Signal(str)
    settings_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_file = None
        self._help_buttons: list[HelpButton] = []
        self._active_help_key: str | None = None
        self._refresh_thread: _ModelRefreshThread | None = None
        self._setup_ui()
        self._populate_models_from_cache()
        self._load_saved_settings()
        self._auto_refresh_models()

    def _make_form_row_with_help(self, label_text: str, widget: QWidget, help_key: str) -> QHBoxLayout:
        """Create a form row: Label  [widget]  [?]"""
        row = QHBoxLayout()
        row.setSpacing(6)

        label = QLabel(label_text)
        label.setMinimumWidth(110)
        row.addWidget(label)

        row.addWidget(widget, stretch=1)

        help_btn = HelpButton(help_key)
        help_btn.help_requested.connect(self._show_help)
        self._help_buttons.append(help_btn)
        row.addWidget(help_btn)

        return row

    def _setup_ui(self):
        # Top-level: horizontal splitter between form panel and help panel
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        self.splitter = QSplitter(Qt.Horizontal)

        # ── LEFT: scrollable form panel ──────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")

        form_widget = QWidget()
        layout = QVBoxLayout(form_widget)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # ── Document section ─────────────────────────────────────────
        doc_header = QLabel("Input Document")
        doc_header.setObjectName("sectionHeader")
        layout.addWidget(doc_header)

        self.drop_zone = DropZone()
        self.drop_zone.file_dropped.connect(self._on_file_dropped)
        layout.addWidget(self.drop_zone)

        browse_layout = QHBoxLayout()
        self.browse_btn = QPushButton("Browse...")
        self.browse_btn.clicked.connect(self._browse_file)
        browse_layout.addWidget(self.browse_btn)

        format_help_btn = HelpButton("input_format")
        format_help_btn.setFixedSize(100, 24)
        format_help_btn.setText("Input format?")
        format_help_btn.setStyleSheet(
            "QPushButton {"
            "  background-color: #4a90d9;"
            "  color: white;"
            "  border-radius: 4px;"
            "  font-size: 11px;"
            "  font-weight: bold;"
            "  border: none;"
            "  padding: 2px 8px;"
            "}"
            "QPushButton:hover {"
            "  background-color: #357abd;"
            "}"
            "QPushButton:pressed {"
            "  background-color: #2a5f9e;"
            "}"
        )
        format_help_btn.help_requested.connect(self._show_help)
        self._help_buttons.append(format_help_btn)
        browse_layout.addWidget(format_help_btn)

        browse_layout.addStretch()
        layout.addLayout(browse_layout)

        # ── Insert mode indicator ─────────────────────────────────────
        self.insert_mode_label = QLabel("")
        self.insert_mode_label.setWordWrap(True)
        self.insert_mode_label.setStyleSheet(
            "QLabel {"
            "  background-color: #e8f5e9;"
            "  border: 1px solid #a5d6a7;"
            "  border-radius: 6px;"
            "  padding: 8px 12px;"
            "  color: #2e7d32;"
            "  font-size: 13px;"
            "}"
        )
        self.insert_mode_label.setVisible(False)
        layout.addWidget(self.insert_mode_label)

        # ── Settings section ─────────────────────────────────────────
        settings_group = QGroupBox("Pipeline Settings")
        form = QFormLayout(settings_group)
        form.setSpacing(10)

        # Citation style — ordered list: (label, CitationStyle enum)
        self._style_options: list[tuple[str, CitationStyle]] = [
            # Grant-specific
            ("NIH Grant (NLM with PMCID)", CitationStyle.NIH_GRANT),
            ("NSF Grant", CitationStyle.NSF_GRANT),
            # Biomedical / Life Sciences
            ("Vancouver", CitationStyle.VANCOUVER),
            ("AMA", CitationStyle.AMA),
            ("NLM", CitationStyle.NLM),
            # General Science
            ("APA 7th Edition", CitationStyle.APA),
            ("CSE Author-Date", CitationStyle.CSE_AUTHOR_DATE),
            ("CSE Citation-Sequence", CitationStyle.CSE_CITATION_SEQ),
            ("Nature", CitationStyle.NATURE),
            ("Science / AAAS", CitationStyle.SCIENCE),
            ("PLOS ONE", CitationStyle.PLOS_ONE),
            ("Cell", CitationStyle.CELL),
            ("eLife", CitationStyle.ELIFE),
            ("PNAS", CitationStyle.PNAS),
            # Chemistry / Physics / Engineering
            ("ACS", CitationStyle.ACS),
            ("IEEE", CitationStyle.IEEE),
            ("APS", CitationStyle.APS),
            # Other
            ("Elsevier Harvard", CitationStyle.ELSEVIER_HARVARD),
            ("Chicago Author-Date", CitationStyle.CHICAGO_AUTHOR_DATE),
        ]
        self.style_combo = QComboBox()
        for label, _ in self._style_options:
            self.style_combo.addItem(label)
        self.style_combo.setCurrentIndex(0)  # NIH Grant is default
        form.addRow("Citation Style:", self.style_combo)

        # How the reference entries themselves are formatted.  The style's own
        # rules are the point of choosing a style; the other two exist for
        # documents that have to stay identifiable or match an older export.
        self._bibliography_options = [
            ("Follow the citation style", "style"),
            ("Follow the style, keep DOI/PMID", "style_with_ids"),
            ("Classic NLM format (all styles)", "nlm"),
        ]
        self.bibliography_combo = QComboBox()
        for label, _ in self._bibliography_options:
            self.bibliography_combo.addItem(label)
        self.bibliography_combo.setCurrentIndex(0)
        self.bibliography_combo.setToolTip(
            "Reference entries follow the chosen style's own rules. Keep DOI/PMID if the "
            "document may later be saved through Google Docs or Pages, which strips the "
            "hidden citation data. Classic NLM matches documents exported by earlier "
            "versions of AI REFs.")
        form.addRow("Reference entries:", self.bibliography_combo)

        # Max refs for (REFS)
        self.max_refs_spin = QSpinBox()
        self.max_refs_spin.setRange(2, 10)
        self.max_refs_spin.setValue(3)
        form.addRow("Max refs for (REFS):", self.max_refs_spin)

        # Author-suggested citation detection
        self.suggested_ids_check = QCheckBox("Verify (PMID: …), (PMC…), and (doi: …) citations")
        self.suggested_ids_check.setChecked(True)
        self.suggested_ids_check.setToolTip(
            "Look up citations you wrote by identifier and score them against the sentence"
        )
        form.addRow("Suggested citations:", self.suggested_ids_check)

        self.author_year_check = QCheckBox("Verify (Author et al. YEAR) citations")
        self.author_year_check.setChecked(True)
        self.author_year_check.setToolTip(
            "Look up author-year citations such as (Battison et al. 2024); "
            "switch off for documents whose author-year citations should stay as they are"
        )
        form.addRow("", self.author_year_check)

        # Recency bias
        self.recency_check = QCheckBox("Prefer more recent publications")
        self.recency_check.setChecked(True)
        form.addRow("Recency bias:", self.recency_check)

        # Review preference
        self.review_check = QCheckBox("Prefer review articles")
        self.review_check.setChecked(False)
        form.addRow("Reviews:", self.review_check)

        # Domain inference
        self.domain_check = QCheckBox("Auto-detect research domain")
        self.domain_check.setChecked(True)
        form.addRow("Domain inference:", self.domain_check)

        # bioRxiv search
        self.biorxiv_check = QCheckBox("Search bioRxiv preprints")
        self.biorxiv_check.setChecked(True)
        form.addRow("bioRxiv:", self.biorxiv_check)

        # Europe PMC search
        self.europepmc_check = QCheckBox("Search Europe PMC (PubMed + PMC + preprints)")
        self.europepmc_check.setChecked(True)
        form.addRow("Europe PMC:", self.europepmc_check)

        # Full text
        self.fulltext_check = QCheckBox("Read open-access full text (Europe PMC) when needed")
        self.fulltext_check.setChecked(True)
        form.addRow("Full text:", self.fulltext_check)

        # Independent verification
        self.verify_check = QCheckBox("Verify each selected paper against its claim")
        self.verify_check.setChecked(True)
        form.addRow("Verification:", self.verify_check)

        # Parallel sentence searches
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 8)
        self.parallel_spin.setValue(3)
        self.parallel_spin.setToolTip(
            "Sentences searched at the same time. More than 1 needs an NCBI API key "
            "(the pipeline falls back to 1 without one).")
        form.addRow("Parallel searches:", self.parallel_spin)

        # Existing-reference enrichment (insert mode)
        self.enrich_check = QCheckBox(
            "Look up existing refs on PubMed to avoid duplicates (insert mode)")
        self.enrich_check.setChecked(True)
        form.addRow("Enrich existing:", self.enrich_check)

        layout.addWidget(settings_group)

        # ── NCBI / ORCID ────────────────────────────────────────────
        api_group = QGroupBox("NCBI & ORCID")
        api_layout = QVBoxLayout(api_group)
        api_layout.setSpacing(10)

        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("your_email@example.com (required for PubMed)")
        api_layout.addLayout(
            self._make_form_row_with_help("NCBI Email:", self.email_edit, "ncbi_email")
        )

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setPlaceholderText("Optional: increases rate limit")
        api_layout.addLayout(
            self._make_form_row_with_help("NCBI API Key:", self.api_key_edit, "ncbi_api_key")
        )

        self.orcid_edit = QLineEdit()
        self.orcid_edit.setPlaceholderText("0000-0000-0000-0000")
        api_layout.addLayout(
            self._make_form_row_with_help("ORCID:", self.orcid_edit, "orcid")
        )

        layout.addWidget(api_group)

        # ── AI Settings ─────────────────────────────────────────────
        ai_group = QGroupBox("AI Settings (Anthropic Claude)")
        ai_layout = QVBoxLayout(ai_group)
        ai_layout.setSpacing(10)

        self.anthropic_key_edit = QLineEdit()
        self.anthropic_key_edit.setPlaceholderText("sk-ant-... (required for AI citation search)")
        self.anthropic_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        ai_layout.addLayout(
            self._make_form_row_with_help("Anthropic API Key:", self.anthropic_key_edit, "anthropic_api_key")
        )

        # Model list: discovered from the Models API (see services.model_catalog),
        # never hard-coded. Combo item data = model id, tooltip = model id.
        self.model_combo = QComboBox()
        self.model_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.refresh_models_btn = QPushButton("\u21bb")
        self.refresh_models_btn.setFixedWidth(32)
        self.refresh_models_btn.setToolTip("Fetch the current list of Claude models for this API key")
        self.refresh_models_btn.clicked.connect(self.request_model_refresh)
        model_row = QWidget()
        model_row_layout = QHBoxLayout(model_row)
        model_row_layout.setContentsMargins(0, 0, 0, 0)
        model_row_layout.setSpacing(4)
        model_row_layout.addWidget(self.model_combo, stretch=1)
        model_row_layout.addWidget(self.refresh_models_btn)
        ai_layout.addLayout(
            self._make_form_row_with_help("Claude Model:", model_row, "claude_model")
        )
        self.model_status_label = QLabel("")
        self.model_status_label.setWordWrap(True)
        self.model_status_label.setStyleSheet("QLabel { color: #666; font-size: 11px; }")
        ai_layout.addWidget(self.model_status_label)

        layout.addWidget(ai_group)

        layout.addStretch()

        scroll.setWidget(form_widget)
        self.splitter.addWidget(scroll)

        # ── RIGHT: Help panel ────────────────────────────────────────
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

        self.help_close_btn = QPushButton("✕")
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

    def _show_help(self, help_key: str):
        """Show the help panel with content for the given key, or toggle off if same key."""
        if self._active_help_key == help_key and self.help_panel.isVisible():
            self._hide_help()
            return

        self._active_help_key = help_key
        content = HELP_TEXTS.get(help_key, "<p>No help available.</p>")
        self.help_content.setHtml(content)

        if not self.help_panel.isVisible():
            self.help_panel.setVisible(True)
            # Set splitter sizes: form gets ~60%, help gets ~40%
            total = self.splitter.width() or 900
            self.splitter.setSizes([int(total * 0.6), int(total * 0.4)])

    def _hide_help(self):
        """Hide the help panel."""
        self._active_help_key = None
        self.help_panel.setVisible(False)

    def _browse_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select DOCX File", "", "Word Documents (*.docx)"
        )
        if path:
            self._on_file_dropped(path)

    def _on_file_dropped(self, path: str):
        self._current_file = path
        self.drop_zone.set_file(path)
        self.file_selected.emit(path)
        logger.info(f"File selected: {path}")

    def get_settings(self) -> ProjectSettings:
        """Collect current settings from the UI and persist them."""
        self._save_settings()
        idx = self.style_combo.currentIndex()
        selected_style = self._style_options[idx][1] if 0 <= idx < len(self._style_options) else CitationStyle.NIH_GRANT
        bib_idx = self.bibliography_combo.currentIndex()
        bibliography_format = (self._bibliography_options[bib_idx][1]
                               if 0 <= bib_idx < len(self._bibliography_options) else "style")
        return ProjectSettings(
            citation_style=selected_style,
            bibliography_format=bibliography_format,
            max_refs_for_refs=self.max_refs_spin.value(),
            detect_suggested_ids=self.suggested_ids_check.isChecked(),
            detect_author_year=self.author_year_check.isChecked(),
            recency_bias=self.recency_check.isChecked(),
            prefer_reviews=self.review_check.isChecked(),
            domain_inference=self.domain_check.isChecked(),
            ncbi_email=self.email_edit.text().strip(),
            ncbi_api_key=self.api_key_edit.text().strip() or None,
            orcid_id=self.orcid_edit.text().strip() or None,
            anthropic_api_key=self.anthropic_key_edit.text().strip() or None,
            claude_model=self._get_model_id(),
            search_biorxiv=self.biorxiv_check.isChecked(),
            search_europepmc=self.europepmc_check.isChecked(),
            enrich_existing_refs=self.enrich_check.isChecked(),
            use_full_text=self.fulltext_check.isChecked(),
            verify_citations=self.verify_check.isChecked(),
            parallel_searches=self.parallel_spin.value(),
        )

    def set_settings(self, settings: ProjectSettings):
        """Apply a ProjectSettings object to the UI."""
        # Citation style
        style_index = 0
        for i, (_, style) in enumerate(self._style_options):
            if style == settings.citation_style:
                style_index = i
                break
        self.style_combo.setCurrentIndex(style_index)
        for i, (_, value) in enumerate(self._bibliography_options):
            if value == settings.bibliography_format:
                self.bibliography_combo.setCurrentIndex(i)
                break

        self.max_refs_spin.setValue(settings.max_refs_for_refs)
        self.set_detection_flags(settings.detect_suggested_ids, settings.detect_author_year)
        self.recency_check.setChecked(settings.recency_bias)
        self.review_check.setChecked(settings.prefer_reviews)
        self.domain_check.setChecked(settings.domain_inference)

        self.email_edit.setText(settings.ncbi_email or "")
        # Sensitive fields are excluded from saved project files; only overwrite
        # when the loaded project actually contains a value.
        if settings.ncbi_api_key:
            self.api_key_edit.setText(settings.ncbi_api_key)
        if settings.anthropic_api_key:
            self.anthropic_key_edit.setText(settings.anthropic_api_key)
        self.orcid_edit.setText(settings.orcid_id or "")

        self._set_model_by_id(settings.claude_model)
        self.biorxiv_check.setChecked(settings.search_biorxiv)
        self.europepmc_check.setChecked(settings.search_europepmc)
        self.enrich_check.setChecked(settings.enrich_existing_refs)
        self.fulltext_check.setChecked(settings.use_full_text)
        self.verify_check.setChecked(settings.verify_citations)
        self.parallel_spin.setValue(settings.parallel_searches)

    # ── Claude model list ─────────────────────────────────────────────

    _LEGACY_MODEL_INDEX = {         # the previous hard-coded combo, by position
        0: "claude-haiku-4-5",
        1: "claude-sonnet-4-6",
        2: "claude-opus-4-8",
    }
    _MISSING_SUFFIX = "  (saved \u2014 not in the current list)"

    def _populate_models_from_cache(self):
        models, source = load_models()
        fetched_text = None
        if source == ModelSource.CACHE:
            _, fetched_at = load_cached_models()
            if fetched_at is not None:
                fetched_text = fetched_at.astimezone().strftime("%Y-%m-%d %H:%M")
        self.set_model_list(models, source, fetched_at_text=fetched_text)

    def _auto_refresh_models(self):
        """Refresh in the background when a key is known and the cache is old."""
        if not self.anthropic_key_edit.text().strip():
            return
        _, fetched_at = load_cached_models()
        if cache_is_stale(fetched_at):
            self.request_model_refresh()

    def set_model_list(self, models: list[ModelInfo], source: ModelSource,
                       fetched_at_text: str | None = None):
        """Fill the combo (newest first) and keep the current selection."""
        previous = self.current_model_id() if self.model_combo.count() else ""
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for m in models:
            self.model_combo.addItem(m.display_name, m.id)
            self.model_combo.setItemData(self.model_combo.count() - 1, m.id, Qt.ItemDataRole.ToolTipRole)
        self.model_combo.blockSignals(False)
        if previous:
            self._set_model_by_id(previous)
        n = len(models)
        if source == ModelSource.LIVE:
            text = f"Model list updated from Anthropic: {n} model{'s' if n != 1 else ''} available to this key."
        elif source == ModelSource.CACHE:
            when = f" (fetched {fetched_at_text})" if fetched_at_text else ""
            text = f"Model list from the last check{when}; click \u21bb to refresh."
        else:
            text = ("Using the built-in model list; enter your Anthropic API key and click "
                    "\u21bb to fetch the current models.")
        self.model_status_label.setText(text)

    def current_model_id(self) -> str:
        data = self.model_combo.currentData()
        if data:
            return str(data)
        return self.model_combo.itemData(0) or NEWEST_MODEL

    def _get_model_id(self) -> str:
        """The selected model id (kept for callers of the old name)."""
        return self.current_model_id()

    def _set_model_by_id(self, model_id: str):
        """Select a model by id; 'newest' (or empty) is the first entry. An id that
        is not on the list (a retired model, or one from another account) is
        appended so the project keeps working, and labelled as such."""
        model_id = (model_id or "").strip()
        if not model_id or model_id == NEWEST_MODEL:
            self.model_combo.setCurrentIndex(0)
            return
        for i in range(self.model_combo.count()):
            if self.model_combo.itemData(i) == model_id:
                self.model_combo.setCurrentIndex(i)
                return
        self.model_combo.addItem(f"{model_id}{self._MISSING_SUFFIX}", model_id)
        self.model_combo.setItemData(self.model_combo.count() - 1, model_id, Qt.ItemDataRole.ToolTipRole)
        self.model_combo.setCurrentIndex(self.model_combo.count() - 1)

    def set_detection_flags(self, detect_ids: bool, detect_author_year: bool):
        """Set the two suggested-citation checkboxes."""
        self.suggested_ids_check.setChecked(bool(detect_ids))
        self.author_year_check.setChecked(bool(detect_author_year))

    def request_model_refresh(self):
        """Fetch the model list for the typed key, off the GUI thread."""
        api_key = self.anthropic_key_edit.text().strip()
        if not api_key:
            self.model_status_label.setText(
                "Enter your Anthropic API key above, then click \u21bb to fetch the model list.")
            return
        if self._refresh_thread is not None and self._refresh_thread.isRunning():
            return
        self.refresh_models_btn.setEnabled(False)
        self.model_status_label.setText("Fetching the model list from Anthropic\u2026")
        self._refresh_thread = _ModelRefreshThread(api_key, self)
        self._refresh_thread.succeeded.connect(self._on_models_refreshed)
        self._refresh_thread.failed.connect(self._on_models_refresh_failed)
        self._refresh_thread.finished.connect(lambda: self.refresh_models_btn.setEnabled(True))
        self._refresh_thread.start()

    def _on_models_refreshed(self, models: object):
        if models:
            self.set_model_list(list(models), ModelSource.LIVE)
        else:
            self.model_status_label.setText(
                "Anthropic returned no Claude models for this key; keeping the current list.")

    def _on_models_refresh_failed(self, reason: str):
        self.model_status_label.setText(
            f"Could not fetch the model list from Anthropic ({reason}); keeping the current list.")

    def wait_for_model_refresh(self, timeout_ms: int = 5000):
        """Block until a running refresh has finished and its result was applied (tests)."""
        from PySide6.QtWidgets import QApplication
        if self._refresh_thread is not None:
            self._refresh_thread.wait(timeout_ms)
        QApplication.processEvents()

    # Banner text and colour per document mode (see MainWindow._document_mode)
    _MODE_STYLES = {
        "ok": ("#e8f5e9", "#a5d6a7", "#2e7d32"),        # green
        "warn": ("#fff8e1", "#ffe082", "#8d6e00"),      # amber
        "error": ("#ffebee", "#ef9a9a", "#b71c1c"),     # red
    }

    def set_document_mode(self, mode: str, report=None, num_existing: int = 0):
        """Show what AI REFs found in the loaded document.

        mode: 'fresh' (no banner), 'legacy' (plain-text citations found),
        'foreign' (another manager's fields; export disabled),
        'analysis-failed' (nothing trusted; export disabled). Later modes:
        'tracked', 'stripped', 'newer-version'.
        """
        first_problem = ""
        if report is not None and getattr(report, "problems", None):
            first_problem = report.problems[0]
        texts = {
            "legacy": (
                f"Insert mode: {num_existing} existing references detected. "
                "Only new (REF)/(REFS) markers will be processed. "
                "Existing citations will be renumbered automatically.", "ok"),
            "tracked": (
                f"Tracked document: {num_existing} references recognised from embedded "
                "AI REFs data. New (REF)/(REFS) markers will be added and everything "
                "renumbered.", "ok"),
            "foreign": (
                "This document contains citation fields from another reference manager "
                "(EndNote/Zotero/Mendeley). AI REFs will not modify it; export is disabled.",
                "warn"),
            "stripped": (
                "This document was exported by AI REFs but its tracking data is gone "
                "(edited in Google Docs or Pages?). Falling back to text-based detection.",
                "warn"),
            "analysis-failed": (
                "AI REFs could not analyse the existing citations"
                + (f": {first_problem}" if first_problem else "")
                + ". Export is disabled.", "error"),
            "newer-version": (
                "This document was created by a newer version of AI REFs; it is opened "
                "read-only. Export is disabled.", "error"),
        }
        if mode not in texts:
            self.insert_mode_label.setVisible(False)
            return
        text, level = texts[mode]
        issues = list(getattr(report, "reconcile", None) or [])
        if issues:
            shown = [f"• {i.message}" for i in issues[:4]]
            if len(issues) > 4:
                shown.append(f"• … and {len(issues) - 4} more")
            text += "\n" + "\n".join(shown)
        bg, border, fg = self._MODE_STYLES[level]
        self.insert_mode_label.setStyleSheet(
            "QLabel {"
            f"  background-color: {bg};"
            f"  border: 1px solid {border};"
            "  border-radius: 6px;"
            "  padding: 8px 12px;"
            f"  color: {fg};"
            "  font-size: 13px;"
            "}"
        )
        self.insert_mode_label.setText(text)
        self.insert_mode_label.setVisible(True)

    # ── Settings persistence ──────────────────────────────────────────

    def _load_saved_settings(self):
        """Restore persisted user settings (API keys, ORCID, model, etc.)."""
        s = QSettings("AIREFs", "AIREFs")
        val = s.value("ncbi_email", "")
        if val:
            self.email_edit.setText(val)
        val = s.value("ncbi_api_key", "")
        if val:
            self.api_key_edit.setText(val)
        val = s.value("orcid_id", "")
        if val:
            self.orcid_edit.setText(val)
        val = s.value("anthropic_api_key", "")
        if val:
            self.anthropic_key_edit.setText(val)
        model_id = s.value("claude_model_id", None)
        if model_id:
            self._set_model_by_id(str(model_id))
        else:
            legacy_idx = s.value("claude_model_index", None)
            if legacy_idx is not None:              # profile from the hard-coded list
                try:
                    migrated = self._LEGACY_MODEL_INDEX.get(int(legacy_idx))
                except (TypeError, ValueError):
                    migrated = None
                if migrated:
                    self._set_model_by_id(migrated)
                    s.setValue("claude_model_id", migrated)
                s.remove("claude_model_index")
                s.sync()
        style_idx = s.value("citation_style_index", None)
        if style_idx is not None:
            self.style_combo.setCurrentIndex(int(style_idx))
        bib_idx = s.value("bibliography_format_index", None)
        if bib_idx is not None:
            self.bibliography_combo.setCurrentIndex(int(bib_idx))
        parallel = s.value("parallel_searches", None)
        if parallel is not None:
            try:
                self.parallel_spin.setValue(int(parallel))
            except (TypeError, ValueError):
                pass
        self.suggested_ids_check.setChecked(s.value("detect_suggested_ids", True, type=bool))
        self.author_year_check.setChecked(s.value("detect_author_year", True, type=bool))
        logger.info("Loaded saved settings")

    def _save_settings(self):
        """Persist current user settings for next session."""
        s = QSettings("AIREFs", "AIREFs")
        s.setValue("ncbi_email", self.email_edit.text().strip())
        s.setValue("ncbi_api_key", self.api_key_edit.text().strip())
        s.setValue("orcid_id", self.orcid_edit.text().strip())
        s.setValue("anthropic_api_key", self.anthropic_key_edit.text().strip())
        s.setValue("claude_model_id", self.current_model_id())
        s.remove("claude_model_index")
        s.setValue("citation_style_index", self.style_combo.currentIndex())
        s.setValue("bibliography_format_index", self.bibliography_combo.currentIndex())
        s.setValue("parallel_searches", self.parallel_spin.value())
        s.setValue("detect_suggested_ids", self.suggested_ids_check.isChecked())
        s.setValue("detect_author_year", self.author_year_check.isChecked())
        s.sync()

    @property
    def current_file(self) -> str:
        return self._current_file or ""
