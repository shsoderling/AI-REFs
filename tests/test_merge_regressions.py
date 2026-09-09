"""Regressions found while re-applying the marker feature on the tracked-document line."""

import os

import pytest

from src.models.citation import CitationCandidate
from src.models.evidence import ConfidenceLevel, EvidenceRecord, ReviewDecision
from src.models.markers import MarkerConfig
from src.models.project import CitationStyle, ProjectState
from src.models.sentence import MarkerType, SentenceRecord
from src.pipeline.claim_context import bare_context
from src.pipeline.docx_export import ExportDecisions, export_fresh, export_legacy, export_tracked
from src.pipeline.existing_citation_parser import ExistingCitationParser
from src.pipeline.export_slots import citations_for_marker
from src.pipeline.orchestrator import PipelineOrchestrator
from src.services.docx_io import DocxHandler
from src.storage.project_io import load_project, save_project
from src.utils.markers import find_markers
from tests.fixture_builders import DocBuilder
from tests.test_round_trip import make_citation, make_project
from tests.test_tracked_round_trip import _add_marker, _paras, _reopen, _results


def _legacy_doc(path, brackets=True):
    """[10] is cited first, so renumbering shrinks it to [1] before the (REF)."""
    b = DocBuilder()
    p = b.paragraph("Old claim")
    if brackets:
        b.add_text(p, " [10]")
    else:
        b.add_text(p, "10", superscript=True)
    b.add_text(p, ". New claim (REF).")
    q = b.paragraph("Second old claim")
    if brackets:
        b.add_text(q, " [1-9]")
    else:
        b.add_text(q, "1-9", superscript=True)
    b.add_text(q, ".")
    b.paragraph("References")
    for i in range(1, 11):
        b.paragraph(f"{i}. Author{i} A. Old title {i}. J Old. 2010;{i}:1-2. doi:10.1/old{i} PMID: 200{i}")
    return b.save(path)


def _legacy_project(path, embed=False):
    p = ProjectState(settings={"citation_style": CitationStyle.NIH_GRANT})
    p.settings.embed_citation_fields = embed
    p.input_docx_path = str(path)
    p.existing_citations = ExistingCitationParser(DocxHandler(str(path))).analyze()
    p.is_insert_mode = True
    sent = SentenceRecord(id="S001", paragraph_index=0, raw_text="New claim (REF).",
                          clean_text="New claim.", marker_type=MarkerType.REF, marker_count=1,
                          marker_types=[MarkerType.REF])
    p.sentences = [sent]
    p.evidence_map = {"S001": EvidenceRecord(sentence_id="S001", selected=[make_citation(99)],
                                             slot_sizes=[1], review_decision=ReviewDecision.ACCEPTED)}
    return p


@pytest.mark.parametrize("brackets", [True, False])
@pytest.mark.parametrize("embed", [False, True])
def test_legacy_export_survives_a_shrinking_citation_before_the_marker(tmp_path, brackets, embed):
    path = _legacy_doc(tmp_path / "in.docx", brackets)
    project = _legacy_project(path, embed=embed)
    out = tmp_path / "out.docx"
    stats = export_legacy(project, str(out), ExportDecisions())
    paras = [x.text for x in DocxHandler(str(out)).get_paragraphs()]
    # Document order: [10] becomes 1, the new paper right after it 2, entries 1-9 become 3-11
    assert paras[0] == ("Old claim [1]. New claim [2]." if brackets else "Old claim1. New claim 2.")
    assert stats.resolved_markers == 1 and stats.unresolved_markers == 0


