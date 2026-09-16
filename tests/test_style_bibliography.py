"""Reference entries follow the citation style the user chose.

The in-text form always came from the style's CSL ``<citation>`` element;
these cover the other half, the ``<bibliography>`` element, and the two
escape hatches (identifiers kept, or the old single format).
"""

import pytest

from src.models.citation import Author, CitationCandidate
from src.models.evidence import EvidenceRecord, ReviewDecision
from src.models.project import AUTHOR_DATE_STYLES, CitationStyle, ProjectState
from src.models.sentence import MarkerType, SentenceRecord
from src.pipeline.bib_format import format_bib_entry, format_nlm_entry
from src.pipeline.csl_bibliography import render_entry
from src.pipeline.docx_export import export_fresh
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder

ARTICLE = CitationCandidate(
    pmid="32879322", pmcid="PMC7467931", doi="10.1038/s41467-020-18074-8",
    title="Interneuron-specific plasticity at inhibitory synapses",
    authors=[Author(last_name="Udakis", first_name="Matt", initials="M"),
             Author(last_name="Pedrosa", first_name="Victor", initials="V"),
             Author(last_name="Mellor", first_name="Jack R", initials="JR")],
    year=2020, journal="Nature communications", journal_abbrev="Nat Commun",
    volume="11", issue="1", pages="4395", source="pubmed")

CROWD = CitationCandidate(
    pmid="38577773", doi="10.1093/brain/awae106",
    title="Tiam1-mediated maladaptive plasticity",
    authors=[Author(last_name=f"Author{i}", first_name=f"First{i}", initials="F")
             for i in range(1, 9)],
    year=2024, journal="Brain", journal_abbrev="Brain", volume="147", issue="7",
    pages="2507-2521", source="pubmed")

PREPRINT = CitationCandidate(
    doi="10.1101/2024.01.03.574066", title="Synaptic proteomes of interneuron classes",
    authors=[Author(last_name="Battison", first_name="Alexandra", initials="A")],
    year=2024, journal="bioRxiv", journal_abbrev="bioRxiv", source="biorxiv")


@pytest.mark.parametrize("style", list(CitationStyle), ids=lambda s: s.value)
def test_every_style_renders_an_entry(style):
    entry = format_bib_entry(ARTICLE, 3, style)
    assert "Udakis" in entry and "2020" in entry
    assert "nat commun" in entry.lower() or "nature communications" in entry.lower()
    if style in AUTHOR_DATE_STYLES:
        assert not entry.lstrip().startswith("3")
    else:
        assert entry.lstrip()[:3] in ("3. ", "[3]", "(3)")


def test_the_entry_shape_differs_between_styles():
    nature = format_bib_entry(ARTICLE, 1, CitationStyle.NATURE)
    apa = format_bib_entry(ARTICLE, 1, CitationStyle.APA)
    vancouver = format_bib_entry(ARTICLE, 1, CitationStyle.VANCOUVER)
    assert nature != apa != vancouver
    assert "Udakis, M., Pedrosa, V., & Mellor, J. R. (2020)" in apa
    assert nature.startswith("1. Udakis, M., Pedrosa, V. & Mellor, J. R.")
    assert vancouver.startswith("1. Udakis M, Pedrosa V, Mellor JR.")


def test_each_style_applies_its_own_author_cap():
    assert "Author1, F. et al." in format_bib_entry(CROWD, 1, CitationStyle.NATURE)
    assert "Author3 F, et al." in format_bib_entry(CROWD, 1, CitationStyle.AMA)
    assert "Author8" in format_bib_entry(CROWD, 1, CitationStyle.NIH_GRANT)   # no cap at 8


def test_nih_grant_keeps_the_pmcid_and_apa_the_doi_link():
    assert "PMC7467931" in format_bib_entry(ARTICLE, 1, CitationStyle.NIH_GRANT)
    assert "https://doi.org/10.1038/s41467-020-18074-8" in format_bib_entry(
        ARTICLE, 1, CitationStyle.APA)


def test_a_preprint_is_labelled_by_styles_that_say_so():
    assert "Preprint" in format_bib_entry(PREPRINT, 1, CitationStyle.APA)
    assert "Preprint" in format_bib_entry(PREPRINT, 1, CitationStyle.NATURE)
    assert "Battison" in format_bib_entry(PREPRINT, 1, CitationStyle.VANCOUVER)


