"""replace_marker_by_regex: locate the marker over text runs, split the touched
runs into before | cite | after, and never touch a run that belongs to a field."""

import pytest
from docx.oxml.ns import qn

from src.services.docx_io import DocxHandler, FieldBoundaryError, W_NS
from tests.fixture_builders import DocBuilder, run_texts, special


def _tags(r_elem) -> list[str]:
    return [c.tag for c in r_elem]


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
    # the untouched w:t of the split run appear once, in their own runs
    assert run_texts(para) == ["Alpha ", "[1]", " omega"]


def test_new_runs_carry_rpr_and_keep_neighbour_children(tmp_path):
    """Formatting travels via w:rPr; the citation run holds nothing but its
    w:t; the neighbours keep the non-text children that were theirs."""
    def build(b):
        p = b.paragraph("")
        r = p.add_run("bold (REF) tail")
        r.font.bold = True
        r._r.append(special("w:lastRenderedPageBreak"))
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    cite = h.replace_marker_by_regex(para, "(REF)", "5", superscript=True)
    assert para.text == "bold 5 tail"
    assert cite is para.runs[1]._r
    before, cite_r, after = (r._r for r in para.runs)
    assert _tags(before) == [qn("w:rPr"), qn("w:t")]
    assert _tags(cite_r) == [qn("w:rPr"), qn("w:t")]
    assert _tags(after) == [qn("w:rPr"), qn("w:t"), qn("w:lastRenderedPageBreak")]
    assert [r.font.bold for r in para.runs] == [True, True, True]
    assert [r.font.superscript for r in para.runs] == [None, True, None]


def test_special_children_of_the_marker_run_survive_in_place(tmp_path):
    """Word packs tabs, soft line breaks, page breaks and symbols into the
    same run as neighbouring text. Splitting that run must keep each of them
    as the element it was -- never flattened into w:t text, never dropped."""
    def build(b):
        p = b.paragraph("")
        b.add_mixed_run(p, ["Aim 1", special("w:tab"), "We show X (REF).",
                            special("w:br"), "Next line",
                            special("w:br", type="page"),
                            special("w:sym", font="Symbol", char="F061")])
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    h.replace_marker_by_regex(para, "(REF)", "[1]")
    assert para.text == "Aim 1\tWe show X [1].\nNext line"
    assert run_texts(para) == ["Aim 1\tWe show X ", "[1]", ".\nNext line"]
    before, cite, after = para._p.findall(qn("w:r"))
    assert _tags(before) == [qn("w:t"), qn("w:tab"), qn("w:t")]
    assert _tags(cite) == [qn("w:t")]
    assert _tags(after) == [qn("w:t"), qn("w:br"), qn("w:t"), qn("w:br"), qn("w:sym")]
    assert after[1].get(qn("w:type")) is None                 # soft line break
    assert after[3].get(qn("w:type")) == "page"
    assert (after[4].get(qn("w:font")), after[4].get(qn("w:char"))) == ("Symbol", "F061")
    for t in para._p.iter(qn("w:t")):                          # no literal controls
        assert "\t" not in t.text and "\n" not in t.text


def test_zero_width_children_at_the_marker_bounds_are_kept(tmp_path):
    """A symbol right before the marker and a page break right after it
    contribute no text, so they sit exactly on the span bounds; they belong
    to the neighbours. The after-run is emitted even when it has no text."""
    def build(b):
        p = b.paragraph("")
        b.add_mixed_run(p, ["Text ", special("w:sym", font="Symbol", char="F061"), "(RE"])
        b.add_mixed_run(p, ["F)", special("w:br", type="page")], superscript=True)
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    h.replace_marker_by_regex(para, "(REF)", "3", superscript=True)
    assert para.text == "Text 3"
    before, cite, after = para._p.findall(qn("w:r"))
    assert _tags(before) == [qn("w:t"), qn("w:sym")]
    assert _tags(cite) == [qn("w:rPr"), qn("w:t")]
    assert _tags(after) == [qn("w:rPr"), qn("w:br")]
    assert after.find(qn("w:rPr")).find(qn("w:vertAlign")) is None   # superscript stripped
    assert [r.font.superscript for r in para.runs] == [None, True, None]


@pytest.mark.parametrize("template_sup, superscript, expected", [
    (True, False, None),     # fresh insertion: superscript iff superscript
    (False, True, True),
    (True, True, True),
    (False, False, None),
    (True, None, True),      # in-place rewrite: keep the marker run's vertAlign
    (False, None, None),
])
def test_citation_vertical_alignment_follows_the_superscript_argument(
        tmp_path, template_sup, superscript, expected):
    """False strips, True sets, None keeps whatever the marker's run had --
    the tri-state renumbering relies on to rewrite an existing token in
    place without changing its look."""
    def build(b):
        p = b.paragraph("")
        p.add_run("x").font.superscript = True
        b.add_text(p, "(REF)", superscript=template_sup)
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    h.replace_marker_by_regex(para, "(REF)", "[2]", superscript=superscript)
    assert [(r.text, r.font.superscript) for r in para.runs] == [("x", True), ("[2]", expected)]


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


def test_replacements_reuse_one_field_index(tmp_path, monkeypatch):
    """_split_and_emit never adds or removes a field run (the guard rejects a
    span touching one first), so the identity-keyed FieldIndex stays valid
    across replacements and is built once -- not once per call, which made a
    300-token renumber (two calls per token) 20x slower. The guard must still
    hold against the retained index after neighbouring edits."""
    import src.services.docx_io as docx_io
    builds = []

    class CountingIndex(docx_io.FieldIndex):
        def __init__(self, doc):
            builds.append(doc)
            super().__init__(doc)
    monkeypatch.setattr(docx_io, "FieldIndex", CountingIndex)

    n_paras = 20

    def build(b):
        p = b.paragraph("Cells")
        b.add_field(p, code=' ADDIN AIREFS.CITE {"a":1} ', result="[F]", superscript=True)
        b.add_text(p, " grow (REF) and divide (REF).")
        for i in range(n_paras - 1):
            b.paragraph(f"Claim {i} (REF) and (REF).")
    h = _handler(tmp_path, build)
    paras = h.get_paragraphs()
    calls = 0
    for para in paras[:n_paras]:
        for k in (1, 2):                       # two calls per paragraph, as renumbering does
            h.replace_marker_by_regex(para, "(REF)", str(k), superscript=True)
            calls += 1
    assert calls == 2 * n_paras
    assert len(builds) == 1
    assert paras[0].text == "Cells[F] grow 1 and divide 2."
    with pytest.raises(FieldBoundaryError):
        h.replace_marker_by_regex(paras[0], "[F]", "9")
    assert len(builds) == 1
    assert 'AIREFS.CITE' in paras[0]._p.xml


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
