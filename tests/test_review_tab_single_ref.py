"""Review tab: single-reference sentences get the same Keep / View / Replace widgets as (REFS)."""
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from src.models.citation import Author, CitationCandidate  # noqa: E402
from src.models.evidence import ConfidenceLevel, EvidenceRecord, ReviewDecision  # noqa: E402
from src.models.project import ProjectState  # noqa: E402
from src.models.sentence import MarkerType, SentenceRecord  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication(sys.argv)


def cite(i, abstract=""):
    return CitationCandidate(pmid=f"100{i}", doi=f"10.1/x{i}", title=f"Paper {i}",
                             authors=[Author(last_name=f"Author{i}", initials="A")], year=2020 + i,
                             journal=f"Journal {i}", composite_score=80.0, score_rationale="match",
                             abstract=abstract)


def sentence(sid, marker, text, count=1):
    return SentenceRecord(id=sid, paragraph_index=0, raw_text=text, clean_text=text.replace(f"({marker.value})", "").strip(),
                          marker_type=marker, marker_count=count, marker_types=[marker] * count)


@pytest.fixture
def tab(qapp):
    from src.gui.review_tab import ReviewTab
    project = ProjectState()
    project.settings.reference_library_path = ""          # no SQLite library in tests
    project.sentences = [
        sentence("S001", MarkerType.REF, "Single claim (REF)."),
        sentence("S002", MarkerType.REFS, "Multi claim (REFS)."),
        sentence("S003", MarkerType.REF, "Nothing found (REF)."),
        sentence("S004", MarkerType.REF, "Alpha (REF) and beta (REF).", count=2),
    ]
    project.evidence_map = {
        "S001": EvidenceRecord(sentence_id="S001", selected=[cite(1, abstract="Dendritic spines remodel.")],
                               confidence_level=ConfidenceLevel.HIGH, confidence_score=90,
                               abstract_snippets=["spines remodel during learning"]),
        "S002": EvidenceRecord(sentence_id="S002", selected=[cite(2), cite(3)],
                               confidence_level=ConfidenceLevel.MEDIUM, confidence_score=60),
        "S003": EvidenceRecord(sentence_id="S003", selected=[], confidence_level=ConfidenceLevel.UNRESOLVED),
        "S004": EvidenceRecord(sentence_id="S004", selected=[cite(4), cite(5)],
                               confidence_level=ConfidenceLevel.MEDIUM, confidence_score=60),
    }
    t = ReviewTab()
    t.load_project(project)
    return t


def ref_widgets(tab):
    from src.gui.review_tab import SingleRefWidget
    return [w for w in tab._per_ref_widgets if isinstance(w, SingleRefWidget)]


def accept_all_buttons(tab):
    return [w for w in tab._per_ref_widgets if isinstance(w, QPushButton)]


def test_single_ref_sentence_uses_per_ref_widget(tab):
    tab.sentence_list.setCurrentRow(0)
    assert tab.ref_detail.isHidden() and not tab.per_ref_scroll.isHidden()
    widgets = ref_widgets(tab)
    assert len(widgets) == 1 and widgets[0].citation.title == "Paper 1"
    w = widgets[0]
    assert not w.accept_btn.isHidden() and w.accept_btn.isEnabled()
    assert not w.view_btn.isHidden() and w.view_btn.isEnabled()
    assert not w.replace_btn.isHidden() and w.replace_btn.isEnabled()
    assert w.remove_btn.isHidden()                       # one slot: nothing to remove
    assert accept_all_buttons(tab) == []                 # "Accept All" is for several refs
    assert tab.accept_btn.isHidden() and tab.modify_btn.isHidden()
    assert "Dendritic spines" in w.abstract_label.text()
    snippet_labels = [x for x in tab._per_ref_widgets
                      if isinstance(x, QLabel) and "spines remodel during learning" in x.text()]
    assert len(snippet_labels) == 1


def test_keep_resolves_a_single_ref_sentence(tab):
    tab.sentence_list.setCurrentRow(0)
    ref_widgets(tab)[0].accept_btn.click()
    ev = tab._project.evidence_map["S001"]
    assert ev.review_decision == ReviewDecision.ACCEPTED
    assert [c.title for c in ev.selected] == ["Paper 1"]
    assert tab.sentence_list.currentRow() == 1           # advanced to the next unresolved sentence


