"""Embedded records completed from records the caller supplies.

A tracked export re-renders every entry from its record, so a record that
went into the document without an identifier (a hand-built record missing
its PMC id, for instance) keeps printing without it. The caller can pass the
records it knows; a record sharing an identifier lends the missing ones.
"""

from src.models.citation import Author, CitationCandidate
from src.models.evidence import EvidenceRecord, ReviewDecision
from src.models.existing_refs import ExistingBibEntry, ExistingCitationMap
from src.models.project import CitationStyle, ProjectState
from src.models.sentence import MarkerType, SentenceRecord
from src.pipeline.docx_export import export_fresh, export_tracked, fill_missing_identifiers
from src.pipeline.existing_citation_parser import ExistingCitationParser
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder


def _candidate(pmid="", pmcid="", doi="", raw_entry=""):
    return CitationCandidate(
        pmid=pmid, pmcid=pmcid, doi=doi, raw_entry=raw_entry, title="A paper", year=2020,
        journal="Nature Communications", journal_abbrev="Nat Commun", volume="11", pages="4395",
        authors=[Author(last_name="Udakis", initials="M")])


def _existing(*candidates):
    return ExistingCitationMap(bib_entries={
        n: ExistingBibEntry(original_number=n, matched_candidate=c)
        for n, c in enumerate(candidates, start=1)})


def test_a_supplement_sharing_the_pmid_lends_the_pmc_id_and_doi():
    cand = _candidate(pmid="32879322")
    filled = fill_missing_identifiers(_existing(cand), {
        "32879322": _candidate(pmid="32879322", pmcid="PMC7467931", doi="10.1038/s41467-020-18074-8")})
    assert filled == 1
    assert (cand.pmcid, cand.doi) == ("PMC7467931", "10.1038/s41467-020-18074-8")


def test_a_supplement_matched_by_doi_lends_the_pmid_without_touching_set_fields():
    cand = _candidate(doi="10.1038/S41467-020-18074-8", pmcid="PMC0000001")
    filled = fill_missing_identifiers(_existing(cand), {
        "10.1038/s41467-020-18074-8": _candidate(pmid="32879322", pmcid="PMC7467931",
                                                 doi="10.1038/s41467-020-18074-8")})
    assert filled == 1
    assert cand.pmid == "32879322"
    assert cand.pmcid == "PMC0000001"                      # already set: kept
    assert cand.doi == "10.1038/S41467-020-18074-8"


def test_records_that_are_complete_unknown_or_adopted_are_left_alone():
    complete = _candidate(pmid="1", pmcid="PMC1", doi="10.1/a")
    unknown = _candidate(pmid="2")
    adopted = _candidate(pmid="3", raw_entry="3. Udakis M. A paper. Nat Commun. 2020.")
    supplements = {"1": _candidate(pmid="1", pmcid="PMC9", doi="10.1/z"),
                   "3": _candidate(pmid="3", pmcid="PMC3")}
    assert fill_missing_identifiers(_existing(complete, unknown, adopted), supplements) == 0
    assert (complete.pmcid, complete.doi) == ("PMC1", "10.1/a")
    assert (unknown.pmcid, adopted.pmcid) == ("", "")


def test_the_tracked_export_completes_the_record_and_the_entry_prints_the_pmcid(tmp_path):
    builder = DocBuilder()
    builder.paragraph("A claim (REF).")
    source = builder.save(tmp_path / "in.docx")
    project = ProjectState(settings={"citation_style": CitationStyle.NIH_GRANT})
    project.input_docx_path = str(source)
    project.sentences = [SentenceRecord(
        id="S001", paragraph_index=0, raw_text="A claim (REF).", clean_text="A claim.",
        marker_type=MarkerType.REF, marker_count=1, marker_types=[MarkerType.REF])]
    project.evidence_map = {"S001": EvidenceRecord(
        sentence_id="S001", selected=[_candidate(pmid="32879322")],
        review_decision=ReviewDecision.ACCEPTED)}
    old = tmp_path / "old.docx"
    export_fresh(project, str(old))
    entries = lambda path: [p.text for p in DocxHandler(str(path)).get_paragraphs() if "Udakis" in p.text]
    assert entries(old)[-1].endswith("PMID: 32879322")

    reopened = ExistingCitationParser(DocxHandler(str(old))).analyze()
    second = ProjectState(settings={"citation_style": CitationStyle.NIH_GRANT})
    second.input_docx_path = str(old)
    second.existing_citations = reopened
    second.doc_tracking = reopened.tracking
    second.is_insert_mode = True
    out = tmp_path / "healed.docx"
    stats = export_tracked(second, str(out), supplements={
        "32879322": _candidate(pmid="32879322", pmcid="PMC7467931")})

    assert stats.identifiers_filled == 1
    assert any("completed with identifiers" in line for line in stats.summary_lines())
    assert entries(out)[-1].endswith("PMCID: PMC7467931")
    reread = ExistingCitationParser(DocxHandler(str(out))).analyze()
    assert [e.matched_candidate.pmcid for e in reread.bib_entries.values()] == ["PMC7467931"]
