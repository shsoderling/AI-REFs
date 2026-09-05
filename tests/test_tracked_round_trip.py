"""Multi-session round trips through the real export code (Task 17):
fresh export -> edit in "Word" -> reopen -> tracked export."""
import copy

import pytest
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from src.models.embedded import DocumentTier
from src.models.evidence import EvidenceRecord, ReviewDecision
from src.models.project import CitationStyle, ProjectState
from src.models.sentence import MarkerType, SentenceRecord
from src.pipeline.citation_payload import parse_bibl_code, parse_cite_code
from src.pipeline.docx_export import ExportBlocked, export_fresh, export_tracked
from src.pipeline.existing_citation_parser import ExistingCitationParser
from src.services.docx_fields import rewrite_result
from src.services.docx_io import DocxHandler
from tests.test_round_trip import BODY, C1, C2, C3, make_citation, make_project


def _reopen(project, path, style=None, **settings):
    """Second session: a new ProjectState pointing at the exported file, analysed."""
    p2 = ProjectState(settings=copy.deepcopy(project.settings))
    if style is not None:
        p2.settings.citation_style = style
    for k, v in settings.items():
        setattr(p2.settings, k, v)
    p2.input_docx_path = str(path)
    p2.existing_citations = ExistingCitationParser(
        DocxHandler(str(path)), keep_uncited=p2.settings.keep_uncited_entries).analyze()
    p2.is_insert_mode = True
    return p2


def _add_marker(path, para_text, sentence_id, citations, para_idx):
    """Insert a paragraph with a marker at body index para_idx; return sentence + evidence."""
    h = DocxHandler(str(path))
    p = h.doc.add_paragraph(para_text)
    body = h.doc.element.body
    body.remove(p._p)
    body.insert(para_idx, p._p)
    h.save(str(path))
    t = MarkerType.REF if len(citations) == 1 else MarkerType.REFS
    sent = SentenceRecord(id=sentence_id, paragraph_index=para_idx, raw_text=para_text,
                          clean_text=para_text, marker_type=t, marker_count=1, marker_types=[t])
    ev = EvidenceRecord(sentence_id=sentence_id, selected=citations,
                        review_decision=ReviewDecision.ACCEPTED)
    return sent, ev


def _cite_fields(path):
    return [f for f in DocxHandler(str(path)).fields.fields
            if f.kind == "airefs_cite" and not f.deleted]


def _numbers(path):
    return [parse_cite_code(f.code).numbers for f in _cite_fields(path)]


def _results(path):
    return [f.result_text for f in _cite_fields(path)]


def _bib_texts(path):
    h = DocxHandler(str(path))
    f = [f for f in h.fields.fields if f.kind == "airefs_bibl"][0]
    return f.result_text.split("\n")


def _paras(path):
    return [p.text for p in DocxHandler(str(path)).get_paragraphs()]


@pytest.fixture
def session1(tmp_path):
    project = make_project(tmp_path, BODY)
    out = tmp_path / "s1.docx"
    export_fresh(project, str(out))
    return project, out


def test_add_marker_at_top_renumbers_everything(session1, tmp_path):
    project, out = session1
    c9 = make_citation(9)
    sent, ev = _add_marker(out, "New first claim (REF).", "S001", [c9], 0)
    p2 = _reopen(project, out)
    p2.sentences = [sent]
    p2.evidence_map = {sent.id: ev}
    out2 = tmp_path / "s2.docx"
    stats = export_tracked(p2, str(out2))
    assert stats.fields_written == 1 and stats.existing_refs_renumbered == 3
    assert stats.new_refs_added == 1 and stats.bibliography_size == 4
    assert _numbers(out2) == [[1], [2], [3, 4], [3]]
    assert _results(out2) == ["1", "2", "3,4", "3"]
    assert _bib_texts(out2)[0].startswith("1. Author9") and len(_bib_texts(out2)) == 4
    m = ExistingCitationParser(DocxHandler(str(out2))).analyze()
    assert m.tracking.tier == DocumentTier.TRACKED and m.tracking.problems == []
    assert m.tracking.reconcile == []
    assert [m.bib_entries[n].pmid for n in (1, 2, 3, 4)] == ["30000009", "30000001", "30000002", "30000003"]
    assert _paras(out2)[0] == "New first claim 1."


def test_no_new_markers_still_exports_and_is_idempotent(session1, tmp_path):
    project, out = session1
    p2 = _reopen(project, out)
    out2 = tmp_path / "s2.docx"
    stats = export_tracked(p2, str(out2))
    assert stats.fields_written == 0 and stats.existing_refs_renumbered == 0
    assert _numbers(out2) == _numbers(out) and _bib_texts(out2) == _bib_texts(out)
    assert _paras(out2) == _paras(out)