def test_replace_via_chat_on_a_single_ref_sentence(tab):
    tab.sentence_list.setCurrentRow(0)
    tab._on_single_ref_chat(0)                           # what the Replace button does
    assert tab._chat_replace_index == 0
    new = cite(9)
    tab._on_chat_citation_selected([new])
    ev = tab._project.evidence_map["S001"]
    assert [c.title for c in ev.selected] == ["Paper 9"]
    assert ev.review_decision == ReviewDecision.MODIFIED
    assert tab.chat_panel.isHidden()


def test_multi_ref_sentence_is_unchanged(tab):
    tab.sentence_list.setCurrentRow(1)
    widgets = ref_widgets(tab)
    assert len(widgets) == 2
    assert all(not w.remove_btn.isHidden() for w in widgets)
    assert len(accept_all_buttons(tab)) == 1


def test_no_candidates_shows_a_placeholder_to_replace(tab):
    tab.sentence_list.setCurrentRow(2)
    assert tab.confidence_label.text() == "No candidates found"
    widgets = ref_widgets(tab)
    assert len(widgets) == 1
    w = widgets[0]
    assert "No citation found" in w.citation.title
    assert not w.accept_btn.isEnabled()                  # nothing to keep
    assert not w.view_btn.isEnabled()
    assert w.replace_btn.isEnabled() and w.remove_btn.isHidden()
    assert tab.ref_detail.isHidden()


def test_verdict_line_and_badge_status_are_shown(tab):
    from src.models.evidence import CitationVerdict, Verdict, VerificationStatus
    ev = tab._project.evidence_map["S001"]
    ev.verdicts = [CitationVerdict(key="1001", verdict=Verdict.SUPPORTS, quote="Dendritic spines remodel",
                                   quote_found=True, source="abstract", reason="direct")]
    ev.verification_status = VerificationStatus.VERIFIED
    tab.sentence_list.setCurrentRow(0)
    w = ref_widgets(tab)[0]
    assert not w.verdict_label.isHidden()
    assert w.verdict_label.text().startswith("Verification: supports — “Dendritic spines remodel”")
    assert "(abstract)" in w.verdict_label.text()
    assert "· verified" in tab.confidence_label.text()

    # Keeping the reference keeps its verdict aligned with the selection
    w.accept_btn.click()
    assert [v.verdict for v in ev.verdicts] == [Verdict.SUPPORTS]

    # A sentence without verdicts shows no verification line
    tab.sentence_list.setCurrentRow(1)
    assert all(x.verdict_label.isHidden() for x in ref_widgets(tab))


def test_choosing_several_papers_in_chat_expands_a_single_ref_marker(tab):
    tab.sentence_list.setCurrentRow(0)
    tab._on_single_ref_chat(0)
    tab._on_chat_citation_selected([cite(9), cite(8), cite(7)])

    ev = tab._project.evidence_map["S001"]
    assert [c.title for c in ev.selected] == ["Paper 9", "Paper 8", "Paper 7"]
    assert ev.review_decision == ReviewDecision.MODIFIED
    assert all(c.score_rationale == "User-selected via chat" for c in ev.selected)
    assert tab.chat_panel.isHidden()

    # Re-opening the sentence shows one card per paper, all decided
    tab.sentence_list.setCurrentRow(1)
    tab.sentence_list.setCurrentRow(0)
    widgets = ref_widgets(tab)
    assert [w.citation.title for w in widgets] == ["Paper 9", "Paper 8", "Paper 7"]
    assert all(w._accepted for w in widgets)
    assert len(accept_all_buttons(tab)) == 1


def test_several_papers_on_a_multi_marker_sentence_fill_only_that_slot(tab):
    tab.sentence_list.setCurrentRow(3)
    tab._on_single_ref_chat(1)
    tab._on_chat_citation_selected([cite(9), cite(8)])

    ev = tab._project.evidence_map["S004"]
    assert ev.review_decision == ReviewDecision.PENDING      # marker 1 still undecided
    assert "2 markers with one reference each" in tab.warnings_label.text()
    assert "1 other selection(s) were not placed" in tab.warnings_label.text()
    widgets = ref_widgets(tab)
    assert [w.citation.title for w in widgets] == ["Paper 4", "Paper 9"]

    widgets[0].accept_btn.click()
    assert [c.title for c in ev.selected] == ["Paper 4", "Paper 9"]
    assert ev.review_decision == ReviewDecision.MODIFIED
