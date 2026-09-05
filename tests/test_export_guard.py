"""Export hard guard and bounded References-section removal (Task 8)."""
import pytest

from src.models.embedded import DocumentTier, TrackingReport
from src.models.existing_refs import ExistingBibEntry, ExistingCitationMap, InTextCitation
from src.models.project import ProjectSettings
from src.pipeline.docx_export import check_export_guard
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder


def _map(n_entries, matched_numbers, heading=3, tier=DocumentTier.LEGACY, foreign=0, tracked=False):
    return ExistingCitationMap(
        bib_entries={i: ExistingBibEntry(original_number=i, raw_text=f"{i}. x")
                     for i in range(1, n_entries + 1)},
        references_heading_para_idx=heading,
        in_text_citations={0: [InTextCitation(char_offset=k, number=n)
                               for k, n in enumerate(matched_numbers)]},
        tracking=TrackingReport(tier=tier, foreign_field_count=foreign),
        pending_tracked_changes=tracked,
    )


def test_fresh_export_blocked_when_bibliography_exists():
    reasons = check_export_guard(_map(2, [1]), "fresh", ProjectSettings())
    assert any("already has a References" in r for r in reasons)


def test_fresh_export_allowed_on_uncited_document():
    m = ExistingCitationMap(tracking=TrackingReport())
    assert check_export_guard(m, "fresh", ProjectSettings()) == []
    assert check_export_guard(None, "fresh", ProjectSettings()) == []


@pytest.mark.parametrize("n,matched,blocked", [
    (50, [1, 2, 3], True), (10, [], True), (10, [1, 2, 3, 4, 5], False), (4, [1, 2, 3, 4], False),
])
def test_ratio_guard(n, matched, blocked):
    reasons = check_export_guard(_map(n, matched), "legacy", ProjectSettings())
    assert bool(reasons) is blocked


def test_ratio_threshold_is_configurable():
    reasons = check_export_guard(_map(50, [1, 2, 3]), "legacy", ProjectSettings(min_match_ratio=0.05))
    assert reasons == []


def test_failed_analysis_and_foreign_fields_block():
    assert check_export_guard(_map(1, [1], tier=DocumentTier.FAILED), "legacy", ProjectSettings())
    assert check_export_guard(_map(1, [1], foreign=2), "legacy", ProjectSettings())


def test_tracked_changes_block_unless_allowed():
    assert check_export_guard(_map(1, [1], tracked=True), "legacy", ProjectSettings())
    assert check_export_guard(
        _map(1, [1], tracked=True), "legacy",
        ProjectSettings(allow_export_with_tracked_changes=True)) == []


def test_bounded_removal_keeps_appendix(tmp_path):
    b = DocBuilder()
    b.paragraph("Body")
    b.paragraph("References")
    b.paragraph("1. A. T. J. 2020.")
    b.paragraph("")
    b.paragraph("2. B. T. J. 2021.")
    b.paragraph("Appendix A")
    b.paragraph("Supplementary text.")
    h = DocxHandler(str(b.save(tmp_path / "a.docx")))
    removed = h.remove_references_section(1)
    assert removed == 4
    assert [p.text for p in h.get_paragraphs()] == ["Body", "Appendix A", "Supplementary text."]


def test_explicit_end_index_removal(tmp_path):
    b = DocBuilder()
    b.paragraph("Body")
    b.paragraph("References")
    b.paragraph("1. A.")
    b.paragraph("2. B.")
    b.paragraph("Tail")
    h = DocxHandler(str(b.save(tmp_path / "e.docx")))
    assert h.remove_references_section(1, 2) == 2
    assert [p.text for p in h.get_paragraphs()] == ["Body", "2. B.", "Tail"]


def test_removal_without_entries_removes_only_heading(tmp_path):
    b = DocBuilder()
    b.paragraph("Body")
    b.paragraph("References")
    b.paragraph("Appendix A")
    h = DocxHandler(str(b.save(tmp_path / "h.docx")))
    assert h.remove_references_section(1) == 1
    assert [p.text for p in h.get_paragraphs()] == ["Body", "Appendix A"]
