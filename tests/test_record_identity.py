"""One hidden record per paper, however many times it is cited.

Record ids used to be minted per candidate object, so two searches that found
the same paper left two records in the document's field data against one
printed entry, and a later pass read more references than the document shows.
"""

import pytest

from src.models.citation import Author, CitationCandidate
from src.models.evidence import EvidenceRecord, ReviewDecision
from src.models.project import CitationStyle, ProjectState
from src.models.sentence import MarkerType, SentenceRecord
from src.pipeline.citation_payload import parse_cite_code
from src.pipeline.docx_export import ExportDecisions, export_fresh, export_legacy, export_tracked
from src.pipeline.existing_citation_parser import ExistingCitationParser
from src.pipeline.renumbering import RecordCanonicaliser
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder
from tests.test_round_trip import make_citation


def _sentence(index: int, text: str, sentence_id: str) -> SentenceRecord:
    return SentenceRecord(id=sentence_id, paragraph_index=index, raw_text=text,
                          clean_text=text.replace(" (REF)", ""), marker_type=MarkerType.REF,
                          marker_count=1, marker_types=[MarkerType.REF])


def _project(path, sentences, evidence, **settings) -> ProjectState:
    project = ProjectState(settings={"citation_style": CitationStyle.NIH_GRANT, **settings})
    project.input_docx_path = str(path)
    project.sentences = sentences
    project.evidence_map = evidence
    return project


def _entries(path) -> list[str]:
    handler = DocxHandler(str(path))
    existing = ExistingCitationParser(handler).analyze()
    return [(e.raw_text or "") for e in existing.bib_entries.values()]


def _record_ids(path) -> list[str]:
    handler = DocxHandler(str(path))
    return [item.record_uuid
            for f in handler.fields.fields if f.kind == "airefs_cite"
            for item in parse_cite_code(f.code).items]


def test_one_paper_cited_twice_leaves_one_record(tmp_path):
    """Two searches, two objects, one paper: one entry and one record id."""
    builder = DocBuilder()
    builder.paragraph("First claim (REF).")
    builder.paragraph("Second claim about the same work (REF).")
    source = builder.save(tmp_path / "in.docx")

    project = _project(
        source,
        [_sentence(0, "First claim (REF).", "S001"),
         _sentence(1, "Second claim about the same work (REF).", "S002")],
        {"S001": EvidenceRecord(sentence_id="S001", selected=[make_citation(7)],
                                review_decision=ReviewDecision.ACCEPTED),
         "S002": EvidenceRecord(sentence_id="S002", selected=[make_citation(7)],
                                review_decision=ReviewDecision.ACCEPTED)})
    out = tmp_path / "out.docx"
    stats = export_fresh(project, str(out))

    assert stats.bibliography_size == 1
    ids = _record_ids(out)
    assert len(ids) == 2 and len(set(ids)) == 1        # both citations, one record
    entries = _entries(out)
    assert len(entries) == 1 and entries[0].strip()    # no phantom, no blank entry
    assert project.record_order == [ids[0]]


def test_the_re_read_entry_count_matches_the_printed_one(tmp_path):
    """What a second pass reads is what the reader sees."""
    builder = DocBuilder()
    builder.paragraph("First claim (REF).")
    builder.paragraph("Second claim about the same work (REF).")
    builder.paragraph("A third claim (REF).")
    source = builder.save(tmp_path / "in.docx")

    project = _project(
        source,
        [_sentence(0, "First claim (REF).", "S001"),
         _sentence(1, "Second claim about the same work (REF).", "S002"),
         _sentence(2, "A third claim (REF).", "S003")],
        {"S001": EvidenceRecord(sentence_id="S001", selected=[make_citation(7)],
                                review_decision=ReviewDecision.ACCEPTED),
         "S002": EvidenceRecord(sentence_id="S002", selected=[make_citation(7)],
                                review_decision=ReviewDecision.ACCEPTED),
         "S003": EvidenceRecord(sentence_id="S003", selected=[make_citation(9)],
                                review_decision=ReviewDecision.ACCEPTED)})
    out = tmp_path / "out.docx"
    export_fresh(project, str(out))

    printed = [p.text for p in DocxHandler(str(out)).get_paragraphs()
               if p.text.strip()[:2] in ("1.", "2.")]
    assert len(printed) == 2
    assert len(_entries(out)) == len(printed)
    assert all(text.strip() for text in _entries(out))


