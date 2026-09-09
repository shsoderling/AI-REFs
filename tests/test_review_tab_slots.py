"""Review tab: slot-aware editing of multi-marker sentences (offscreen Qt)."""

import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("QT_QPA_PLATFORM") != "offscreen", reason="needs offscreen Qt")

from src.models.citation import Author, CitationCandidate
from src.models.evidence import EvidenceRecord, ReviewDecision
from src.models.project import ProjectState
from src.models.sentence import SentenceRecord
from src.pipeline.export_slots import collect_marker_slots, ACTION_CITE, ACTION_LEAVE, ACTION_UNRESOLVED
from src.utils.markers import find_markers


def cand(pmid, title=None):
    return CitationCandidate(pmid=pmid, title=title or f"Paper {pmid}", year=2020,
                             authors=[Author(last_name="A")], composite_score=50.0)


def make_project(text, selected, slot_sizes):
    s = SentenceRecord(id="S001", paragraph_index=0, sentence_index=0, raw_text=text, clean_text=text)
    s.markers = find_markers(text)
    s.marker_type = s.markers[0].kind
    s.marker_count = len(s.markers)
    p = ProjectState(sentences=[s])
    p.evidence_map[s.id] = EvidenceRecord(sentence_id=s.id, selected=selected, slot_sizes=slot_sizes)
    return p


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def review_tab(app):
    from src.gui.review_tab import ReviewTab
    tab = ReviewTab()
    yield tab
    tab.close()


def ref_widgets(tab):
    from src.gui.review_tab import SingleRefWidget
    return [w for w in tab._per_ref_widgets if isinstance(w, SingleRefWidget)]


def test_placeholder_goes_into_the_empty_slot(review_tab):
    project = make_project("A (REF) and B (PMID: 1) and C (REF).", [cand("9")], [0, 1, 0])
    review_tab.load_project(project)
    review_tab.sentence_list.setCurrentRow(0)

    ev = project.evidence_map["S001"]
    widgets = ref_widgets(review_tab)
    assert len(widgets) == 3
    assert ev.slot_sizes == [1, 1, 1]
    assert [w.slot_label for w in widgets] == ["(REF)", "(PMID: 1)", "(REF)"]
    assert ev.selected[1].pmid == "9"            # real citation stayed in slot 1
    assert ev.selected[0].pmid == "" and ev.selected[2].pmid == ""  # placeholders around it

    # Placeholders never reach the export
    slots = collect_marker_slots(project)[0]
    assert [x.citations == [] for x in slots] == [True, False, True]


def test_remove_updates_slots_and_widget_indices(review_tab):
    project = make_project("A (REF) and B (REFS).", [cand("1"), cand("2"), cand("3")], [1, 2])
    review_tab.load_project(project)
    review_tab.sentence_list.setCurrentRow(0)
    ev = project.evidence_map["S001"]

    widgets = ref_widgets(review_tab)
    widgets[1]._on_remove()          # remove paper 2 (first of the REFS slot)
    assert [c.pmid for c in ev.selected] == ["1", "3"]
    assert ev.slot_sizes == [1, 1]
    assert [w.index for w in widgets] == [0, -1, 1]  # removed row retired, later row shifted down

    # Replace the remaining REFS row: the replacement must land on that row, not the removed one
    review_tab._replace_ref_index = 1
    review_tab._replace_ref_sentence_id = "S001"
    review_tab._on_single_ref_fetched(cand("77", "Replacement"))
    assert [c.pmid for c in ev.selected] == ["1", "77"]
    assert widgets[2].citation.pmid == "77" and widgets[2]._accepted
    assert widgets[1].citation.pmid == "2"       # removed row untouched

    widgets[0]._on_accept()
    assert ev.review_decision == ReviewDecision.MODIFIED
    slots = collect_marker_slots(project)[0]
    assert [x.action for x in slots] == [ACTION_CITE, ACTION_CITE]
    assert [c.pmid for c in slots[1].citations] == ["77"]