def test_marker_after_references_heading_gets_a_number(session1, tmp_path):
    project, out = session1
    n = len(DocxHandler(str(out)).get_paragraphs())
    sent, ev = _add_marker(out, "Appendix claim (REF).", "S001", [make_citation(7)], n)
    p2 = _reopen(project, out)
    p2.sentences = [sent]
    p2.evidence_map = {sent.id: ev}
    out2 = tmp_path / "s2.docx"
    export_tracked(p2, str(out2))
    assert _numbers(out2)[-1] == [4] and len(_bib_texts(out2)) == 4


def test_delete_sentence_drops_record_and_reports(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out))
    h.doc.element.body.remove(h.get_paragraphs()[0]._p)     # C1's only citation is gone
    h.save(str(out))
    p2 = _reopen(project, out)
    out2 = tmp_path / "s2.docx"
    stats = export_tracked(p2, str(out2))
    assert stats.uncited_dropped == 1 and len(_bib_texts(out2)) == 2
    assert _numbers(out2) == [[1, 2], [1]]
    assert _bib_texts(out2)[0].startswith("1. Author2")


def test_keep_uncited_setting_keeps_entry(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out))
    h.doc.element.body.remove(h.get_paragraphs()[0]._p)
    h.save(str(out))
    p2 = _reopen(project, out, keep_uncited_entries=True)
    out2 = tmp_path / "s2.docx"
    stats = export_tracked(p2, str(out2))
    assert stats.uncited_dropped == 0 and len(_bib_texts(out2)) == 3
    assert _bib_texts(out2)[2].startswith("3. Author1")
    assert parse_bibl_code([f for f in DocxHandler(str(out2)).fields.fields
                            if f.kind == "airefs_bibl"][0].code).uncited == [C1.record_uuid]


def test_deleted_references_section_is_regenerated_identically(session1, tmp_path):
    project, out = session1
    before = _bib_texts(out)
    h = DocxHandler(str(out))
    h.remove_references_section(3, 6)
    h.save(str(out))
    p2 = _reopen(project, out)
    out2 = tmp_path / "s2.docx"
    stats = export_tracked(p2, str(out2))
    assert stats.bibliography_regenerated is True
    assert _bib_texts(out2) == before
    assert "References" in _paras(out2)
    m = ExistingCitationParser(DocxHandler(str(out2))).analyze()
    assert m.references_heading_para_idx == 3 and m.tracking.problems == []


def test_paragraph_reorder_renumbers(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out))
    body = h.doc.element.body
    p0 = h.get_paragraphs()[0]._p
    body.remove(p0)
    body.insert(2, p0)              # first paragraph moves after the second
    h.save(str(out))
    p2 = _reopen(project, out)
    out2 = tmp_path / "s2.docx"
    export_tracked(p2, str(out2))
    assert _numbers(out2) == [[1, 2], [1], [3]]      # moved paragraph now comes last
    assert _bib_texts(out2)[0].startswith("1. Author2") and _bib_texts(out2)[2].startswith("3. Author1")


def test_pasted_duplicate_gets_new_cid_and_same_number(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out))
    f0 = _cite_fields(out)[0]
    for r in f0.all_runs:
        h.get_paragraphs()[2]._p.append(copy.deepcopy(r))
    h.save(str(out))
    p2 = _reopen(project, out)
    out2 = tmp_path / "s2.docx"
    export_tracked(p2, str(out2))
    payloads = [parse_cite_code(f.code) for f in _cite_fields(out2)]
    assert len({p.citation_id for p in payloads}) == 4
    assert [p.numbers for p in payloads] == [[1], [2, 3], [2], [1]]


def test_hand_edited_number_is_overwritten_and_author_date_preserved(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out))
    rewrite_result(_cite_fields(out)[0], "42", superscript=True)
    h_edit = DocxHandler(str(out))
    f = [f for f in h_edit.fields.fields if f.kind == "airefs_cite"][0]
    rewrite_result(f, "42", superscript=True)
    h_edit.save(str(out))
    p2 = _reopen(project, out)
    out2 = tmp_path / "s2.docx"
    stats = export_tracked(p2, str(out2))
    assert stats.hand_edits_overwritten == 1
    assert _results(out2)[0] == "1"

    # author-date document: hand edits survive
    apa = make_project(tmp_path, BODY, CitationStyle.APA, name="apa_in.docx")
    a1 = tmp_path / "apa1.docx"
    export_fresh(apa, str(a1))
    h = DocxHandler(str(a1))
    f = [f for f in h.fields.fields if f.kind == "airefs_cite"][0]
    rewrite_result(f, "(Author1, 2019, p. 5)", superscript=False)
    h.save(str(a1))
    p3 = _reopen(apa, a1)
    a2 = tmp_path / "apa2.docx"
    stats = export_tracked(p3, str(a2))
    assert stats.hand_edits_preserved == 1
    assert _results(a2)[0] == "(Author1, 2019, p. 5)"


