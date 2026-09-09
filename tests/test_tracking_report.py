"""Tracking report: every analysis reports a tier, foreign fields are counted
but never read, and a failing analysis is reported instead of raised."""

from src.models.embedded import DocumentTier, TrackingReport
from src.pipeline.existing_citation_parser import ExistingCitationParser
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder


def test_legacy_document_reports_legacy_tier(tmp_path):
    b = DocBuilder()
    p = b.paragraph("Claim")
    b.add_text(p, "1", superscript=True)
    b.paragraph("References")
    b.paragraph("1. A. T. J. 2020. doi:10.1/a")
    h = DocxHandler(str(b.save(tmp_path / "l.docx")))
    m = ExistingCitationParser(h).analyze()
    assert m.tracking.tier == DocumentTier.LEGACY
    assert m.tracking.field_count == 0 and m.tracking.foreign_field_count == 0
    assert m.has_existing_citations


def test_foreign_fields_are_counted_not_read(tmp_path):
    b = DocBuilder()
    p = b.paragraph("Claim")
    b.add_field(p, code=" ADDIN EN.CITE <EndNote/> ", result="1", superscript=True)
    b.paragraph("References")
    b.paragraph("1. A. T. J. 2020.")
    h = DocxHandler(str(b.save(tmp_path / "f.docx")))
    m = ExistingCitationParser(h).analyze()
    assert m.tracking.foreign_field_count == 1
    assert m.tracking.tier == DocumentTier.LEGACY
    assert any("another reference manager" in p for p in m.tracking.problems)
    assert not m.in_text_citations          # the field's result "1" is not scanned


def test_analysis_failure_is_reported_not_raised(tmp_path, monkeypatch):
    b = DocBuilder()
    b.paragraph("Claim")
    h = DocxHandler(str(b.save(tmp_path / "x.docx")))
    parser = ExistingCitationParser(h)
    monkeypatch.setattr(parser, "_find_references_heading", lambda paras: 1 / 0)
    m = parser.analyze()
    assert m.tracking.tier == DocumentTier.FAILED
    assert m.tracking.problems and "ZeroDivisionError" in m.tracking.problems[0]
    assert not m.has_existing_citations


def test_pending_tracked_changes_flag(tmp_path):
    b = DocBuilder()
    p = b.paragraph("Claim")
    b.add_field(p, code=' ADDIN AIREFS.CITE {"a":1} ', result="1", wrap="del")
    h = DocxHandler(str(b.save(tmp_path / "t.docx")))
    m = ExistingCitationParser(h).analyze()
    assert m.pending_tracked_changes is True


def test_tracking_report_roundtrips_through_json():
    r = TrackingReport(tier=DocumentTier.LEGACY, problems=["p"])
    assert TrackingReport.model_validate_json(r.model_dump_json()) == r