def test_the_nlm_format_is_one_shape_for_every_style():
    shapes = {format_nlm_entry(ARTICLE, 1, style) for style in
              (CitationStyle.NATURE, CitationStyle.VANCOUVER, CitationStyle.IEEE)}
    assert len(shapes) == 1


def test_an_unrenderable_style_falls_back_instead_of_writing_a_fragment(monkeypatch, tmp_path):
    broken = tmp_path / "broken.csl"
    broken.write_text("<style><not-csl></style>", encoding="utf-8")
    monkeypatch.setattr("src.pipeline.csl_bibliography._CACHE", {})
    monkeypatch.setattr("src.models.project.get_csl_path", lambda style: broken)
    assert render_entry(ARTICLE, 1, CitationStyle.NATURE) is None
    assert format_bib_entry(ARTICLE, 1, CitationStyle.NATURE) == format_nlm_entry(
        ARTICLE, 1, CitationStyle.NATURE)
    monkeypatch.setattr("src.pipeline.csl_bibliography._CACHE", {})


def test_an_exported_document_carries_the_chosen_style_s_entries(tmp_path):
    builder = DocBuilder()
    builder.paragraph("A claim (REF).")
    source = builder.save(tmp_path / "in.docx")
    project = ProjectState(settings={"citation_style": CitationStyle.APA})
    project.input_docx_path = str(source)
    project.sentences = [SentenceRecord(
        id="S001", paragraph_index=0, raw_text="A claim (REF).", clean_text="A claim.",
        marker_type=MarkerType.REF, marker_count=1, marker_types=[MarkerType.REF])]
    project.evidence_map = {"S001": EvidenceRecord(
        sentence_id="S001", selected=[ARTICLE], review_decision=ReviewDecision.ACCEPTED)}
    out = tmp_path / "out.docx"
    export_fresh(project, str(out))

    paragraphs = [p.text for p in DocxHandler(str(out)).get_paragraphs()]
    assert "A claim (Udakis et al., 2020)." == paragraphs[0]
    assert paragraphs[-1].startswith("Udakis, M., Pedrosa, V., & Mellor, J. R. (2020).")


def test_the_nlm_setting_writes_what_earlier_versions_wrote(tmp_path):
    builder = DocBuilder()
    builder.paragraph("A claim (REF).")
    source = builder.save(tmp_path / "in.docx")
    project = ProjectState(settings={"citation_style": CitationStyle.NATURE,
                                     "bibliography_format": "nlm"})
    project.input_docx_path = str(source)
    project.sentences = [SentenceRecord(
        id="S001", paragraph_index=0, raw_text="A claim (REF).", clean_text="A claim.",
        marker_type=MarkerType.REF, marker_count=1, marker_types=[MarkerType.REF])]
    project.evidence_map = {"S001": EvidenceRecord(
        sentence_id="S001", selected=[ARTICLE], review_decision=ReviewDecision.ACCEPTED)}
    out = tmp_path / "out.docx"
    export_fresh(project, str(out))
    assert DocxHandler(str(out)).get_paragraphs()[-1].text == format_nlm_entry(
        ARTICLE, 1, CitationStyle.NATURE)


def test_a_document_with_no_hidden_fields_keeps_its_identifiers(tmp_path):
    """Plain-text exports have nowhere else to carry identity."""
    builder = DocBuilder()
    builder.paragraph("A claim (REF).")
    source = builder.save(tmp_path / "in.docx")
    project = ProjectState(settings={"citation_style": CitationStyle.NATURE,
                                     "embed_citation_fields": False})
    project.input_docx_path = str(source)
    project.sentences = [SentenceRecord(
        id="S001", paragraph_index=0, raw_text="A claim (REF).", clean_text="A claim.",
        marker_type=MarkerType.REF, marker_count=1, marker_types=[MarkerType.REF])]
    project.evidence_map = {"S001": EvidenceRecord(
        sentence_id="S001", selected=[ARTICLE], review_decision=ReviewDecision.ACCEPTED)}
    out = tmp_path / "out.docx"
    export_fresh(project, str(out))

    entry = DocxHandler(str(out)).get_paragraphs()[-1].text
    assert "Udakis, M." in entry                      # still Nature's shape
    assert "PMID: 32879322" in entry
    assert "10.1038/s41467-020-18074-8" in entry


