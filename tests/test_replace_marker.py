"""replace_marker_by_regex: locate the marker over text runs, split the touched
runs into before | cite | after, and never touch a run that belongs to a field."""

import pytest
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from src.services.docx_io import DocxHandler, FieldBoundaryError, W_NS
from tests.fixture_builders import DocBuilder


def _handler(tmp_path, build):
    b = DocBuilder()
    build(b)
    return DocxHandler(str(b.save(tmp_path / "m.docx")))


def test_marker_after_hyperlink_is_placed_correctly(tmp_path):
    def build(b):
        p = b.paragraph("See ")
        b.add_hyperlink(p, "https://x.org", "the site")
        b.add_text(p, " for details (REF). More.")
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    h.replace_marker_by_regex(para, "(REF)", "7", superscript=True)
    assert para.text == "See the site for details 7. More."
    sup = [r for r in para.runs if r.font.superscript]
    assert [r.text for r in sup] == ["7"]


def test_marker_spanning_runs_keeps_neighbour_formatting(tmp_path):
    def build(b):
        p = b.paragraph("Claim ")
        b.add_text(p, "(RE")
        b.add_text(p, "F) rest")
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    h.replace_marker_by_regex(para, "(REF)", "[3]")
    assert para.text == "Claim [3] rest"


def test_multi_wt_run_is_not_duplicated(tmp_path):
    def build(b):
        p = b.paragraph("")
        b.add_multi_wt_run(p, ["Alpha ", "(REF)", " omega"])
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    h.replace_marker_by_regex(para, "(REF)", "[1]")
    assert para.text == "Alpha [1] omega"
    # one w:t per new run
    for r in para.runs:
        assert len(r._r.findall(f"{{{W_NS}}}t")) == 1


def test_new_runs_clone_only_rpr(tmp_path):
    """Formatting travels via w:rPr alone; other run children never do."""
    def build(b):
        p = b.paragraph("")
        r = p.add_run("bold (REF) tail")
        r.font.bold = True
        r._r.append(OxmlElement("w:lastRenderedPageBreak"))
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    cite = h.replace_marker_by_regex(para, "(REF)", "5", superscript=True)
    assert para.text == "bold 5 tail"
    assert cite is para.runs[1]._r
    for r in para.runs:
        assert [c.tag for c in r._r] == [qn("w:rPr"), qn("w:t")]
        assert r.font.bold is True
    assert [r.font.superscript for r in para.runs] == [None, True, None]


def test_unsuperscripted_citation_in_superscript_template(tmp_path):
    def build(b):
        p = b.paragraph("")
        p.add_run("x").font.superscript = True
        b.add_text(p, "(REF)", superscript=True)
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    h.replace_marker_by_regex(para, "(REF)", "[2]", superscript=False)
    assert [(r.text, r.font.superscript) for r in para.runs] == [("x", True), ("[2]", None)]


def test_missing_marker_returns_none_and_changes_nothing(tmp_path):
    h = _handler(tmp_path, lambda b: b.paragraph("No marker here."))
    para = h.get_paragraphs()[0]
    before = para._p.xml
    assert h.replace_marker_by_regex(para, "(REF)", "1") is None
    assert para._p.xml == before


def test_refuses_to_touch_a_field_result(tmp_path):
    def build(b):
        p = b.paragraph("Cells ")
        b.add_field(p, code=' ADDIN AIREFS.CITE {"a":1} ', result="(REF)")
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    with pytest.raises(FieldBoundaryError):
        h.replace_marker_by_regex(para, "(REF)", "1")


def test_works_adjacent_to_a_field(tmp_path):
    def build(b):
        p = b.paragraph("Cells")
        b.add_field(p, code=' ADDIN AIREFS.CITE {"a":1} ', result="1", superscript=True)
        b.add_text(p, " and more (REF).")
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    h.replace_marker_by_regex(para, "(REF)", "2", superscript=True)
    assert para.text == "Cells1 and more 2."
    # begin/separate/end untouched
    fldchars = para._p.findall(f".//{{{W_NS}}}fldChar")
    assert [fc.get(f"{{{W_NS}}}fldCharType") for fc in fldchars] == ["begin", "separate", "end"]
    assert 'AIREFS.CITE' in para._p.xml


def test_never_clears_paragraph_content(tmp_path, monkeypatch):
    from docx.oxml.text.paragraph import CT_P
    called = []
    orig = CT_P.clear_content
    monkeypatch.setattr(CT_P, "clear_content", lambda self: called.append(1) or orig(self))

    def build(b):
        p = b.paragraph("")
        b.add_hyperlink(p, "https://x.org", "only a link (REF)")
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    h.replace_marker_by_regex(para, "(REF)", "[4]")
    assert called == []
    assert para.text == "only a link [4]"


def test_collapse_refuses_fielded_paragraph(tmp_path):
    def build(b):
        p = b.paragraph("A ")
        b.add_field(p, code=' ADDIN AIREFS.CITE {"a":1} ', result="1")
        b.add_text(p, " (REF)")
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    with pytest.raises(FieldBoundaryError):
        h._collapse_and_replace_superscript(para, "(REF)", "9")
