"""Round-trip tests through the REAL export code (Task 15).

A fresh export writes an AIREFS.CITE field per marker and an AIREFS.BIBL
field around the bibliography; the visible text still re-parses with the
legacy scanner, and the tracked reader (Task 16) reopens it exactly.
"""
import pytest

from src.models.citation import Author, CitationCandidate
from src.models.evidence import EvidenceRecord, ReviewDecision
from src.models.project import AUTHOR_DATE_STYLES, CitationStyle, ProjectState
from src.models.sentence import MarkerType, SentenceRecord
from src.pipeline.bib_format import format_bib_entry
from src.pipeline.citation_payload import parse_bibl_code, parse_cite_code
from src.pipeline.docx_export import export_fresh
from src.pipeline.existing_citation_parser import ExistingCitationParser
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder


def make_citation(i):
    return CitationCandidate(
        pmid=f"3000000{i}", doi=f"10.1234/test.{i}", title=f"Important Study Number {i}",
        authors=[Author(last_name=f"Author{i}", initials="A")], year=2018 + i,
        journal=f"Journal of Tests {i}", journal_abbrev=f"J Test {i}", volume=str(10 + i),
        pages=f"{i}00-{i}10", source="pubmed")


def make_project(tmp_path, body, style=CitationStyle.NIH_GRANT, name="in.docx"):
    """body: list of (paragraph text with markers, [[citations per marker], ...])."""
    b = DocBuilder()
    project = ProjectState(settings={"citation_style": style})
    sid = 0
    for para_idx, (text, per_marker) in enumerate(body):
        b.paragraph(text)
        if per_marker:
            sid += 1
            types = [MarkerType.REF if len(c) == 1 else MarkerType.REFS for c in per_marker]
            sent = SentenceRecord(id=f"S{sid:03d}", paragraph_index=para_idx, raw_text=text,
                                  clean_text=text, marker_type=types[0], marker_count=len(types),
                                  marker_types=types)
            project.sentences.append(sent)
            if sent.searched_per_marker:
                selected = [group[0] for group in per_marker if group]
            else:
                selected = [c for group in per_marker for c in group]
            project.evidence_map[sent.id] = EvidenceRecord(
                sentence_id=sent.id, selected=selected, review_decision=ReviewDecision.ACCEPTED)
    path = b.save(tmp_path / name)
    project.input_docx_path = str(path)
    return project


C1, C2, C3 = make_citation(1), make_citation(2), make_citation(3)
BODY = [("First finding (REF).", [[C1]]),
        ("Second finding (REFS).", [[C2, C3]]),
        ("Recap (REF).", [[C2]])]


@pytest.mark.parametrize("style", list(CitationStyle))
def test_fresh_export_writes_fields_for_every_style(tmp_path, style):
    project = make_project(tmp_path, BODY, style)
    out = tmp_path / "out.docx"
    stats = export_fresh(project, str(out))
    assert stats.fields_written == 3 and stats.unresolved_fields == 0
    assert stats.bibliography_size == 3 and stats.new_refs_added == 3
    h = DocxHandler(str(out))
    cites = [f for f in h.fields.fields if f.kind == "airefs_cite"]
    bibl = [f for f in h.fields.fields if f.kind == "airefs_bibl"]
    assert len(cites) == 3 and len(bibl) == 1 and all(f.complete for f in cites + bibl)
    payloads = [parse_cite_code(f.code) for f in cites]
    assert [[it.identity["pmid"] for it in p.items] for p in payloads] == [
        ["30000001"], ["30000002", "30000003"], ["30000002"]]
    b = parse_bibl_code(bibl[0].code)
    assert len(b.order) == 3 and b.style == style.value and b.doc_id
    assert len(set(b.order)) == 3 and b.order[0] == payloads[0].items[0].record_uuid or style in AUTHOR_DATE_STYLES
    if style in AUTHOR_DATE_STYLES:
        assert "Author1" in cites[0].result_text and payloads[0].render == "author-date"
        assert payloads[0].numbers == []
    else:
        assert payloads[1].numbers == [2, 3] and payloads[2].numbers == [2]
        assert cites[2].result_text.strip("[]()\xa0") == "2"
    # visible text never contains a field code; the bibliography is styled
    assert "ADDIN" not in "\n".join(p.text for p in h.get_paragraphs())
    assert h.get_paragraphs()[-1].style.name == "AIREFS Bibliography"
    assert h.validate_before_save(expected_cite_fields=3) == []


