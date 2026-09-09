"""export_slots: per-marker actions, plus an offscreen end-to-end DOCX export."""

import os

import pytest
from docx import Document

from src.models.citation import Author, CitationCandidate
from src.models.evidence import EvidenceRecord, ReviewDecision
from src.models.markers import MarkerConfig
from src.models.project import ProjectState, CitationStyle
from src.pipeline.document_parser import DocumentParser
from src.pipeline.export_slots import (
    ACTION_CITE, ACTION_LEAVE, ACTION_UNRESOLVED, PLACEHOLDER_TITLE_PREFIX,
    collect_marker_slots, slot_for_docx_marker, action_for_unmatched, record_action, ExportStats,
)
from src.pipeline.marker_locator import MarkerLocator
from src.services.docx_io import DocxHandler


def cand(pmid, title=None, year=2020):
    return CitationCandidate(pmid=pmid, title=title or f"Paper {pmid}", year=year, journal_abbrev="J",
                             volume="1", pages="1-2", authors=[Author(last_name=f"Author{pmid}", initials="A")])


def build_project(tmp_path, paragraphs):
    path = tmp_path / "in.docx"
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    doc.save(str(path))
    project = ProjectState(input_docx_path=str(path))
    handler = DocxHandler(str(path))
    sentences = DocumentParser(handler, marker_config=project.marker_config).parse()
    MarkerLocator(project.marker_config).locate(sentences)
    project.sentences = sentences
    project.run_marker_config = project.marker_config   # what the orchestrator records
    return project


def test_actions_per_marker_kind(tmp_path):
    project = build_project(tmp_path, [
        "A (REF) B (PMID: 1) C (Smith 2020) D (REFS) E (PMC2000).",
    ])
    (s,) = project.sentences
    assert len(s.markers) == 5
    ev = EvidenceRecord(sentence_id=s.id)
    # slot 0 REF: cited; slot 1 PMID: cited; slot 2 author-year: nothing found;
    # slot 3 REFS: placeholder only; slot 4 PMC: found but user removed it
    ev.selected = [cand("10"), cand("1"), CitationCandidate(title=f"{PLACEHOLDER_TITLE_PREFIX} x)")]
    ev.slot_sizes = [1, 1, 0, 1, 0]
    ev.review_decision = ReviewDecision.ACCEPTED
    project.evidence_map[s.id] = ev

    slots = collect_marker_slots(project)[s.paragraph_index]
    assert [x.action for x in slots] == [ACTION_CITE, ACTION_CITE, ACTION_LEAVE, ACTION_UNRESOLVED, ACTION_LEAVE]
    assert [c.pmid for c in slots[0].citations] == ["10"]
    assert slots[3].citations == []  # placeholder filtered out

    # skipped sentence -> everything left as written
    ev.review_decision = ReviewDecision.REJECTED
    slots = collect_marker_slots(project)[s.paragraph_index]
    assert {x.action for x in slots} == {ACTION_LEAVE}

    # pending sentence -> REF/REFS become [?], suggested stay
    ev.review_decision = ReviewDecision.PENDING
    slots = collect_marker_slots(project)[s.paragraph_index]
    assert [x.action for x in slots] == [ACTION_UNRESOLVED, ACTION_LEAVE, ACTION_LEAVE, ACTION_UNRESOLVED, ACTION_LEAVE]

    assert slot_for_docx_marker({0: slots}, 0, 7) is None
    assert action_for_unmatched("SUGGESTED") == ACTION_LEAVE and action_for_unmatched("REF") == ACTION_UNRESOLVED

    stats = ExportStats()
    record_action(stats, None, ACTION_UNRESOLVED)
    for x in slots:
        record_action(stats, x, x.action)
    assert (stats.unmatched, stats.unresolved, stats.left_unverified) == (1, 2, 3)
    assert "left unverified" in stats.summary()


@pytest.mark.skipif(os.environ.get("QT_QPA_PLATFORM") != "offscreen", reason="needs offscreen Qt")
def test_end_to_end_docx_export(tmp_path):
    """Replace cited markers, write [?] for unresolved (REF), keep unconfirmed suggestions verbatim."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from src.gui.main_window import MainWindow

    project = build_project(tmp_path, [
        "First claim (REF). Second claim (PMID: 1) and third (Smith et al. 2020).",
        "Fourth (REF) fourth-b (REF) fifth (PMC7000000).",
        "Sixth keeps its text (Jones 2019).",
    ])
    project.settings.citation_style = CitationStyle.VANCOUVER
    by_text = {s.raw_text: s for s in project.sentences}

    s1 = by_text["First claim (REF)."]
    project.evidence_map[s1.id] = EvidenceRecord(
        sentence_id=s1.id, selected=[cand("100")], slot_sizes=[1], review_decision=ReviewDecision.ACCEPTED)

    s2 = by_text["Second claim (PMID: 1) and third (Smith et al. 2020)."]
    project.evidence_map[s2.id] = EvidenceRecord(
        sentence_id=s2.id, selected=[cand("1")], slot_sizes=[1, 0], review_decision=ReviewDecision.ACCEPTED)

    s3 = by_text["Fourth (REF) fourth-b (REF) fifth (PMC7000000)."]
    # first REF skipped by the user? No: same sentence; user accepted with slot 0 empty,
    # slot 1 cited, slot 2 cited (same paper as sentence 1 -> reuses number 1)
    project.evidence_map[s3.id] = EvidenceRecord(
        sentence_id=s3.id, selected=[cand("200"), cand("100")], slot_sizes=[0, 1, 1],
        review_decision=ReviewDecision.ACCEPTED)

    s4 = by_text["Sixth keeps its text (Jones 2019)."]
    project.evidence_map[s4.id] = EvidenceRecord(sentence_id=s4.id, review_decision=ReviewDecision.REJECTED)

    win = MainWindow()
    win._project = project
    out = tmp_path / "out.docx"
    stats = win._do_export(str(out))

    paras = [p.text for p in Document(str(out)).paragraphs]
    # Vancouver CSL renders in-text citations as (n)
    assert paras[0] == "First claim (1). Second claim (2) and third (Smith et al. 2020)."
    assert paras[1] == "Fourth [?] fourth-b (3) fifth (1)."
    assert paras[2] == "Sixth keeps its text (Jones 2019)."
    assert "References" in paras
    bib = [p for p in paras if p[:2] in ("1.", "2.", "3.")]
    assert bib[0].startswith("1. Author100 A. Paper 100.")
    assert (stats.cited, stats.unresolved, stats.left_unverified, stats.skipped) == (4, 1, 1, 1)

    # A project saved by the old app version records no run configuration:
    # export then scans with the (REF)/(REFS)-only grammar even though the
    # current settings have detection on, so author-year text is never touched.
    project.run_marker_config = None
    for s in project.sentences:
        s.markers = [m for m in s.markers if m.kind.value in ("REF", "REFS")]
        s.marker_count = len(s.markers)
        s.marker_type = s.markers[0].kind if s.markers else None
    project.evidence_map[s2.id].slot_sizes = []
    stats = win._do_export(str(out))
    paras = [p.text for p in Document(str(out)).paragraphs]
    assert paras[0].startswith("First claim (1). Second claim (PMID: 1)")
    win.close()
