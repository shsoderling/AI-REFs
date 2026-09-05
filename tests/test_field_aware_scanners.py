"""Field-aware legacy scanners (Task 5).

These documents mix plain-text citations with dummy AIREFS fields; they
exercise the plain-text (legacy) scanners directly through _legacy_map(),
because analyze() routes any document carrying our fields to the tracked reader.
"""
from src.pipeline.author_date_convert import convert_in_text_to_author_date
from src.pipeline.existing_citation_parser import ExistingCitationParser
from src.pipeline.renumber_apply import apply_renumbering
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder


def _cited_doc(tmp_path, *, with_field=True, split_range=False):
    b = DocBuilder()
    p = b.paragraph("First claim")
    if split_range:                 # Word split "3-5" at an rsid boundary
        b.add_text(p, "3-", superscript=True)
        b.add_text(p, "5", superscript=True)
    else:
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
    existing = ExistingCitationParser(h)._legacy_map()
    nums = sorted(c.number for c in existing.in_text_citations[0])
    assert nums == [1, 2, 3, 4, 5]                          # 6 and [7] live in fields


def test_skip_fields_false_returns_field_results_too(tmp_path):
    h = _cited_doc(tmp_path)
    runs = h.find_superscript_citation_runs(skip_fields=False)
    assert [r["numbers"] for r in runs] == [[3, 4, 5], [6]]


def test_apply_renumbering_skips_fields_and_maps_ranges(tmp_path):
    h = _cited_doc(tmp_path)
    existing = ExistingCitationParser(h)._legacy_map()
    apply_renumbering(h, existing, {3: 10, 4: 11, 5: 12, 1: 2, 2: 3, 6: 66, 7: 99})
    para = h.get_paragraphs()[0]
    assert para.text == "First claim10-12. Second claim [2, 3]. 6 and [7]"
    assert "AIREFS.CITE" in para._p.xml


def test_split_range_run_is_scanned_and_renumbered(tmp_path):
    """Word may split a superscript "3-5" into "3-" + "5" at an rsid
    boundary. Neither piece is a well-formed range, yet every number written
    must be reported (a dropped 3 would later be seeded as uncited) and the
    range must be renumbered as a whole: 10-12, not 3-12."""
    h = _cited_doc(tmp_path, split_range=True)
    runs = h.find_superscript_citation_runs()
    assert [r["numbers"] for r in runs] == [[3], [5]]      # field result "6" skipped
    existing = ExistingCitationParser(h)._legacy_map()
    sups = [c.number for c in existing.in_text_citations[0] if c.is_superscript]
    assert sups == [3, 5]
    apply_renumbering(h, existing, {3: 10, 4: 11, 5: 12})
    para = h.get_paragraphs()[0]
    assert para.text == "First claim10-12. Second claim [1, 2]. 6 and [7]"
    assert "AIREFS.CITE" in para._p.xml


def test_bracket_regex_ignores_field_results(tmp_path):
    h = _cited_doc(tmp_path)
    existing = ExistingCitationParser(h)._legacy_map()
    assert all(c.number != 7 for c in existing.in_text_citations[0])


def test_bracket_group_straddling_a_field_result_is_left_alone(tmp_path):
    """A bracket match that merely overlaps a field result (here it starts in
    plain text and ends inside the result) is field text: not a citation for
    the parser, and never rewritten by the renumbering writer."""
    b = DocBuilder()
    p = b.paragraph("Claim [1, ")
    b.add_field(p, code=' ADDIN AIREFS.CITE {"a":1} ', result="2]")
    b.add_text(p, " and [3].")
    b.paragraph("References")
    for i in range(1, 4):
        b.paragraph(f"{i}. Author{i} A. Title {i}. J. 2020.")
    h = DocxHandler(str(b.save(tmp_path / "s.docx")))
    existing = ExistingCitationParser(h)._legacy_map()
    assert [c.number for c in existing.in_text_citations[0]] == [3]
    apply_renumbering(h, existing, {1: 7, 2: 8, 3: 9})
    assert h.get_paragraphs()[0].text == "Claim [1, 2] and [9]."


def test_author_date_conversion_skips_fields_and_expands_ranges(tmp_path):
    h = _cited_doc(tmp_path)
    existing = ExistingCitationParser(h)._legacy_map()
    labels = {n: f"L{n}" for n in range(1, 8)}
    n = convert_in_text_to_author_date(h, existing, labels)
    assert n == 2
    para = h.get_paragraphs()[0]
    assert para.text == "First claim(L3; L4; L5). Second claim (L1; L2). 6 and [7]"
    assert "AIREFS.CITE" in para._p.xml
