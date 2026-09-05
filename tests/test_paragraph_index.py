"""One paragraph index space: parser, marker finder and field walker agree."""
import glob

import pytest

from src.models.sentence import MarkerType, SentenceRecord
from src.pipeline.document_parser import DocumentParser
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder

FIXTURES = sorted(glob.glob("fixtures/*.docx"))
assert FIXTURES, "no fixture DOCX files found"


def test_searched_per_marker_predicate():
    s = SentenceRecord(marker_type=MarkerType.REFS, marker_count=2,
                       marker_types=[MarkerType.REF, MarkerType.REFS])
    assert s.searched_per_marker is True
    s2 = SentenceRecord(marker_type=MarkerType.REFS, marker_count=2,
                        marker_types=[MarkerType.REFS, MarkerType.REFS])
    assert s2.searched_per_marker is False
    s3 = SentenceRecord(marker_type=MarkerType.REF, marker_count=1)
    assert s3.searched_per_marker is False
    # Older projects without marker_types fall back to marker_type
    s4 = SentenceRecord(marker_type=MarkerType.REF, marker_count=2)
    assert s4.searched_per_marker is True


@pytest.mark.parametrize("path", FIXTURES)
def test_parser_and_marker_indices_share_doc_paragraphs_space(path):
    h = DocxHandler(path)
    sentences = DocumentParser(h).parse()
    markers = h.find_markers()
    paras = h.get_paragraphs()
    for s in sentences:
        assert s.raw_text in paras[s.paragraph_index].text
    for m in markers:
        assert paras[m["para_index"]]._p is m["paragraph"]._p   # same w:p element


def test_indices_survive_hyperlinks_and_tracked_changes(tmp_path):
    b = DocBuilder()
    b.paragraph("Intro ")
    b.add_hyperlink(b.paragraph("Link "), "https://x.org", "site")
    p3 = b.paragraph("Claim one (REF). ")
    b.add_text(p3, "inserted", wrap="ins")
    b.paragraph("Claim two (REFS).")
    h = DocxHandler(str(b.save(tmp_path / "i.docx")))
    sentences = [s for s in DocumentParser(h).parse() if "(REF" in s.raw_text]
    markers = h.find_markers()
    assert [s.paragraph_index for s in sentences] == [2, 3]
    assert [m["para_index"] for m in markers] == [2, 3]