def test_tracked_export_survives_a_shrinking_field_before_the_marker(tmp_path):
    body = [(f"Finding {i} (REF).", [[make_citation(i)]]) for i in range(1, 11)]
    project = make_project(tmp_path, body)
    out = tmp_path / "s1.docx"
    export_fresh(project, str(out))
    # In Word: delete paragraph 1 (entry 1 becomes uncited and is dropped, so
    # "10" becomes "9") and add a (REF) at the end of the paragraph citing 10.
    h = DocxHandler(str(out))
    h.doc.element.body.remove(h.get_paragraphs()[0]._p)
    para = h.get_paragraphs()[8]
    para.add_run(" New claim (REF).")
    h.save(str(out))
    p2 = _reopen(project, out)
    sent = SentenceRecord(id="S001", paragraph_index=8, raw_text=para.text, clean_text=para.text,
                          marker_type=MarkerType.REF, marker_count=1, marker_types=[MarkerType.REF])
    p2.sentences = [sent]
    p2.evidence_map = {"S001": EvidenceRecord(sentence_id="S001", selected=[make_citation(11)],
                                              slot_sizes=[1], review_decision=ReviewDecision.ACCEPTED)}
    out2 = tmp_path / "s2.docx"
    stats = export_tracked(p2, str(out2))
    assert _paras(out2)[8] == "Finding 10 9. New claim 10."
    assert stats.fields_written == 1 and stats.uncited_dropped == 1


def test_identical_marker_texts_are_written_at_their_own_positions(tmp_path):
    b = DocBuilder()
    b.paragraph("A (Smith 2020) and B (Smith 2020) and C (REF).")
    path = b.save(tmp_path / "in.docx")
    project = ProjectState(input_docx_path=str(path))
    project.settings.embed_citation_fields = False
    project.settings.citation_style = CitationStyle.VANCOUVER
    text = "A (Smith 2020) and B (Smith 2020) and C (REF)."
    s = SentenceRecord(id="S001", paragraph_index=0, raw_text=text, clean_text=text)
    s.markers = find_markers(text)
    s.marker_types = [m.kind for m in s.markers]
    s.marker_type = s.markers[0].kind
    s.marker_count = 3
    project.sentences = [s]
    project.run_marker_config = MarkerConfig.all_on()
    # first (Smith 2020) confirmed, second left unverified, (REF) cited
    project.evidence_map["S001"] = EvidenceRecord(
        sentence_id="S001", selected=[make_citation(1), make_citation(2)], slot_sizes=[1, 0, 1],
        review_decision=ReviewDecision.ACCEPTED)
    out = tmp_path / "out.docx"
    stats = export_fresh(project, str(out))
    assert DocxHandler(str(out)).get_paragraphs()[0].text == "A (1) and B (Smith 2020) and C (2)."
    assert stats.left_unverified == 1


def test_suggested_marker_typed_into_a_pending_field_is_filled(tmp_path):
    project = make_project(tmp_path, [("First (REF).", [[make_citation(1)]])])
    out = tmp_path / "s1.docx"
    export_fresh(project, str(out))
    sent, ev = _add_marker(out, "Pending (REF).", "S001", [make_citation(9)], 0)
    ev.review_decision = ReviewDecision.REJECTED          # not accepted: exported as [?]
    p2 = _reopen(project, out)
    p2.sentences = [sent]
    p2.evidence_map = {sent.id: ev}
    pending = tmp_path / "pending.docx"
    export_tracked(p2, str(pending))
    assert _results(pending)[0] == "[?]"
    # The user types an author-suggested citation into the [?] field
    h = DocxHandler(str(pending))
    field = [f for f in h.fields.fields if f.kind == "airefs_cite"][0]
    from src.services.docx_fields import rewrite_result
    rewrite_result(field, "(Smith et al. 2020)", superscript=False)
    h.save(str(pending))
    p3 = _reopen(project, pending)
    raw = "Pending (Smith et al. 2020)."
    s3 = SentenceRecord(id="S001", paragraph_index=0, raw_text=raw, clean_text="Pending.")
    s3.markers = find_markers(raw)
    s3.marker_types = [m.kind for m in s3.markers]
    s3.marker_type = s3.markers[0].kind
    s3.marker_count = 1
    p3.sentences = [s3]
    p3.run_marker_config = MarkerConfig.all_on()
    p3.evidence_map = {"S001": EvidenceRecord(sentence_id="S001", selected=[make_citation(9)],
                                              slot_sizes=[1], review_decision=ReviewDecision.ACCEPTED)}
    out3 = tmp_path / "s3.docx"
    stats = export_tracked(p3, str(out3))
    assert _results(out3) == ["1", "2"] and stats.unresolved_fields == 0
    assert _paras(out3)[0] == "Pending 1."