def test_edited_bibliography_entry_is_kept_verbatim(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out))
    entry_p = h.get_paragraphs()[5]                        # entry 2
    entry_p.runs[-1].text = entry_p.runs[-1].text + " [corrected volume]"
    h.save(str(out))
    sent, ev = _add_marker(out, "New first claim (REF).", "S001", [make_citation(9)], 0)
    p2 = _reopen(project, out)
    p2.sentences = [sent]
    p2.evidence_map = {sent.id: ev}
    out2 = tmp_path / "s2.docx"
    export_tracked(p2, str(out2))
    texts = _bib_texts(out2)
    assert texts[2].startswith("3. Author2") and texts[2].endswith("[corrected volume]")


def test_content_after_references_survives(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out))
    h.doc.add_paragraph("Appendix A")
    h.doc.add_paragraph("Extra")
    h.save(str(out))
    p2 = _reopen(project, out)
    out2 = tmp_path / "s2.docx"
    export_tracked(p2, str(out2))
    assert _paras(out2)[-2:] == ["Appendix A", "Extra"]
    assert _paras(out2)[3] == "References"


def test_style_switch_numbered_to_apa_and_back_is_idempotent(session1, tmp_path):
    project, out = session1
    p2 = _reopen(project, out, CitationStyle.APA)
    apa = tmp_path / "apa.docx"
    export_tracked(p2, str(apa))
    assert _results(apa)[0] == "(Author1, 2019)"
    assert parse_cite_code(_cite_fields(apa)[0].code).render == "author-date"
    assert 'w:val="superscript"' not in _cite_fields(apa)[0].result_runs[0].xml
    p3 = _reopen(p2, apa, CitationStyle.NIH_GRANT)
    back = tmp_path / "back.docx"
    export_tracked(p3, str(back))
    assert _numbers(back) == _numbers(out) and _bib_texts(back) == _bib_texts(out)
    assert _results(back) == _results(out)


def test_three_sessions_keep_uuids_stable(session1, tmp_path):
    project, out = session1
    uuids = [parse_cite_code(f.code).items[0].record_uuid for f in _cite_fields(out)]
    path = out
    for i in range(3):
        p = _reopen(project, path)
        nxt = tmp_path / f"cycle{i}.docx"
        export_tracked(p, str(nxt))
        path = nxt
    after = [parse_cite_code(f.code).items[0].record_uuid for f in _cite_fields(path)]
    assert after == uuids
    assert ExistingCitationParser(DocxHandler(str(path))).analyze().tracking.reconcile == []


def test_tracked_changes_block_unless_overridden(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out))
    f = [f for f in h.fields.fields if f.kind == "airefs_cite"][0]
    d = OxmlElement("w:del")
    d.set(qn("w:id"), "9")
    d.set(qn("w:author"), "t")
    d.set(qn("w:date"), "2026-01-01T00:00:00Z")
    f.begin.addprevious(d)
    for r in f.all_runs:
        d.append(r)
    h.save(str(out))
    p2 = _reopen(project, out)
    with pytest.raises(ExportBlocked):
        export_tracked(p2, str(tmp_path / "blocked.docx"))
    p2.settings.allow_export_with_tracked_changes = True
    export_tracked(p2, str(tmp_path / "ok.docx"))
    assert _numbers(tmp_path / "ok.docx") == [[1, 2], [1]]      # deleted field is gone


def test_table_field_that_would_change_number_blocks(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out))
    f0 = _cite_fields(out)[0]
    cell = h.doc.add_table(rows=1, cols=1).cell(0, 0).paragraphs[0]
    for r in f0.all_runs:
        cell._p.append(copy.deepcopy(r))
    h.save(str(out))
    # unchanged numbering: allowed, table field left alone and counted
    p2 = _reopen(project, out)
    stats = export_tracked(p2, str(tmp_path / "same.docx"))
    assert stats.tables_citations == 1
    # a new first marker would renumber the table's record 1 -> 2: blocked
    sent, ev = _add_marker(out, "Top (REF).", "S001", [make_citation(9)], 0)
    p3 = _reopen(project, out)
    p3.sentences = [sent]
    p3.evidence_map = {sent.id: ev}
    with pytest.raises(ExportBlocked):
        export_tracked(p3, str(tmp_path / "t.docx"))


def test_unresolved_field_is_kept_until_filled(session1, tmp_path):
    project, out = session1
    sent, ev = _add_marker(out, "Pending (REF).", "S001", [make_citation(9)], 0)
    p2 = _reopen(project, out)
    p2.sentences = [sent]
    p2.evidence_map = {sent.id: ev}
    p2.evidence_map[sent.id].review_decision = ReviewDecision.REJECTED
    out2 = tmp_path / "s2.docx"
    stats = export_tracked(p2, str(out2))
    assert stats.unresolved_fields == 1 and _results(out2)[0] == "[?]"
    m = ExistingCitationParser(DocxHandler(str(out2))).analyze()
    assert [i.kind for i in m.tracking.reconcile] == ["unresolved"]
    assert _numbers(out2)[1:] == [[1], [2, 3], [2]]
