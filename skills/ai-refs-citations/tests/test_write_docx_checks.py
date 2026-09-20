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
