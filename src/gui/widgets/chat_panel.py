"""Chat panel widget for interactive citation search in the Review tab."""

import re
import html as html_mod
import logging
from typing import Optional

from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QWidget, QLineEdit, QSizePolicy,
)
from PySide6.QtCore import Signal, Slot, Qt, QTimer

from ...models.citation import CitationCandidate
from ...models.project import ProjectSettings
from ...services.model_catalog import resolve_model_id
from ...services.orcid_client import fetch_orcid_name
from .chat_worker import ChatSearchWorker

logger = logging.getLogger(__name__)

# Regex to detect a bare PMID (all digits, 7-8 chars) or DOI
PMID_PATTERN = re.compile(r'^\d{7,8}$')
DOI_PATTERN = re.compile(r'^(https?://(?:dx\.)?doi\.org/)?10\.\d{4,9}/[^\s]+$')


class ChatBubble(QFrame):
    """A single message bubble in the chat."""

    def __init__(self, text: str, is_user: bool, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)

        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setOpenExternalLinks(True)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        if is_user:
            label.setStyleSheet(
                "background-color: #4a90d9; color: white; "
                "border-radius: 12px; padding: 8px 12px; font-size: 12px;"
            )
            layout.addStretch()
            layout.addWidget(label)
            label.setMaximumWidth(350)
        else:
            label.setStyleSheet(
                "background-color: #f0f0f0; color: #333; "
                "border-radius: 12px; padding: 8px 12px; font-size: 12px;"
            )
            layout.addWidget(label)
            layout.addStretch()
            label.setMaximumWidth(400)


