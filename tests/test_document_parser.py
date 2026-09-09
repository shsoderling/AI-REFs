"""DocumentParser + MarkerLocator on generated DOCX files."""

from docx import Document

from src.models.markers import MarkerConfig, MarkerType
from src.pipeline.document_parser import DocumentParser, _split_sentences
from src.pipeline.marker_locator import MarkerLocator
from src.services.docx_io import DocxHandler


def make_docx(path, paragraphs):
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    doc.save(str(path))
    return str(path)


def test_split_does_not_break_inside_parentheses():
    text = "We used a method (Smith, J. A., 2020). It worked (cf. Jones 2019). Done."
    parts = _split_sentences(text)
    assert parts == [
        "We used a method (Smith, J. A., 2020).",
        "It worked (cf. Jones 2019).",
        "Done.",
    ]


def test_unbalanced_paren_does_not_swallow_paragraph():
    parts = _split_sentences("Typo (Smith et al. 2020. Second sentence here. Third one (REF). Fourth.")
    assert parts == ["Typo (Smith et al. 2020.", "Second sentence here.", "Third one (REF).", "Fourth."]


def test_split_still_breaks_after_closing_paren():
    text = "Spines grow during LTP (Smith et al. 2020). However, this is contested (PMID: 1). End."
    parts = _split_sentences(text)
    assert parts[0] == "Spines grow during LTP (Smith et al. 2020)."
    assert parts[1] == "However, this is contested (PMID: 1)."
    assert parts[2] == "End."


def test_parser_and_locator_end_to_end(tmp_path):
    path = make_docx(tmp_path / "doc.docx", [
        "Rac1 drives spine growth (REF). PAK is downstream (PMID: 32879322; Battison et al. 2024).",
        "Previous studies (Smith et al., 2022) and (Johnson et al., 2021) showed X. Nothing here (n = 12).",
        "Two IDs (PMC11413553, PMC3159129) support this claim.",
    ])
    handler = DocxHandler(path)
    sentences = DocumentParser(handler, marker_config=MarkerConfig.all_on()).parse()
    MarkerLocator(MarkerConfig.all_on()).locate(sentences)

    texts = {s.raw_text: s for s in sentences}
    s1 = texts["Rac1 drives spine growth (REF)."]
    assert s1.marker_type == MarkerType.REF and s1.marker_count == 1
    assert s1.clean_text == "Rac1 drives spine growth."

    s2 = texts["PAK is downstream (PMID: 32879322; Battison et al. 2024)."]
    assert s2.marker_type == MarkerType.SUGGESTED
    assert s2.marker_count == 1
    assert [x.value for x in s2.markers[0].suggestions] == ["32879322", "Battison 2024"]
    assert s2.clean_text == "PAK is downstream."

    s3 = texts["Previous studies (Smith et al., 2022) and (Johnson et al., 2021) showed X."]
    assert s3.marker_count == 2
    assert [m.kind for m in s3.markers] == [MarkerType.SUGGESTED, MarkerType.SUGGESTED]
    assert s3.clean_text == "Previous studies and showed X."

    s4 = texts["Nothing here (n = 12)."]
    assert s4.marker_type is None and s4.markers == []

    s5 = texts["Two IDs (PMC11413553, PMC3159129) support this claim."]
    assert len(s5.markers[0].suggestions) == 2
    # Spans are relative to the sentence text
    m = s5.markers[0]
    assert s5.raw_text[m.start:m.end] == "(PMC11413553, PMC3159129)"

    # The DOCX scan used by export sees the same markers in the same order
    docx_markers = handler.find_markers(MarkerConfig.all_on())
    assert [d["text"] for d in docx_markers] == [
        "(REF)", "(PMID: 32879322; Battison et al. 2024)",
        "(Smith et al., 2022)", "(Johnson et al., 2021)",
        "(PMC11413553, PMC3159129)",
    ]


def test_parser_respects_config(tmp_path):
    path = make_docx(tmp_path / "doc.docx", ["Claim (Smith 2020) and (PMID: 1) and (REF)."])
    handler = DocxHandler(path)
    cfg = MarkerConfig(detect_ids=True, detect_author_year=False)
    sentences = DocumentParser(handler, marker_config=cfg).parse()
    MarkerLocator(cfg).locate(sentences)
    (s,) = sentences
    assert s.clean_text == "Claim (Smith 2020) and and."
    assert [m.text for m in s.markers] == ["(PMID: 1)", "(REF)"]
    assert [d["text"] for d in handler.find_markers(cfg)] == ["(PMID: 1)", "(REF)"]


def test_existing_citation_parser_ignores_numbers_inside_markers(tmp_path):
    from src.pipeline.existing_citation_parser import ExistingCitationParser
    path = make_docx(tmp_path / "doc.docx", [
        "Body text with a bracket citation [1] and a suggestion (PMID: 12345) here [2].",
        "References",
        "1. Smith J. Paper one. J Neurosci. 2020;1:1-2. PMID: 111",
        "2. Jones K. Paper two. Cell. 2021;2:3-4. doi:10.1/xyz",
    ])
    result = ExistingCitationParser(DocxHandler(path)).analyze()
    assert result.references_heading_para_idx == 1
    numbers = [c.number for c in result.in_text_citations[0]]
    assert numbers == [1, 2]
