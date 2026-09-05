"""Writing AIREFS citation and bibliography fields (Task 14)."""
import pytest

from src.pipeline.citation_payload import build_bibl_code, parse_cite_code
from src.services.docx_fields import rewrite_code, rewrite_result
from src.services.docx_io import DocxHandler, FieldBoundaryError
from tests.fixture_builders import DocBuilder, make_run

CODE = ' ADDIN AIREFS.CITE {"citationID":"abcdefghijkl","citationItems":[],"airefs":{"v":1,"numbers":[3]}} '


def _handler(tmp_path, build):
    b = DocBuilder()
    build(b)
    return DocxHandler(str(b.save(tmp_path / "w.docx")))


def test_insert_citation_field_replaces_marker(tmp_path):
    h = _handler(tmp_path, lambda b: b.paragraph("Neurons fire (REF). Then"))
    para = h.get_paragraphs()[0]
    h.insert_citation_field(para, "(REF)", CODE, "3", superscript=True)
    assert para.text == "Neurons fire 3. Then"
    f = h.fields.fields[0]
    assert f.kind == "airefs_cite" and f.complete and f.result_text == "3"
    assert parse_cite_code(f.code).numbers == [3]
    xml = para._p.xml
    assert 'w:fldLock="1"' in xml and "<w:noProof/>" in xml and 'w:val="superscript"' in xml
    # rPr children in schema order: noProof before vertAlign
    assert xml.index("<w:noProof/>") < xml.index('w:val="superscript"')


def test_insert_field_after_hyperlink_and_round_trip_to_disk(tmp_path):
    def build(b):
        p = b.paragraph("See ")
        b.add_hyperlink(p, "https://x.org", "site")
        b.add_text(p, " and (REFS).")
    h = _handler(tmp_path, build)
    h.insert_citation_field(h.get_paragraphs()[0], "(REFS)", CODE, "[3]", superscript=False)
    out = tmp_path / "out.docx"
    h.save(str(out))
    h2 = DocxHandler(str(out))
    assert h2.get_paragraphs()[0].text == "See site and [3]."
    assert h2.fields.airefs_cite == 1
    assert 'w:val="superscript"' not in h2.get_paragraphs()[0]._p.xml


def test_insert_refuses_marker_inside_a_field(tmp_path):
    def build(b):
        p = b.paragraph("x")
        b.add_field(p, code=CODE, result="(REF)")
    h = _handler(tmp_path, build)
    with pytest.raises(FieldBoundaryError):
        h.insert_citation_field(h.get_paragraphs()[0], "(REF)", CODE, "1", superscript=False)


def test_bibliography_field_write_and_replace(tmp_path):
    h = _handler(tmp_path, lambda b: (b.paragraph("Body"), b.paragraph("Appendix")))
    code = build_bibl_code(doc_id="d", style="nih_grant", render="numeric-superscript",
                           heading_text="References", order=["u1", "u2"], uncited=[],
                           entry_hashes=["a", "b"])
    h.write_bibliography_field(["1. One", "2. Two"], code)
    paras = h.get_paragraphs()
    assert [p.text for p in paras] == ["Body", "Appendix", "References", "1. One", "2. Two"]
    assert paras[2].style.name == "AIREFS Bibliography Heading"
    assert paras[3].style.name == "AIREFS Bibliography"
    f = [f for f in h.fields.fields if f.kind == "airefs_bibl"][0]
    assert f.complete and f.result_text == "1. One\n2. Two" and len(f.paragraphs) == 2
    assert h.locate_bibliography_heading(f, "References") == 2

    start = h.replace_bibliography_field(f, ["1. Uno", "2. Dos", "3. Tres"], code)
    assert start == 3
    paras = h.get_paragraphs()
    assert [p.text for p in paras] == ["Body", "Appendix", "References", "1. Uno", "2. Dos", "3. Tres"]
    f2 = [f for f in h.fields.fields if f.kind == "airefs_bibl"][0]
    assert f2.complete and len(f2.paragraphs) == 3 and h.fields.airefs_bibl == 1


