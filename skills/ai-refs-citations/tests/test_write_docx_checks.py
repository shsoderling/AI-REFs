"""The writer's PMC-id check.

The NIH grant style prints the PMCID at the end of each entry and falls back
to the PMID when the record has none; the writer reports such records so the
skill looks them up instead of handing over a grant full of "PMID:" entries.

Run from the repository root: ``python3 -m pytest skills/ai-refs-citations/tests -q``
"""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import common  # noqa: E402,F401  (puts the vendored airefs package on the path)
import write_docx  # noqa: E402

from airefs.models.citation import Author, CitationCandidate  # noqa: E402
from airefs.models.evidence import EvidenceRecord, ReviewDecision  # noqa: E402
from airefs.models.project import CitationStyle, ProjectState  # noqa: E402


def _candidate(pmid, pmcid=""):
    return CitationCandidate(
        pmid=pmid, pmcid=pmcid, doi=f"10.1000/{pmid}", title="A paper", year=2020,
        journal="Nature Communications", journal_abbrev="Nat Commun", volume="11", pages="4395",
        authors=[Author(last_name="Udakis", initials="M")])


def _project(style, *candidates):
    project = ProjectState(input_docx_path="unused.docx")
    project.settings.citation_style = style
    project.evidence_map = {
        f"S{i:03d}": EvidenceRecord(sentence_id=f"S{i:03d}", selected=[cand],
                                    review_decision=ReviewDecision.ACCEPTED)
        for i, cand in enumerate(candidates, start=1)}
    return project


def test_nih_grant_and_nlm_print_the_pmcid_and_nature_does_not():
    assert write_docx.style_prints_pmcid(CitationStyle.NIH_GRANT)
    assert write_docx.style_prints_pmcid(CitationStyle.NLM)
    assert not write_docx.style_prints_pmcid(CitationStyle.NATURE)
    assert not write_docx.style_prints_pmcid(CitationStyle.APA)


def test_pubmed_record_without_pmc_id_is_reported_under_nih_grant():
    project = _project(CitationStyle.NIH_GRANT, _candidate("32879322"), _candidate("31978345"))
    assert write_docx.records_missing_pmcid(project) == [
        "Udakis, 2020 (PMID 31978345)", "Udakis, 2020 (PMID 32879322)"]


def test_record_with_pmc_id_is_not_reported():
    project = _project(CitationStyle.NIH_GRANT, _candidate("32879322", pmcid="PMC7467931"))
    assert write_docx.records_missing_pmcid(project) == []


def test_same_paper_cited_twice_is_reported_once():
    cand = _candidate("32879322")
    project = _project(CitationStyle.NIH_GRANT, cand, cand)
    assert write_docx.records_missing_pmcid(project) == ["Udakis, 2020 (PMID 32879322)"]


def test_style_without_pmcid_reports_nothing():
    project = _project(CitationStyle.NATURE, _candidate("32879322"))
    assert write_docx.records_missing_pmcid(project) == []


def test_preprint_without_pmid_is_not_a_missing_pmcid():
    preprint = CitationCandidate(doi="10.1101/2024.01.03.574066", title="A preprint", year=2024,
                                 journal="bioRxiv", source="biorxiv",
                                 authors=[Author(last_name="Smith", initials="J")])
    project = _project(CitationStyle.NIH_GRANT, preprint)
    assert write_docx.records_missing_pmcid(project) == []


def test_the_classic_nlm_format_prints_pmids_whatever_the_record_holds():
    project = _project(CitationStyle.NIH_GRANT, _candidate("32879322"))
    project.settings.bibliography_format = "nlm"
    assert write_docx.records_missing_pmcid(project) == []


# ── records already embedded in a tracked document ──────────────────────────

from airefs.models.sentence import MarkerType, SentenceRecord  # noqa: E402
from airefs.pipeline.docx_export import export_fresh, export_tracked  # noqa: E402
from airefs.pipeline.existing_citation_parser import ExistingCitationParser  # noqa: E402
from airefs.services.docx_io import DocxHandler  # noqa: E402


def _fresh_nih_document(tmp_path, candidate):
    import docx
    source = tmp_path / "in.docx"
    document = docx.Document()
    document.add_paragraph("A claim (REF).")
    document.save(str(source))
    project = _project(CitationStyle.NIH_GRANT, candidate)
    project.input_docx_path = str(source)
    project.sentences = [SentenceRecord(
        id="S001", paragraph_index=0, raw_text="A claim (REF).", clean_text="A claim.",
        marker_type=MarkerType.REF, marker_count=1, marker_types=[MarkerType.REF])]
    out = tmp_path / "old.docx"
    export_fresh(project, str(out))
    return out


def _reopen(path):
    reopened = ExistingCitationParser(DocxHandler(str(path))).analyze()
    project = ProjectState(settings={"citation_style": CitationStyle.NIH_GRANT})
    project.input_docx_path = str(path)
    project.existing_citations = reopened
    project.doc_tracking = reopened.tracking
    project.is_insert_mode = True
    return project


def _bibliography(path):
    return [p.text for p in DocxHandler(str(path)).get_paragraphs() if "Udakis" in p.text]


def test_an_embedded_record_without_pmc_id_is_reported_when_reopened(tmp_path):
    old = _fresh_nih_document(tmp_path, _candidate("32879322"))
    assert _bibliography(old)[-1].endswith("PMID: 32879322")     # the defect, as written

    project = _reopen(old)
    assert write_docx.records_missing_pmcid(project) == ["Udakis, 2020 (PMID 32879322)"]


def test_records_passed_on_the_next_pass_fill_the_pmc_id_and_it_sticks(tmp_path):
    old = _fresh_nih_document(tmp_path, _candidate("32879322"))
    project = _reopen(old)
    records = {"32879322": _candidate("32879322", pmcid="PMC7467931")}   # as index_records builds it

    healed = tmp_path / "healed.docx"
    stats = export_tracked(project, str(healed), supplements=records)

    assert stats.identifiers_filled == 1
    assert write_docx.records_missing_pmcid(project) == []          # what the writer reports afterwards
    assert _bibliography(healed)[-1].endswith("PMCID: PMC7467931")
    reread = _reopen(healed)                                        # the field carries it now
    assert [e.matched_candidate.pmcid for e in reread.existing_citations.bib_entries.values()] == ["PMC7467931"]
    assert write_docx.records_missing_pmcid(reread) == []


def test_a_record_the_files_do_not_know_stays_reported(tmp_path):
    old = _fresh_nih_document(tmp_path, _candidate("32879322"))
    project = _reopen(old)
    out = tmp_path / "out.docx"
    stats = export_tracked(project, str(out), supplements={"1": _candidate("1", pmcid="PMC1")})
    assert stats.identifiers_filled == 0
    assert write_docx.records_missing_pmcid(project) == ["Udakis, 2020 (PMID 32879322)"]
    assert _bibliography(out)[-1].endswith("PMID: 32879322")


def test_an_entry_adopted_from_plain_text_keeps_its_wording_and_is_not_reported():
    adopted = _candidate("32879322")
    adopted.raw_entry = "1. Udakis M. A paper. Nat Commun. 2020."
    from airefs.models.embedded import DocumentTier, TrackingReport
    from airefs.models.existing_refs import ExistingBibEntry, ExistingCitationMap
    project = _project(CitationStyle.NIH_GRANT)
    project.existing_citations = ExistingCitationMap(
        bib_entries={1: ExistingBibEntry(original_number=1, matched_candidate=adopted)})
    project.doc_tracking = TrackingReport(tier=DocumentTier.TRACKED)
    assert write_docx.records_missing_pmcid(project) == []