def test_a_document_written_before_the_fix_heals_on_the_next_pass(tmp_path, monkeypatch):
    """Documents in the wild carry the duplicate; rewriting collapses it."""
    builder = DocBuilder()
    builder.paragraph("First claim (REF).")
    builder.paragraph("Second claim about the same work (REF).")
    source = builder.save(tmp_path / "in.docx")
    project = _project(
        source,
        [_sentence(0, "First claim (REF).", "S001"),
         _sentence(1, "Second claim about the same work (REF).", "S002")],
        {"S001": EvidenceRecord(sentence_id="S001", selected=[make_citation(7)],
                                review_decision=ReviewDecision.ACCEPTED),
         "S002": EvidenceRecord(sentence_id="S002", selected=[make_citation(7)],
                                review_decision=ReviewDecision.ACCEPTED)})

    monkeypatch.setattr(RecordCanonicaliser, "canonical", lambda self, candidate: candidate)
    old = tmp_path / "old.docx"
    export_fresh(project, str(old))
    monkeypatch.undo()
    assert len(set(_record_ids(old))) == 2                 # the old defect, reproduced

    reopened = ExistingCitationParser(DocxHandler(str(old))).analyze()
    second = ProjectState(settings={"citation_style": CitationStyle.NIH_GRANT})
    second.input_docx_path = str(old)
    second.existing_citations = reopened
    second.doc_tracking = reopened.tracking
    second.is_insert_mode = True
    out = tmp_path / "healed.docx"
    export_tracked(second, str(out))

    assert len(set(_record_ids(out))) == 1
    entries = [e for e in _entries(out) if e.strip()]
    assert len(entries) == 1


def test_a_new_citation_joins_an_existing_entry_in_a_legacy_document(tmp_path):
    """Citing a paper the hand-written list already has adds no second entry."""
    builder = DocBuilder()
    paragraph = builder.paragraph("Old claim")
    builder.add_text(paragraph, "1", superscript=True)
    builder.add_text(paragraph, ". New claim about the same paper (REF).")
    builder.paragraph("References")
    builder.paragraph("1. Author7 A. Important Study Number 7. J Test 7. 2025;17:700-710. "
                      "doi:10.1234/test.7 PMID: 30000007")
    source = builder.save(tmp_path / "legacy.docx")

    existing = ExistingCitationParser(DocxHandler(str(source))).analyze()
    project = _project(source,
                       [_sentence(0, "New claim about the same paper (REF).", "S001")],
                       {"S001": EvidenceRecord(sentence_id="S001", selected=[make_citation(7)],
                                               review_decision=ReviewDecision.ACCEPTED)})
    project.existing_citations = existing
    project.doc_tracking = existing.tracking
    project.is_insert_mode = True
    out = tmp_path / "out.docx"
    export_legacy(project, str(out), ExportDecisions())

    body = DocxHandler(str(out)).get_paragraphs()[0].text
    assert body.count("1") == 2 and "2" not in body        # both citations are number 1
    assert len([e for e in _entries(out) if e.strip()]) == 1


# ── the canonicaliser itself ──────────────────────────────────────────

def test_the_owning_record_learns_from_later_copies():
    owner = CitationCandidate(doi="10.1101/2024.01.03.574066", title="A preprint",
                              year=2024, journal="bioRxiv",
                              authors=[Author(last_name="Battison", initials="A")])
    owner.record_uuid = "keep-me"
    published = CitationCandidate(pmid="41249054", doi="10.1101/2024.01.03.574066",
                                  title="A preprint", year=2025, journal="Nature Methods",
                                  journal_abbrev="Nat Methods", volume="22", pages="1-9",
                                  authors=[Author(last_name="Battison", initials="A"),
                                           Author(last_name="LoTurco", initials="J")])
    canon = RecordCanonicaliser()
    assert canon.canonical(owner) is owner
    assert canon.canonical(published) is owner
    assert owner.record_uuid == "keep-me"                  # the document still points here
    assert owner.pmid == "41249054" and owner.journal == "Nature Methods"
    assert len(owner.authors) == 2


def test_gaps_are_filled_without_overwriting_what_is_there():
    owner = make_citation(3)
    owner.pmcid = ""
    other = make_citation(3)
    other.pmcid = "PMC999"
    other.journal = "A different journal name"
    canon = RecordCanonicaliser()
    canon.canonical(owner)
    canon.canonical(other)
    assert owner.pmcid == "PMC999"                         # a gap is filled
    assert owner.journal != "A different journal name"     # a value is not replaced


def test_a_candidate_with_nothing_to_identify_it_is_left_alone():
    bare = CitationCandidate()
    canon = RecordCanonicaliser()
    assert canon.canonical(bare) is bare


@pytest.mark.parametrize("other_kwargs", [
    {"pmid": "30000007", "doi": "", "title": ""},          # same PMID, nothing else
    {"pmid": "", "doi": "10.1234/test.7", "title": ""},    # same DOI only
])
def test_any_shared_identifier_resolves_to_the_same_record(other_kwargs):
    owner = make_citation(7)
    canon = RecordCanonicaliser()
    canon.canonical(owner)
    assert canon.canonical(CitationCandidate(**other_kwargs)) is owner