def test_cancel_mid_sentence_pads_the_slots():
    text = "A (REF) B (REF) C (REF)."
    s = SentenceRecord(id="S001", raw_text=text, clean_text=text)
    s.markers = find_markers(text)
    s.marker_types = [m.kind for m in s.markers]
    s.marker_type = MarkerType.REF
    s.marker_count = 3
    orch = PipelineOrchestrator(ProjectState())

    class Agent:
        max_refs = 3

        def find_citations(self, temp, domains=None, context=None):
            orch.cancel()
            return EvidenceRecord(selected=[CitationCandidate(pmid="1", title="P1")],
                                  confidence_level=ConfidenceLevel.HIGH, confidence_score=80)

    out = orch._find_citations_per_marker(Agent(), s, None, bare_context(s), None)
    assert [c.pmid for c in out.selected] == ["1"]
    assert out.slot_sizes == [1, 0, 0]


def test_run_only_marker_override_is_consumed_and_never_saved(tmp_path):
    project = ProjectState(project_name="p")
    project.run_marker_override = MarkerConfig.legacy()
    assert "run_marker_override" not in project.model_dump()
    path = tmp_path / "p.airefsproj"
    save_project(project, str(path))
    assert load_project(str(path)).run_marker_override is None


@pytest.mark.skipif(os.environ.get("QT_QPA_PLATFORM") != "offscreen", reason="needs offscreen Qt")
def test_review_rebuild_groups_cards_by_marker(tmp_path):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from src.gui.review_tab import ReviewTab

    def cand(pmid):
        return CitationCandidate(pmid=pmid, title=f"Paper {pmid}", year=2020, journal="J")

    text = "Alpha (REF) and beta (REF)."
    s = SentenceRecord(id="S001", paragraph_index=0, raw_text=text, clean_text=text)
    s.markers = find_markers(text)
    s.marker_types = [m.kind for m in s.markers]
    s.marker_type = MarkerType.REF
    s.marker_count = 2
    project = ProjectState(sentences=[s])
    project.settings.reference_library_path = ""
    ev = EvidenceRecord(sentence_id="S001", selected=[cand("A"), cand("B")], slot_sizes=[1, 1])
    project.evidence_map["S001"] = ev
    tab = ReviewTab()
    tab.load_project(project)
    tab.sentence_list.setCurrentRow(0)

    # Replace the FIRST marker's reference with two papers, then keep B
    tab._on_single_ref_chat(0)
    tab._on_chat_citation_selected([cand("A1"), cand("A2")])
    [w for w in tab._ref_widgets() if w.index == 1][0]._on_accept()
    assert [c.pmid for c in ev.selected] == ["A1", "A2", "B"]
    assert ev.slot_sizes == [2, 1]
    assert [c.pmid for c in citations_for_marker(s, ev, 0)] == ["A1", "A2"]
    assert [c.pmid for c in citations_for_marker(s, ev, 1)] == ["B"]

    # Whole-sentence picks on an empty multi-marker sentence fill the markers in order
    ev2 = EvidenceRecord(sentence_id="S001", selected=[], slot_sizes=[0, 0])
    project.evidence_map["S001"] = ev2
    tab.load_project(project)
    tab.sentence_list.setCurrentRow(0)
    tab._on_modify()
    tab._on_chat_citation_selected([cand("X"), cand("Y"), cand("Z")])
    assert [c.pmid for c in ev2.selected] == ["X", "Y", "Z"] and ev2.slot_sizes == [1, 2]
    tab.close()
