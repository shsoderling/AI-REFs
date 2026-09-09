"""Legacy (plain-text) documents: headless export and adoption into fields (Task 20)."""
import pytest

from src.models.embedded import DocumentTier
from src.models.evidence import EvidenceRecord, ReviewDecision
from src.models.project import CitationStyle, ProjectState
from src.models.sentence import MarkerType, SentenceRecord
from src.pipeline.citation_payload import parse_bibl_code, parse_cite_code
from src.pipeline.docx_export import ExportBlocked, ExportDecisions, export_legacy, export_tracked
from src.pipeline.existing_citation_parser import ExistingCitationParser
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder
from tests.test_round_trip import make_citation


def _legacy_doc(tmp_path, n_entries=3, cite_all=True, brackets=False):
    b = DocBuilder()
    p = b.paragraph("Old claim")
    if brackets:
        b.add_text(p, " [1-3]" if cite_all else " [1]")
    else:
        b.add_text(p, "1-3" if cite_all else "1", superscript=True)
    b.add_text(p, ". New claim (REF).")
    q = b.paragraph("Second old claim")
    if brackets:
        b.add_text(q, " [2]")
    else:
        b.add_text(q, "2", superscript=True)
    b.add_text(q, ".")
    b.paragraph("References")
    for i in range(1, n_entries + 1):
        b.paragraph(f"{i}. Author{i} A. Old title {i}. J Old. 2010;{i}:1-2. doi:10.1/old{i} PMID: 200{i}")
    b.paragraph("Acknowledgements")
    b.paragraph("Thanks.")
    return b.save(tmp_path / "legacy.docx")


def _project(path, citations, style=CitationStyle.NIH_GRANT):
    p = ProjectState(settings={"citation_style": style})
    p.input_docx_path = str(path)
    p.existing_citations = ExistingCitationParser(DocxHandler(str(path))).analyze()
    p.is_insert_mode = True
    sent = SentenceRecord(id="S001", paragraph_index=0, raw_text="New claim (REF).",
                          clean_text="New claim.", marker_type=MarkerType.REF, marker_count=1,
                          marker_types=[MarkerType.REF])
    p.sentences = [sent]
    p.evidence_map = {"S001": EvidenceRecord(sentence_id="S001", selected=citations,
                                             review_decision=ReviewDecision.ACCEPTED)}
    return p


def _paras(path):
    return [p.text for p in DocxHandler(str(path)).get_paragraphs()]


def test_legacy_document_is_adopted_then_reopens_tracked(tmp_path):
    path = _legacy_doc(tmp_path)
    p = _project(path, [make_citation(9)])
    out = tmp_path / "out.docx"
    stats = export_legacy(p, str(out), ExportDecisions())
    assert stats.legacy_adopted == 2 and stats.fields_written == 3
    assert stats.bibliography_size == 4 and stats.new_refs_added == 1
    assert _paras(out)[:2] == ["Old claim1-3. New claim 4.", "Second old claim2."]
    assert _paras(out)[-2:] == ["Acknowledgements", "Thanks."]
    m = ExistingCitationParser(DocxHandler(str(out))).analyze()
    assert m.tracking.tier == DocumentTier.TRACKED and m.tracking.record_count == 4
    assert m.tracking.problems == [] and m.tracking.reconcile == []
    assert [m.bib_entries[n].pmid for n in (1, 2, 3, 4)] == ["2001", "2002", "2003", "30000009"]
    assert m.bib_entries[1].raw_text.startswith("1. Author1 A. Old title 1")   # verbatim
    assert m.bib_entries[1].item["custom"]["airefs"]["rawEntry"].startswith("Author1 A. Old title 1")
    cites = {k: [(c.number, c.source) for c in v] for k, v in m.in_text_citations.items()}
    assert cites == {0: [(1, "field"), (2, "field"), (3, "field"), (4, "field")], 1: [(2, "field")]}


def test_adopted_document_survives_a_tracked_export(tmp_path):
    path = _legacy_doc(tmp_path)
    p = _project(path, [make_citation(9)])
    out = tmp_path / "out.docx"
    export_legacy(p, str(out), ExportDecisions())
    p2 = ProjectState(settings={"citation_style": CitationStyle.NIH_GRANT})
    p2.input_docx_path = str(out)
    p2.existing_citations = ExistingCitationParser(DocxHandler(str(out))).analyze()
    p2.is_insert_mode = True
    out2 = tmp_path / "out2.docx"
    stats = export_tracked(p2, str(out2))
    assert stats.bibliography_size == 4
    assert _paras(out2)[:2] == ["Old claim1-3. New claim 4.", "Second old claim2."]


def test_bracket_document_is_adopted(tmp_path):
    path = _legacy_doc(tmp_path, brackets=True)
    p = _project(path, [make_citation(9)])
    out = tmp_path / "out.docx"
    stats = export_legacy(p, str(out), ExportDecisions())
    assert stats.legacy_adopted == 2 and stats.fields_written == 3
    assert _paras(out)[:2] == ["Old claim [1-3]. New claim [4].", "Second old claim [2]."]
    m = ExistingCitationParser(DocxHandler(str(out))).analyze()
    assert m.tracking.tier == DocumentTier.TRACKED and m.detected_style_is_superscript is False


def test_partial_parse_is_blocked_then_seeded_when_guard_lowered(tmp_path):
    path = _legacy_doc(tmp_path, n_entries=50, cite_all=False)
    p = _project(path, [make_citation(9)])
    with pytest.raises(ExportBlocked):
        export_legacy(p, str(tmp_path / "blocked.docx"), ExportDecisions())
    p.settings.min_match_ratio = 0.0
    out = tmp_path / "out.docx"
    stats = export_legacy(p, str(out), ExportDecisions())
    assert stats.entries_seeded_uncited == 48 and stats.bibliography_size == 51
    b = parse_bibl_code([f for f in DocxHandler(str(out)).fields.fields if f.kind == "airefs_bibl"][0].code)
    assert len(b.uncited) == 48 and len(b.order) == 51


def test_author_date_conversion_writes_no_fields(tmp_path):
    path = _legacy_doc(tmp_path)
    p = _project(path, [make_citation(9)], CitationStyle.APA)
    out = tmp_path / "apa.docx"
    stats = export_legacy(p, str(out), ExportDecisions(convert_to_author_date=True))
    assert stats.legacy_adopted == -1 and stats.citations_converted == 2
    assert DocxHandler(str(out)).fields.airefs_cite == 0
    assert _paras(out)[0].startswith("Old claim(Author1, 2010; Author2, 2010; Author3, 2010). New claim (Author9")


def test_embedding_off_keeps_plain_text_behaviour(tmp_path):
    path = _legacy_doc(tmp_path)
    p = _project(path, [make_citation(9)])
    p.settings.embed_citation_fields = False
    out = tmp_path / "plain.docx"
    stats = export_legacy(p, str(out), ExportDecisions())
    assert stats.legacy_adopted == -1 and DocxHandler(str(out)).fields.airefs_cite == 0
    assert _paras(out)[0] == "Old claim1-3. New claim 4."
    assert "References" in _paras(out) and _paras(out)[-2:] == ["Acknowledgements", "Thanks."]
