"""Regressions from the Phase 0 review: bounded parsing, in-place rebuild,
field-boundary checks on removal, and guard refinements."""
import pytest

from src.models.embedded import TrackingReport
from src.models.existing_refs import ExistingBibEntry, ExistingCitationMap, InTextCitation
from src.models.project import ProjectSettings
from src.pipeline.docx_export import check_export_guard
from src.pipeline.existing_citation_parser import ExistingCitationParser
from src.services.docx_io import DocxHandler, FieldBoundaryError
from tests.fixture_builders import DocBuilder


def _cited(b):
    p = b.paragraph("Claim ")
    b.add_text(p, "1,2", superscript=True)
    b.paragraph("References")
    b.paragraph("1. Alpha A. Title. J. 2020.")
    b.paragraph("2. Beta B. Title. J. 2021.")


def test_numbered_appendix_after_bibliography_is_not_parsed_as_entries(tmp_path):
    b = DocBuilder()
    _cited(b)
    b.paragraph("Appendix A: Protocol")
    b.paragraph("1. Mix the buffer.")
    b.paragraph("2. Incubate 10 min.")
    b.paragraph("3. Spin.")
    h = DocxHandler(str(b.save(tmp_path / "a.docx")))
    m = ExistingCitationParser(h).analyze()
    assert sorted(m.bib_entries) == [1, 2]
    assert m.bib_entries[1].body.startswith("Alpha") and m.bib_entries[2].body.startswith("Beta")
    assert m.bibliography_span == (2, 3)
    assert m.tracking.problems == []
    h.remove_references_section(m.references_heading_para_idx, m.bibliography_span[1])
    assert [p.text for p in h.get_paragraphs()] == [
        "Claim 1,2", "Appendix A: Protocol", "1. Mix the buffer.", "2. Incubate 10 min.", "3. Spin."]


def test_blank_lines_between_entries_are_inside_the_span(tmp_path):
    b = DocBuilder()
    p = b.paragraph("Claim ")
    b.add_text(p, "1", superscript=True)
    b.paragraph("References")
    b.paragraph("")
    b.paragraph("1. Alpha A. 2020.")
    b.paragraph("")
    b.paragraph("2. Beta B. 2021.")
    b.paragraph("")
    b.paragraph("Acknowledgements")
    m = ExistingCitationParser(DocxHandler(str(b.save(tmp_path / "b.docx")))).analyze()
    assert sorted(m.bib_entries) == [1, 2]
    assert m.bibliography_span == (3, 5)          # trailing blank not included


def test_duplicate_entry_numbers_are_reported_and_block_legacy_export(tmp_path):
    b = DocBuilder()
    _cited(b)
    b.paragraph("2. Gamma G. 2022.")
    m = ExistingCitationParser(DocxHandler(str(b.save(tmp_path / "d.docx")))).analyze()
    assert any("uplicate" in p for p in m.tracking.problems)
    assert check_export_guard(m, "legacy", ProjectSettings())


def test_rebuilt_bibliography_stays_in_place(tmp_path):
    b = DocBuilder()
    b.paragraph("Body")
    b.paragraph("References")
    b.paragraph("1. A. 2020.")
    b.paragraph("Acknowledgements")
    b.paragraph("We thank everyone.")
    h = DocxHandler(str(b.save(tmp_path / "r.docx")))
    h.remove_references_section(1)
    h.append_bibliography(["1. New A. 2020."])
    assert [p.text for p in h.get_paragraphs()] == [
        "Body", "References", "1. New A. 2020.", "Acknowledgements", "We thank everyone."]


def test_append_bibliography_without_prior_removal_goes_to_the_end(tmp_path):
    b = DocBuilder()
    b.paragraph("Body")
    h = DocxHandler(str(b.save(tmp_path / "e.docx")))
    h.append_bibliography(["1. A."])
    assert [p.text for p in h.get_paragraphs()] == ["Body", "References", "1. A."]


def test_removal_refuses_to_cut_through_a_field(tmp_path):
    b = DocBuilder()
    b.paragraph("Body")
    b.paragraph("References")
    p = b.paragraph("")
    b.add_field(p, code=' ADDIN AIREFS.BIBL {"v":1} ', result="1. One",
                end_in_new_paragraph=True, extra_paragraph_texts=["2. Two"])
    b.paragraph("Tail")
    h = DocxHandler(str(b.save(tmp_path / "f.docx")))
    with pytest.raises(FieldBoundaryError):
        h.remove_references_section(1, 2)
    assert h.remove_references_section(1, 3) == 3
    assert [p.text for p in h.get_paragraphs()] == ["Body", "Tail"]


def test_removal_validates_indices(tmp_path):
    b = DocBuilder()
    b.paragraph("Body")
    b.paragraph("References")
    h = DocxHandler(str(b.save(tmp_path / "v.docx")))
    with pytest.raises(ValueError):
        h.remove_references_section(1, 0)
    with pytest.raises(ValueError):
        h.remove_references_section(1, 9)


def _map(n_entries, matched):
    return ExistingCitationMap(
        bib_entries={i: ExistingBibEntry(original_number=i, raw_text=f"{i}. x")
                     for i in range(1, n_entries + 1)},
        references_heading_para_idx=3,
        in_text_citations={0: [InTextCitation(char_offset=k, number=n) for k, n in enumerate(matched)]},
        tracking=TrackingReport())


def test_fresh_append_can_be_confirmed_only_without_parsed_entries():
    heading_only = ExistingCitationMap(references_heading_para_idx=3, tracking=TrackingReport())
    assert check_export_guard(heading_only, "fresh", ProjectSettings())
    assert check_export_guard(heading_only, "fresh", ProjectSettings(), allow_fresh_append=True) == []
    assert check_export_guard(_map(2, [1]), "fresh", ProjectSettings(), allow_fresh_append=True)


def test_ratio_guard_ignores_numbers_without_an_entry():
    m = _map(4, [1, 2, 9, 10, 11])          # 9-11 have no entry: only 2 of 4 matched
    assert check_export_guard(m, "legacy", ProjectSettings(min_match_ratio=0.6))
    assert check_export_guard(m, "legacy", ProjectSettings(min_match_ratio=0.5)) == []