def test_fresh_export_reparses_with_legacy_parser(tmp_path):
    project = make_project(tmp_path, BODY)
    out = tmp_path / "out.docx"
    export_fresh(project, str(out))
    h = DocxHandler(str(out))
    m = ExistingCitationParser(h)._legacy_map()
    assert sorted(m.bib_entries) == [1, 2, 3]
    assert m.bib_entries[1].pmid == "30000001" and m.bib_entries[3].doi == "10.1234/test.3"
    assert m.references_heading_para_idx == 3 and m.bibliography_span == (4, 6)


def test_record_uuids_are_minted_once_and_shared(tmp_path):
    project = make_project(tmp_path, BODY)
    export_fresh(project, str(tmp_path / "out.docx"))
    assert len(C2.record_uuid) == 36
    h = DocxHandler(str(tmp_path / "out.docx"))
    payloads = [parse_cite_code(f.code) for f in h.fields.fields if f.kind == "airefs_cite"]
    assert payloads[1].items[0].record_uuid == payloads[2].items[0].record_uuid == C2.record_uuid
    assert project.doc_id and project.record_order[:2] == [C1.record_uuid, C2.record_uuid]
    assert project.output_docx_path == str(tmp_path / "out.docx")


def test_unresolved_marker_becomes_unresolved_field(tmp_path):
    project = make_project(tmp_path, [("Claim (REF).", [[C1]]), ("Nothing (REF).", [[C2]])])
    project.evidence_map["S002"].review_decision = ReviewDecision.REJECTED
    out = tmp_path / "out.docx"
    stats = export_fresh(project, str(out))
    assert stats.unresolved_fields == 1 and stats.fields_written == 2
    assert stats.unresolved_markers == 1 and stats.unresolved_sentence_ids == ["S002"]
    h = DocxHandler(str(out))
    unresolved = [f for f in h.fields.fields
                  if f.kind == "airefs_cite" and parse_cite_code(f.code).unresolved]
    assert len(unresolved) == 1 and unresolved[0].result_text == "[?]"
    assert h.get_paragraphs()[1].text == "Nothing [?]."


def test_plain_text_export_when_fields_disabled(tmp_path):
    project = make_project(tmp_path, BODY)
    project.settings.embed_citation_fields = False
    out = tmp_path / "out.docx"
    stats = export_fresh(project, str(out))
    h = DocxHandler(str(out))
    assert h.fields.airefs_cite == 0 and stats.fields_written == 0
    assert "References" in [p.text for p in h.get_paragraphs()]
    assert h.get_paragraphs()[0].text == "First finding 1."


def test_per_marker_sentences_get_one_field_each(tmp_path):
    body = [("A (REF) and B (REFS).", [[C1], [C2, C3]])]
    project = make_project(tmp_path, body)
    out = tmp_path / "out.docx"
    stats = export_fresh(project, str(out))
    assert stats.fields_written == 2
    h = DocxHandler(str(out))
    payloads = [parse_cite_code(f.code) for f in h.fields.fields if f.kind == "airefs_cite"]
    assert [p.numbers for p in payloads] == [[1], [2]]     # per-marker search keeps one item each


def test_format_bib_entry_always_includes_identifiers():
    c = make_citation(1)
    for style in (CitationStyle.NIH_GRANT, CitationStyle.NATURE, CitationStyle.SCIENCE):
        entry = format_bib_entry(c, 1, style)
        assert f"doi:{c.doi}" in entry and f"PMID: {c.pmid}" in entry