def test_replace_keeps_content_after_the_field(tmp_path):
    h = _handler(tmp_path, lambda b: b.paragraph("Body"))
    code = build_bibl_code(doc_id="d", style="x", render="numeric-bracket", heading_text="References",
                           order=["u1"], uncited=[], entry_hashes=["a"])
    h.write_bibliography_field(["1. Only"], code)
    h.doc.add_paragraph("Appendix A")
    h.invalidate_fields()
    f = [f for f in h.fields.fields if f.kind == "airefs_bibl"][0]
    h.replace_bibliography_field(f, ["1. Uno", "2. Dos"], code)
    assert [p.text for p in h.get_paragraphs()] == ["Body", "References", "1. Uno", "2. Dos", "Appendix A"]


def test_single_entry_bibliography_and_heading_rule(tmp_path):
    h = _handler(tmp_path, lambda b: b.paragraph("Body"))
    code = build_bibl_code(doc_id="d", style="x", render="numeric-bracket",
                           heading_text="Literature Cited", order=["u1"], uncited=[], entry_hashes=["a"])
    h.write_bibliography_field(["1. Only"], code, heading_text="Literature Cited")
    f = [f for f in h.fields.fields if f.kind == "airefs_bibl"][0]
    assert f.complete and f.result_text == "1. Only" and len(f.paragraphs) == 1
    # user renamed the heading: the positional rule still finds it
    h.get_paragraphs()[1].runs[0].text = "REFERENCES CITED"
    assert h.locate_bibliography_heading(f, "Literature Cited") == 1
    # entry-shaped paragraph directly above the field: positional rule fails,
    # the styled paragraph above it is found instead
    h.get_paragraphs()[1].runs[0].text = "0. Not a heading"
    h.get_paragraphs()[1].style = h.doc.styles["Normal"]
    assert h.locate_bibliography_heading(f, "Literature Cited") == -1
    body = h.doc.element.body
    new_p = h.doc.add_paragraph("Literature Cited")
    body.remove(new_p._p)
    h.get_paragraphs()[1]._p.addprevious(new_p._p)
    assert h.locate_bibliography_heading(f, "Literature Cited") == 1   # text match, one above


def test_rewrite_result_collapses_split_runs_and_rewrite_code(tmp_path):
    def build(b):
        p = b.paragraph("x")
        runs = b.add_field(p, code=CODE, result="1", superscript=True)
        runs[-1].addprevious(make_run("2", superscript=True))   # Word split the result into "1","2"
    h = _handler(tmp_path, build)
    f = h.fields.fields[0]
    assert len(f.result_runs) == 2
    rewrite_result(f, "7,8", superscript=True)
    rewrite_code(f, CODE.replace("[3]", "[7,8]"))
    h.invalidate_fields()
    f = h.fields.fields[0]
    assert f.result_text == "7,8" and len(f.result_runs) == 1
    assert parse_cite_code(f.code).numbers == [7, 8]
    assert 'w:val="superscript"' in f.result_runs[0].xml


def test_rewrite_result_can_drop_superscript(tmp_path):
    def build(b):
        p = b.paragraph("x")
        b.add_field(p, code=CODE, result="1", superscript=True)
    h = _handler(tmp_path, build)
    f = h.fields.fields[0]
    rewrite_result(f, "(Smith, 2020)", superscript=False)
    assert 'w:val="superscript"' not in f.result_runs[0].xml
    assert f.result_text == "(Smith, 2020)"


def test_validate_before_save_catches_leaks_and_counts(tmp_path):
    h = _handler(tmp_path, lambda b: b.paragraph("Body (REF)"))
    h.insert_citation_field(h.get_paragraphs()[0], "(REF)", CODE, "1", superscript=False)
    assert h.validate_before_save(expected_cite_fields=1) == []
    assert h.validate_before_save(expected_cite_fields=2)
    h.get_paragraphs()[0].add_run(" ADDIN AIREFS.CITE leaked")
    assert any("visible text" in p for p in h.validate_before_save())


def test_validate_before_save_catches_incomplete_and_unparsable(tmp_path):
    def build(b):
        p = b.paragraph("x")
        p._p.append(make_run(fldchar="begin"))
        p._p.append(make_run(instr=" ADDIN AIREFS.CITE {} "))
        q = b.paragraph("y")
        b.add_field(q, code=" ADDIN AIREFS.CITE not-json ", result="1")
    h = _handler(tmp_path, build)
    problems = h.validate_before_save()
    assert any("incomplete" in p for p in problems) and any("parse" in p for p in problems)