class ChatPanel(QFrame):
    """Interactive chat panel for citation search.

    Signals:
        citation_selected: Emitted when the user picks citation(s).
        close_requested: Emitted when user clicks the close button.
    """

    citation_selected = Signal(list)   # list[CitationCandidate]
    close_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings: Optional[ProjectSettings] = None
        self._claim_text: str = ""
        self._sentence_id: str = ""
        self._conversation: list[dict] = []
        self._worker: Optional[ChatSearchWorker] = None
        self._fetch_worker = None
        self._user_first_name: Optional[str] = None
        self._cached_orcid_id: Optional[str] = None   # track which ORCID was looked up
        self._all_candidates: dict[str, CitationCandidate] = {}
        self._pending_direct: Optional[CitationCandidate] = None

        self.setObjectName("chatPanel")
        self.setStyleSheet(
            "QFrame#chatPanel {"
            "  background-color: #fafbfc;"
            "  border-left: 1px solid #ddd;"
            "}"
        )
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # ── Header: title + close button ─────────────────────────
        header_layout = QHBoxLayout()
        self.title_label = QLabel("Citation Assistant")
        self.title_label.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: #2c3e50;"
        )
        header_layout.addWidget(self.title_label)
        header_layout.addStretch()

        close_btn = QPushButton("\u2715")
        close_btn.setFixedSize(24, 24)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; "
            "font-size: 16px; color: #888; }"
            "QPushButton:hover { color: #e74c3c; }"
        )
        close_btn.clicked.connect(self.close_requested.emit)
        header_layout.addWidget(close_btn)
        layout.addLayout(header_layout)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #ddd;")
        layout.addWidget(sep)

        # ── Claim context banner ─────────────────────────────────
        self.claim_banner = QLabel("")
        self.claim_banner.setWordWrap(True)
        self.claim_banner.setStyleSheet(
            "background-color: #fff3cd; color: #856404; "
            "padding: 6px 10px; border-radius: 6px; "
            "font-size: 11px; border: 1px solid #ffc107;"
        )
        layout.addWidget(self.claim_banner)

        # ── Scrollable message area ──────────────────────────────
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
        )

        self.messages_container = QWidget()
        self.messages_layout = QVBoxLayout(self.messages_container)
        self.messages_layout.setContentsMargins(0, 0, 0, 0)
        self.messages_layout.setSpacing(6)
        self.messages_layout.addStretch()

        self.scroll_area.setWidget(self.messages_container)
        layout.addWidget(self.scroll_area, stretch=1)

        # ── Status label ─────────────────────────────────────────
        self.status_label = QLabel("")
        self.status_label.setStyleSheet(
            "color: #888; font-size: 11px; font-style: italic;"
        )
        layout.addWidget(self.status_label)

        # ── Input area ───────────────────────────────────────────
        input_layout = QHBoxLayout()

        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Type a PMID, DOI, or search query...")
        self.input_field.setStyleSheet(
            "padding: 8px; border: 1px solid #ccc; "
            "border-radius: 8px; font-size: 12px;"
        )
        self.input_field.returnPressed.connect(self._on_send)
        input_layout.addWidget(self.input_field)

        self.send_btn = QPushButton("Send")
        self.send_btn.setStyleSheet(
            "background-color: #4a90d9; color: white; "
            "padding: 8px 16px; border-radius: 8px; "
            "font-size: 12px; font-weight: bold;"
        )
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.clicked.connect(self._on_send)
        input_layout.addWidget(self.send_btn)

        layout.addLayout(input_layout)

    # ── Public API ───────────────────────────────────────────────

    def cancel_active_search(self):
        """Cancel any running chat search; do not block the UI.

        The worker checks its flag between rounds and exits without emitting,
        so no stale replies land on a different sentence's context.
        """
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            logger.info("Cancelled active chat search")
        self.status_label.setText("")
        self._enable_input()

    def shutdown_workers(self, wait_ms: int = 3000):
        """Cancel and wait for all workers — call before app shutdown.

        Destroying a running QThread crashes Qt, so this blocks (bounded)
        until the threads exit.
        """
        for worker in (self._worker, self._fetch_worker):
            if worker and worker.isRunning():
                if hasattr(worker, "cancel"):
                    worker.cancel()
                if not worker.wait(wait_ms):
                    logger.warning("Chat worker did not stop in time; terminating")
                    worker.terminate()
                    worker.wait(1000)

    def open_for_sentence(
        self,
        sentence_id: str,
        claim_text: str,
        settings: ProjectSettings,
    ):
        """Initialize the chat panel for a specific sentence."""
        # A search may still be running for the previous sentence
        self.cancel_active_search()
        self._settings = settings
        self._claim_text = claim_text
        self._sentence_id = sentence_id
        self._conversation = []
        self._all_candidates = {}
        self._pending_direct = None

        # Update claim banner
        display_claim = claim_text[:200]
        if len(claim_text) > 200:
            display_claim += "..."
        self.claim_banner.setText(f"Claim: {display_claim}")

        # Clear previous messages
        self._clear_messages()

        # Look up user name from ORCID (re-fetch if ORCID changed)
        current_orcid = (settings.orcid_id or "").strip()
        if current_orcid and current_orcid != self._cached_orcid_id:
            self._user_first_name = fetch_orcid_name(current_orcid)
            self._cached_orcid_id = current_orcid
        elif not current_orcid:
            self._user_first_name = None
            self._cached_orcid_id = None

        self._show_welcome()
        self.input_field.setFocus()

    # ── Welcome message ──────────────────────────────────────────

    def _show_welcome(self):
        name_part = ""
        if self._user_first_name:
            name_part = f" {self._user_first_name}"

        welcome = (
            f"<b>Hi{name_part}!</b> I can help you find a citation "
            f"for this claim.<br><br>"
            f"You can:<br>"
            f"&bull; <b>Paste a PMID or DOI</b> directly and I'll fetch it<br>"
            f"&bull; <b>Describe what you're looking for</b> and I'll "
            f"search PubMed, bioRxiv, and Europe PMC<br><br>"
            f"What would you like to do?"
        )
        self._add_assistant_bubble(welcome)

    # ── Message management ───────────────────────────────────────

    def _clear_messages(self):
        """Remove all message bubbles from the chat."""
        while self.messages_layout.count() > 1:
            item = self.messages_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def _add_user_bubble(self, text: str):
        escaped = html_mod.escape(text)
        bubble = ChatBubble(escaped, is_user=True)
        count = self.messages_layout.count()
        self.messages_layout.insertWidget(count - 1, bubble)
        QTimer.singleShot(50, self._scroll_to_bottom)

    def _add_assistant_bubble(self, html: str):
        bubble = ChatBubble(html, is_user=False)
        count = self.messages_layout.count()
        self.messages_layout.insertWidget(count - 1, bubble)
        QTimer.singleShot(50, self._scroll_to_bottom)

    def _scroll_to_bottom(self):
        vbar = self.scroll_area.verticalScrollBar()
        vbar.setValue(vbar.maximum())

    # ── Input handling ───────────────────────────────────────────

    def _on_send(self):
        text = self.input_field.text().strip()
        if not text:
            return

        self.input_field.clear()
        self._add_user_bubble(text)

        # Check for direct PMID/DOI before engaging Claude
        if self._is_direct_identifier(text):
            self._handle_direct_fetch(text)
        else:
            self._handle_chat_message(text)

    @staticmethod
    def _is_direct_identifier(text: str) -> bool:
        text = text.strip()
        if PMID_PATTERN.match(text):
            return True
        if DOI_PATTERN.match(text):
            return True
        return False

    # ── Direct PMID/DOI fetch (no Claude) ────────────────────────

    def _handle_direct_fetch(self, identifier: str):
        self._add_assistant_bubble(f"Fetching article for <b>{html_mod.escape(identifier)}</b>...")
        self.status_label.setText("Fetching...")
        self._disable_input()

        from ..review_tab import PMIDFetchWorker

        email = self._settings.ncbi_email if self._settings else ""
        api_key = (self._settings.ncbi_api_key or "") if self._settings else ""

        self._fetch_worker = PMIDFetchWorker(identifier, email, api_key, parent=self)
        self._fetch_worker.finished.connect(self._on_direct_fetch_done)
        self._fetch_worker.error.connect(self._on_direct_fetch_error)
        self._fetch_worker.start()

    @Slot(object)
    def _on_direct_fetch_done(self, article: CitationCandidate):
        self.status_label.setText("")
        self._enable_input()

        info = (
            f"Found: <b>{html_mod.escape(article.title)}</b><br>"
            f"{html_mod.escape(article.first_author_year)} | "
            f"{html_mod.escape(article.journal_abbrev or article.journal)}<br>"
        )
        if article.pmid:
            info += f"PMID: {article.pmid} "
        if article.doi:
            info += f"DOI: {article.doi}"
        info += "<br><br>Use this citation? Type <b>yes</b> or <b>no</b>."

        self._add_assistant_bubble(info)
        self._pending_direct = article

    @Slot(str)
    def _on_direct_fetch_error(self, error_msg: str):
        self.status_label.setText("")
        self._enable_input()
        self._add_assistant_bubble(
            f"Could not fetch that identifier:<br>"
            f"<i>{html_mod.escape(error_msg)}</i><br><br>"
            f"Please check the PMID/DOI and try again, or describe "
            f"what you're looking for."
        )
        self._pending_direct = None

    # ── Chat message handling (Claude-powered) ───────────────────

    def _handle_chat_message(self, text: str):
        # Check if user is confirming a direct fetch
        if self._pending_direct is not None:
            lower = text.lower().strip()
            if lower in ("yes", "y", "ok", "sure", "use it", "accept"):
                self._add_assistant_bubble(
                    "Applied! This citation has been set for the current claim."
                )
                self.citation_selected.emit([self._pending_direct])
                self._pending_direct = None
                return
            elif lower in ("no", "n", "cancel", "skip", "nope"):
                self._add_assistant_bubble(
                    "OK, that citation was not applied. "
                    "You can search for another or paste a different PMID/DOI."
                )
                self._pending_direct = None
                return

        self._pending_direct = None

        # Add user message to conversation
        self._conversation.append({"role": "user", "content": text})

        self._disable_input()
        self.status_label.setText("Thinking...")

        if not self._settings or not self._settings.anthropic_api_key:
            self._enable_input()
            self.status_label.setText("")
            self._add_assistant_bubble(
                "No Anthropic API key is configured. "
                "Please set it in the <b>Input</b> tab to enable AI search.<br><br>"
                "You can still paste a PMID or DOI directly."
            )
            return

        self._worker = ChatSearchWorker(
            messages=list(self._conversation),
            claim_text=self._claim_text,
            anthropic_api_key=self._settings.anthropic_api_key,
            model=resolve_model_id(self._settings.claude_model),
            ncbi_email=self._settings.ncbi_email or "",
            ncbi_api_key=self._settings.ncbi_api_key or "",
            search_biorxiv=self._settings.search_biorxiv,
            search_europepmc=self._settings.search_europepmc,
            reference_library_path=self._settings.reference_library_path or "",
            prefer_user_library=self._settings.prefer_user_library,
            max_library_results=self._settings.max_library_results,
            prior_candidates=self._all_candidates,
            parent=self,
            prefer_reviews=self._settings.prefer_reviews,
            recency_bias=self._settings.recency_bias,
        )
        self._worker.status_update.connect(self._on_status)
        self._worker.assistant_message.connect(self._on_assistant_reply)
        self._worker.candidates_found.connect(self._on_candidates)
        self._worker.selection_made.connect(self._on_selection)
        self._worker.error.connect(self._on_chat_error)
        self._worker.finished.connect(self._on_worker_done)
        self._worker.start()

    @Slot(str)
    def _on_status(self, status: str):
        self.status_label.setText(status)

    @Slot(str)
    def _on_assistant_reply(self, text: str):
        self.status_label.setText("")
        self._enable_input()

        html = self._format_assistant_text(text)
        self._add_assistant_bubble(html)

        # Add to conversation for context continuity
        self._conversation.append({"role": "assistant", "content": text})

    @Slot(list)
    def _on_candidates(self, candidates: list):
        for c in candidates:
            key = c.pmid or c.doi or c.title
            if key:
                self._all_candidates[key] = c

    @Slot(list)
    def _on_selection(self, selected: list):
        self.status_label.setText("")
        self._enable_input()

        if selected:
            names = ", ".join(s.title[:50] + "..." for s in selected)
            self._add_assistant_bubble(
                f"Applied! Citation(s) set: <b>{html_mod.escape(names)}</b>"
            )
            self.citation_selected.emit(selected)
        else:
            self._add_assistant_bubble(
                "I couldn't match that selection to a fetched article. "
                "Please try again with a PMID or article number."
            )

    @Slot(str)
    def _on_chat_error(self, error_msg: str):
        self.status_label.setText("")
        self._enable_input()
        self._add_assistant_bubble(
            f"Error: <i>{html_mod.escape(error_msg)}</i><br><br>"
            f"Please try again."
        )

    def _on_worker_done(self):
        self._enable_input()
        self.status_label.setText("")

    # ── Input enable/disable ─────────────────────────────────────

    def _disable_input(self):
        self.input_field.setEnabled(False)
        self.send_btn.setEnabled(False)

    def _enable_input(self):
        self.input_field.setEnabled(True)
        self.send_btn.setEnabled(True)
        self.input_field.setFocus()

    # ── Text formatting ──────────────────────────────────────────

    @staticmethod
    def _format_assistant_text(text: str) -> str:
        """Convert Claude's markdown-style text to simple HTML for bubbles."""
        text = html_mod.escape(text)
        # Bold: **text** -> <b>text</b>
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
        # Italic: *text* -> <i>text</i>  (but not inside bold)
        text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
        # Blockquote: > text -> indented
        text = re.sub(
            r'^&gt; (.+)$',
            r'<div style="margin-left:12px; color:#666; '
            r'border-left: 2px solid #ccc; padding-left: 8px;">\1</div>',
            text,
            flags=re.MULTILINE,
        )
        # Newlines to <br>
        text = text.replace("\n", "<br>")
        return text