def test_two_papers_that_share_a_title_prefix_keep_their_own_records(tmp_path):
    """A 60-character title prefix is enough to share a number, never a record.

    Long biomedical titles collide on their opening words; merging their
    metadata would print a reference that belongs to neither paper.
    """
    prefix = "Efficacy and safety of semaglutide in adults with overweight or obesity"
    preprint = CitationCandidate(
        doi="10.1101/2024.05.05.000123", title=f"{prefix}", year=2024,
        journal="bioRxiv", journal_abbrev="bioRxiv",
        authors=[Author(last_name="Alpha", initials="A")])
    other_paper = CitationCandidate(
        pmid="99999999", doi="10.1038/s41586-025-00001-x",
        title=f"{prefix} and type 2 diabetes", year=2025,
        journal="Nature", journal_abbrev="Nature", volume="640", pages="55-62",
        authors=[Author(last_name="Zeta", initials="Z"), Author(last_name="Omega", initials="O")])

    canon = RecordCanonicaliser()
    assert canon.canonical(preprint) is preprint
    assert canon.canonical(other_paper) is other_paper      # different papers
    assert preprint.journal == "bioRxiv" and preprint.year == 2024
    assert preprint.pmid == "" and len(preprint.authors) == 1


def test_a_retraction_does_not_travel_between_papers():
    prefix = "Single-cell transcriptomic atlas of the human prefrontal cortex in health"
    clean = CitationCandidate(pmid="1", doi="10.1/clean", title=prefix, year=2024,
                              journal="Cell", authors=[Author(last_name="Alpha", initials="A")])
    retracted = CitationCandidate(pmid="2", doi="10.1/retracted", title=f"{prefix} and disease",
                                  year=2024, journal="Cell", is_retracted=True,
                                  retraction_notice="Retracted 2025",
                                  authors=[Author(last_name="Zeta", initials="Z")])
    canon = RecordCanonicaliser()
    canon.canonical(clean)
    canon.canonical(retracted)
    assert clean.is_retracted is False and not clean.retraction_notice


def test_a_reviewed_record_is_not_demoted_to_a_documents_stub():
    """A legacy entry is a title and a year; the reviewed paper has the rest."""
    stub = CitationCandidate(pmid="30000007", title="Important Study Number 7", year=2025)
    stub.raw_entry = "Author7 A. Important Study Number 7. J Test 7. 2025;17:700-710."
    reviewed = make_citation(7)
    canon = RecordCanonicaliser()
    canon.canonical(stub)
    assert canon.canonical(reviewed) is stub
    assert stub.journal == reviewed.journal and stub.volume == reviewed.volume
    assert stub.authors and stub.authors[0].last_name == reviewed.authors[0].last_name


def test_a_new_citation_before_the_existing_one_still_adopts(tmp_path):
    """The new marker comes first in the document, the old citation second."""
    builder = DocBuilder()
    paragraph = builder.paragraph("New claim about the same paper (REF). Old claim")
    builder.add_text(paragraph, "1", superscript=True)
    builder.add_text(paragraph, ".")
    builder.paragraph("References")
    builder.paragraph("1. Author7 A. Important Study Number 7. J Test 7. 2025;17:700-710. "
                      "doi:10.1234/test.7 PMID: 30000007")
    source = builder.save(tmp_path / "legacy.docx")

    existing = ExistingCitationParser(DocxHandler(str(source))).analyze()
    project = _project(source,
                       [_sentence(0, "New claim about the same paper (REF).", "S001")],
                       {"S001": EvidenceRecord(sentence_id="S001", selected=[make_citation(7)],
                                               review_decision=ReviewDecision.ACCEPTED)})
    project.existing_citations = existing
    project.doc_tracking = existing.tracking
    project.is_insert_mode = True
    out = tmp_path / "out.docx"
    export_legacy(project, str(out), ExportDecisions())          # must not raise

    assert len([e for e in _entries(out) if e.strip()]) == 1
    assert len(set(_record_ids(out))) == 1                        # both sites, one record


def test_previewing_the_numbering_leaves_the_reviewed_records_alone(tmp_path):
    """The plan runs on the GUI's preview path too: it must not rewrite state."""
    from src.pipeline.renumber_plan import build_renumber_plan

    builder = DocBuilder()
    builder.paragraph("First claim (REF).")
    builder.paragraph("Second claim (REF).")
    source = builder.save(tmp_path / "in.docx")
    first, second = make_citation(7), make_citation(9)
    project = _project(source,
                       [_sentence(0, "First claim (REF).", "S001"),
                        _sentence(1, "Second claim (REF).", "S002")],
                       {"S001": EvidenceRecord(sentence_id="S001", selected=[first],
                                               review_decision=ReviewDecision.ACCEPTED),
                        "S002": EvidenceRecord(sentence_id="S002", selected=[second],
                                               review_decision=ReviewDecision.ACCEPTED)})
    before = [(c.pmid, c.doi, c.title, c.journal, c.year) for c in (first, second)]
    build_renumber_plan(DocxHandler(str(source)), project)
    after = [(c.pmid, c.doi, c.title, c.journal, c.year) for c in (first, second)]
    assert before == after
