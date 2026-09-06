"""A (REF) marker whose owner chose several papers exports all of them."""

from src.models.evidence import EvidenceRecord, ReviewDecision
from src.models.project import ProjectState
from src.models.sentence import MarkerType, SentenceRecord
from src.pipeline.citation_payload import parse_cite_code
from src.pipeline.docx_export import export_fresh
from src.pipeline.renumber_plan import build_renumber_plan
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder
from tests.test_round_trip import make_citation


def _project(tmp_path):
    text = "Spines remodel during learning (REF)."
    builder = DocBuilder()
    builder.paragraph(text)
    path = builder.save(tmp_path / "in.docx")
    project = ProjectState(input_docx_path=str(path))
    sent = SentenceRecord(id="S001", paragraph_index=0, raw_text=text, clean_text=text,
                          marker_type=MarkerType.REF, marker_count=1, marker_types=[MarkerType.REF])
    project.sentences = [sent]
    project.evidence_map["S001"] = EvidenceRecord(
        sentence_id="S001", selected=[make_citation(1), make_citation(2)],
        review_decision=ReviewDecision.MODIFIED)
    return project


def test_single_ref_marker_with_two_papers_resolves_both(tmp_path):
    project = _project(tmp_path)
    plan = build_renumber_plan(DocxHandler(project.input_docx_path), project)
    assert len(plan.markers) == 1
    assert [c.pmid for c in plan.marker_resolved_map[0]] == ["30000001", "30000002"]


def test_single_ref_marker_with_two_papers_exports_a_two_item_cluster(tmp_path):
    project = _project(tmp_path)
    out = tmp_path / "out.docx"
    stats = export_fresh(project, str(out))
    assert stats.fields_written == 1 and stats.unresolved_fields == 0
    assert stats.bibliography_size == 2 and stats.new_refs_added == 2

    h = DocxHandler(str(out))
    [cite] = [f for f in h.fields.fields if f.kind == "airefs_cite"]
    payload = parse_cite_code(cite.code)
    assert [it.identity["pmid"] for it in payload.items] == ["30000001", "30000002"]
    assert "(REF)" not in h.get_paragraphs()[0].text