def test_skipped_multi_ref_sentence_can_be_accepted_again(review_tab):
    project = make_project("A (REF) and B (PMID: 2).", [cand("1"), cand("2")], [1, 1])
    review_tab.load_project(project)
    review_tab.sentence_list.setCurrentRow(0)
    ev = project.evidence_map["S001"]

    review_tab._on_skip()
    assert ev.review_decision == ReviewDecision.REJECTED
    review_tab.sentence_list.setCurrentRow(0)
    review_tab._on_sentence_selected(review_tab.sentence_list.item(0), None)
    review_tab._on_accept_all_refs()
    assert ev.review_decision == ReviewDecision.ACCEPTED
    assert [x.action for x in collect_marker_slots(project)[0]] == [ACTION_CITE, ACTION_CITE]


def test_empty_multi_marker_sentence_uses_per_ref_mode(review_tab):
    project = make_project("A (REF) and B (REF).", [], [0, 0])
    review_tab.load_project(project)
    review_tab.sentence_list.setCurrentRow(0)
    widgets = ref_widgets(review_tab)
    assert len(widgets) == 2 and all(not w.accept_btn.isEnabled() for w in widgets)
    ev = project.evidence_map["S001"]
    assert ev.slot_sizes == [1, 1]  # two placeholders, one per marker
    # Accept All must not "keep" placeholders
    review_tab._on_accept_all_refs()
    assert ev.review_decision == ReviewDecision.PENDING


def test_replace_keeps_slot_layout(review_tab):
    project = make_project("A (REF) and B (PMID: 1).", [cand("1"), cand("2")], [1, 1])
    review_tab.load_project(project)
    review_tab.sentence_list.setCurrentRow(0)
    ev = project.evidence_map["S001"]

    review_tab._replace_ref_index = 1
    review_tab._replace_ref_sentence_id = "S001"
    review_tab._on_single_ref_fetched(cand("77", "Replacement"))
    assert [c.pmid for c in ev.selected] == ["1", "77"]
    assert ev.slot_sizes == [1, 1]
    assert ev.candidates[0].pmid == "77"


def test_leave_unchanged_marks_sentence_skipped(review_tab):
    project = make_project("Claim (Smith 2020).", [], [0])
    review_tab.load_project(project)
    review_tab.sentence_list.setCurrentRow(0)
    assert review_tab.skip_btn.isEnabled()
    assert not review_tab.accept_btn.isEnabled()
    assert "could not be confirmed" in review_tab.ref_detail.toPlainText()

    review_tab._on_skip()
    ev = project.evidence_map["S001"]
    assert ev.review_decision == ReviewDecision.REJECTED and ev.is_resolved
    assert project.all_resolved
    assert review_tab.export_btn.isEnabled()
    slots = collect_marker_slots(project)[0]
    assert slots[0].action == ACTION_LEAVE


def test_text_mode_shows_suggested_details_and_highlight(review_tab):
    project = make_project("Spines grow (PMID: 1) here.", [cand("1")], [1])
    ev = project.evidence_map["S001"]
    ev.selected[0].score_rationale = "Author-suggested (PMID 1): directly supports"
    ev.confidence_rationale = "All good"
    review_tab.load_project(project)
    review_tab.sentence_list.setCurrentRow(0)

    detail = review_tab.ref_detail.toPlainText()
    assert "Author-suggested (PMID: 1): PMID 1" in detail
    assert "AI assessment: All good" in detail
    html_text = review_tab.sentence_text.toHtml()
    assert "(PMID: 1)" in html_text
    item = review_tab.sentence_list.item(0)
    assert "(PMID: 1)" in item.text()

    # Filter: author-suggested only keeps this row visible; a (REF) row would be hidden
    review_tab.filter_combo.setCurrentIndex(4)
    assert not item.isHidden()


def test_unresolved_ref_marker_still_gets_question_mark(review_tab):
    project = make_project("Claim (REF).", [], [0])
    review_tab.load_project(project)
    review_tab.sentence_list.setCurrentRow(0)
    slots = collect_marker_slots(project)[0]
    assert slots[0].action == ACTION_UNRESOLVED
