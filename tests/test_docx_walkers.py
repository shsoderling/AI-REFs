"""Tests for the run walkers in docx_io: the text-run walker must reproduce
``paragraph.text`` exactly; the all-run walker must reach nested runs and
skip mc:Fallback duplicates."""

from pathlib import Path

import pytest
from docx import Document

from src.services.docx_io import iter_text_runs, iter_all_runs, run_text
from tests.fixture_builders import DocBuilder, make_run

FIXTURES = sorted((Path(__file__).resolve().parents[1] / "fixtures").glob("*.docx"))


def _edge_doc(tmp_path):
    b = DocBuilder()
    p = b.paragraph("Plain ")
    b.add_hyperlink(p, "https://x.org", "link")
    b.add_text(p, " then ")
    b.add_field(p, code=' ADDIN AIREFS.CITE {"v":1} ', result="3", superscript=True)
    b.add_text(p, " ins", wrap="ins")
    b.add_text(p, " del", wrap="del")
    b.add_inline_sdt(p, "TAG", " sdt")
    b.add_multi_wt_run(p, ["M1", "M2"])
    return b.save(tmp_path / "edge.docx")


@pytest.mark.parametrize("path", FIXTURES, ids=[p.name for p in FIXTURES])
def test_text_runs_reproduce_paragraph_text_on_fixtures(path):
    for para in Document(str(path)).paragraphs:
        assert "".join(run_text(r) for r in iter_text_runs(para)) == para.text


def test_text_runs_reproduce_paragraph_text_on_edge_doc(tmp_path):
    para = Document(str(_edge_doc(tmp_path))).paragraphs[0]
    texts = [run_text(r) for r in iter_text_runs(para)]
    assert "".join(texts) == para.text
    assert "link" in texts            # hyperlink run is included
    assert " sdt" not in texts        # sdt is invisible to paragraph.text, so also to us
    assert "M1M2" in texts            # multi-w:t run text is concatenated


def test_all_runs_covers_nested_and_marks_deleted(tmp_path):
    para = Document(str(_edge_doc(tmp_path))).paragraphs[0]
    infos = list(iter_all_runs(para))
    texts = [run_text(i.elem) for i in infos]
    assert " ins" in texts and " sdt" in texts and "link" in texts
    deleted = [i for i in infos if i.deleted]
    assert len(deleted) == 1 and deleted[0].elem.xpath("string(w:delText)") == " del"


def test_all_runs_skips_mc_fallback(tmp_path):
    b = DocBuilder()
    p = b.paragraph("Body")

    def fill(inner_p):
        inner_p.append(make_run("boxed"))

    b.add_text_box(p, fill)
    para = Document(str(b.save(tmp_path / "tb.docx"))).paragraphs[0]
    texts = [run_text(i.elem) for i in iter_all_runs(para)]
    assert texts.count("boxed") == 1