def test_a_style_that_prints_the_doi_as_a_url_round_trips(tmp_path):
    """APA gives the DOI as a link; the entry reader has to accept that form."""
    from src.models.existing_refs import ExistingBibEntry
    from src.pipeline.existing_citation_parser import ExistingCitationParser

    entry = ExistingBibEntry(original_number=1)
    ExistingCitationParser._extract_bib_fields(
        entry, format_bib_entry(ARTICLE, 1, CitationStyle.APA))
    assert entry.doi == ARTICLE.doi


def test_style_with_ids_does_not_print_an_identifier_twice():
    apa = format_bib_entry(ARTICLE, 1, CitationStyle.APA, "style_with_ids")
    assert apa.count(ARTICLE.doi) == 1               # APA prints it as a URL already
    vancouver = format_bib_entry(ARTICLE, 1, CitationStyle.VANCOUVER, "style_with_ids")
    assert vancouver.count(ARTICLE.doi) == 1         # Vancouver prints none, so one is added
    assert vancouver.count(ARTICLE.pmid) == 1


def test_a_cold_spring_harbor_journal_is_not_called_a_preprint():
    """10.1101 is the publisher's prefix, not bioRxiv's."""
    genome_research = CitationCandidate(
        doi="10.1101/gr.123456.119", title="A genome research paper", year=2019,
        journal="Genome Research", journal_abbrev="Genome Res", volume="29", pages="1-10",
        authors=[Author(last_name="Ng", first_name="Wei", initials="W")])
    assert "Preprint" not in format_bib_entry(genome_research, 1, CitationStyle.APA)


def test_a_preprint_pubmed_has_indexed_is_still_a_preprint():
    indexed = CitationCandidate(
        pmid="39000001", doi="10.1101/2024.02.02.500000", title="An indexed preprint",
        year=2024, journal="bioRxiv", journal_abbrev="bioRxiv",
        publication_types=["Preprint"],
        authors=[Author(last_name="Kim", first_name="Soo", initials="S")])
    assert "Preprint" in format_bib_entry(indexed, 1, CitationStyle.APA)


def test_a_failure_to_read_a_style_is_not_remembered_for_the_process(monkeypatch, tmp_path):
    """A transient failure must not silently disable styling for everything after."""
    broken = tmp_path / "broken.csl"
    broken.write_text("<style><not-csl></style>", encoding="utf-8")
    monkeypatch.setattr("src.pipeline.csl_bibliography._CACHE", {})
    monkeypatch.setattr("src.models.project.get_csl_path", lambda style: broken)
    assert render_entry(ARTICLE, 1, CitationStyle.NATURE) is None
    monkeypatch.undo()
    monkeypatch.setattr("src.pipeline.csl_bibliography._CACHE", {})
    assert render_entry(ARTICLE, 1, CitationStyle.NATURE) is not None


def test_the_tracked_and_legacy_paths_honour_the_format(tmp_path):
    """Not just the fresh export: every path reads the setting."""
    from src.pipeline.docx_export import ExportDecisions, export_legacy, export_tracked
    from src.pipeline.existing_citation_parser import ExistingCitationParser

    builder = DocBuilder()
    builder.paragraph("A claim (REF).")
    source = builder.save(tmp_path / "in.docx")
    project = ProjectState(settings={"citation_style": CitationStyle.NATURE})
    project.input_docx_path = str(source)
    project.sentences = [SentenceRecord(
        id="S001", paragraph_index=0, raw_text="A claim (REF).", clean_text="A claim.",
        marker_type=MarkerType.REF, marker_count=1, marker_types=[MarkerType.REF])]
    project.evidence_map = {"S001": EvidenceRecord(
        sentence_id="S001", selected=[ARTICLE], review_decision=ReviewDecision.ACCEPTED)}
    first = tmp_path / "first.docx"
    export_fresh(project, str(first))
    assert DocxHandler(str(first)).get_paragraphs()[-1].text.startswith("1. Udakis, M.")

    # the tracked pass re-renders the same entry, and the setting still decides
    reopened = ExistingCitationParser(DocxHandler(str(first))).analyze()
    second = ProjectState(settings={"citation_style": CitationStyle.NATURE,
                                    "bibliography_format": "nlm"})
    second.input_docx_path = str(first)
    second.existing_citations = reopened
    second.doc_tracking = reopened.tracking
    second.is_insert_mode = True
    out = tmp_path / "tracked.docx"
    export_tracked(second, str(out))
    assert DocxHandler(str(out)).get_paragraphs()[-1].text == format_nlm_entry(
        ARTICLE, 1, CitationStyle.NATURE)
