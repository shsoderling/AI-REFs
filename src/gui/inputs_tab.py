"""Tab 1: Document loading and settings."""

import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QGroupBox, QFormLayout, QLineEdit, QSpinBox,
    QComboBox, QCheckBox, QFrame, QSplitter, QTextEdit, QScrollArea
)
from PySide6.QtCore import Signal, Qt, QSettings
from PySide6.QtGui import QDragEnterEvent, QDropEvent

from ..models.project import ProjectSettings, CitationStyle

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
        "<p>Choose which Claude model performs the citation search. Each model offers a "
        "different balance of speed, cost, and quality.</p>"
        "<p><b>What it does:</b> The selected model reads each marked sentence, decides "
        "what to search for, evaluates candidate papers, and picks the best match. "
        "A smarter model finds better references but costs more and runs slower.</p>"
        "<p><b>Options:</b></p>"
        "<ul>"
        "<li><b>Haiku 4.5</b> &mdash; Fastest and cheapest. Good for straightforward "
        "claims with obvious keywords. Best for quick drafts or budget-conscious use. "
        "~$0.01&ndash;0.05 per document.</li>"
        "<li><b>Sonnet 4.6</b> &mdash; Balanced. Recommended for most use cases. "
        "Better at nuanced claims and multi-step reasoning. "
        "~$0.05&ndash;0.30 per document.</li>"
        "<li><b>Opus 4.8</b> &mdash; Highest quality. Best for complex interdisciplinary "
        "papers or when citation accuracy is critical. "
        "~$0.30&ndash;2.00 per document.</li>"
        "</ul>"
        "<p><i>Tip: Start with Haiku for a quick test run, then re-run with Sonnet or "
        "Opus for your final submission.</i></p>"
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
        "<h3>Insert Mode</h3>"
        "<p>If your document already has numbered citations and a References section, "
        "AI REFs automatically enters <b>insert mode</b>. In this mode:</p>"
        "<ul>"
        "<li>Only new <b>(REF)</b>/<b>(REFS)</b> markers are processed</li>"
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


class InputsTab(QWidget):
    """Tab for document loading and pipeline settings."""
    file_selected = Signal(str)
    settings_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_file = None
        self._help_buttons: list[HelpButton] = []
        self._active_help_key: str | None = None
        self._setup_ui()
        self._load_saved_settings()

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

        # Max refs for (REFS)
        self.max_refs_spin = QSpinBox()
        self.max_refs_spin.setRange(2, 10)
        self.max_refs_spin.setValue(3)
        form.addRow("Max refs for (REFS):", self.max_refs_spin)

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

        self.model_combo = QComboBox()
        self.model_combo.addItems([
            "Haiku 4.5 (Fast, cheapest)",
            "Sonnet 4.6 (Balanced)",
            "Opus 4.8 (Highest quality)",
        ])
        ai_layout.addLayout(
            self._make_form_row_with_help("Claude Model:", self.model_combo, "claude_model")
        )

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
        return ProjectSettings(
            citation_style=selected_style,
            max_refs_for_refs=self.max_refs_spin.value(),
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

        self.max_refs_spin.setValue(settings.max_refs_for_refs)
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

    def _get_model_id(self) -> str:
        """Map combo box index to Anthropic model ID."""
        model_map = {
            0: "claude-haiku-4-5-20251001",
            1: "claude-sonnet-4-6",
            2: "claude-opus-4-8",
        }
        return model_map.get(self.model_combo.currentIndex(), "claude-haiku-4-5-20251001")

    def _set_model_by_id(self, model_id: str):
        """Set model combo box from Anthropic model ID."""
        model_to_index = {
            "claude-haiku-4-5-20251001": 0,
            "claude-sonnet-4-6": 1,
            "claude-opus-4-8": 2,
            # Legacy IDs from older saved projects map to their current tier
            "claude-sonnet-4-5-20250929": 1,
            "claude-opus-4-6": 2,
        }
        self.model_combo.setCurrentIndex(model_to_index.get(model_id, 0))

    def set_insert_mode(self, enabled: bool, num_existing: int):
        """Show or hide the insert-mode indicator."""
        if enabled:
            self.insert_mode_label.setText(
                f"Insert mode: {num_existing} existing references detected. "
                f"Only new (REF)/(REFS) markers will be processed. "
                f"Existing citations will be renumbered automatically."
            )
            self.insert_mode_label.setVisible(True)
        else:
            self.insert_mode_label.setVisible(False)

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
        model_idx = s.value("claude_model_index", None)
        if model_idx is not None:
            self.model_combo.setCurrentIndex(int(model_idx))
        style_idx = s.value("citation_style_index", None)
        if style_idx is not None:
            self.style_combo.setCurrentIndex(int(style_idx))
        logger.info("Loaded saved settings")

    def _save_settings(self):
        """Persist current user settings for next session."""
        s = QSettings("AIREFs", "AIREFs")
        s.setValue("ncbi_email", self.email_edit.text().strip())
        s.setValue("ncbi_api_key", self.api_key_edit.text().strip())
        s.setValue("orcid_id", self.orcid_edit.text().strip())
        s.setValue("anthropic_api_key", self.anthropic_key_edit.text().strip())
        s.setValue("claude_model_index", self.model_combo.currentIndex())
        s.setValue("citation_style_index", self.style_combo.currentIndex())
        s.sync()

    @property
    def current_file(self) -> str:
        return self._current_file or ""
