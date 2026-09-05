"""Field-aware scanners: runs inside a Word field are never treated as plain
citation text, and a superscript range ``3-5`` keeps its middle number."""

from src.pipeline.author_date_convert import convert_in_text_to_author_date
from src.pipeline.existing_citation_parser import ExistingCitationParser
from src.pipeline.renumber_apply import apply_renumbering
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder


def _cited_doc(tmp_path, *, with_field=True):
    b = DocBuilder()
    p = b.paragraph("First claim")
    b.add_text(p, "3-5", superscript=True)
    b.add_text(p, ". Second claim [1, 2]. ")
    if with_field:
        b.add_field(p, code=' ADDIN AIREFS.CITE {"a":1} ', result="6", superscript=True)
        b.add_text(p, " and ")
        b.add_field(p, code=' ADDIN AIREFS.CITE {"a":2} ', result="[7]")
    b.paragraph("References")
    for i in range(1, 8):
        b.paragraph(f"{i}. Author{i} A. Title {i}. J. 2020. doi:10.1/x{i}")
    return DocxHandler(str(b.save(tmp_path / "c.docx")))


def test_superscript_range_expands_and_fields_are_skipped(tmp_path):
    h = _cited_doc(tmp_path)
    runs = h.find_superscript_citation_runs()
    assert [r["numbers"] for r in runs] == [[3, 4, 5]]     # field result "6" skipped
    existing = ExistingCitationParser(h).analyze()
    nums = sorted(c.number for c in existing.in_text_citations[0])
    assert nums == [1, 2, 3, 4, 5]                          # 6 and [7] live in fields


def test_skip_fields_false_returns_field_results_too(tmp_path):
    h = _cited_doc(tmp_path)
    runs = h.find_superscript_citation_runs(skip_fields=False)
    assert [r["numbers"] for r in runs] == [[3, 4, 5], [6]]


def test_apply_renumbering_skips_fields_and_maps_ranges(tmp_path):
    h = _cited_doc(tmp_path)
    existing = ExistingCitationParser(h).analyze()
    apply_renumbering(h, existing, {3: 10, 4: 11, 5: 12, 1: 2, 2: 3, 6: 66, 7: 99})
    para = h.get_paragraphs()[0]
    assert para.text == "First claim10-12. Second claim [2, 3]. 6 and [7]"
    assert "AIREFS.CITE" in para._p.xml


def test_bracket_regex_ignores_field_results(tmp_path):
    h = _cited_doc(tmp_path)
    existing = ExistingCitationParser(h).analyze()
    assert all(c.number != 7 for c in existing.in_text_citations[0])


def test_author_date_conversion_skips_fields_and_expands_ranges(tmp_path):
    h = _cited_doc(tmp_path)
    existing = ExistingCitationParser(h).analyze()
    labels = {n: f"L{n}" for n in range(1, 8)}
    n = convert_in_text_to_author_date(h, existing, labels)
    assert n == 2
    para = h.get_paragraphs()[0]
    assert para.text == "First claim(L3; L4; L5). Second claim (L1; L2). 6 and [7]"
    assert "AIREFS.CITE" in para._p.xml
