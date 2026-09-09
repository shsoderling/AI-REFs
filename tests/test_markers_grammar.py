"""Grammar tests for citation markers (src/utils/markers.py)."""

import pytest

from src.models.markers import MarkerConfig, MarkerType, SuggestionKind
from src.utils.markers import find_markers, strip_markers, parse_marker_content


def one(text, config=None):
    markers = find_markers(text, config)
    assert len(markers) == 1, f"expected exactly one marker in {text!r}, got {markers}"
    return markers[0]


def kinds(marker):
    return [(s.kind, s.value) for s in marker.suggestions]


# ── (REF) / (REFS) keep working exactly as before ─────────────────────

def test_ref_and_refs():
    assert one("Claim (REF).").kind == MarkerType.REF
    assert one("Claim (REFS).").kind == MarkerType.REFS
    assert find_markers("lowercase (ref) is prose") == []
    assert find_markers("(refs) (Ref)") == []


def test_ref_spans_and_text():
    m = one("AlphaMissense(REF) predicts variants.")
    assert (m.start, m.end) == (13, 18)
    assert m.text == "(REF)"


# ── identifier suggestions ────────────────────────────────────────────

@pytest.mark.parametrize("text,kind,value", [
    ("(PMID: 32879322)", SuggestionKind.PMID, "32879322"),
    ("(PMID 32879322)", SuggestionKind.PMID, "32879322"),
    ("(PMID:32879322)", SuggestionKind.PMID, "32879322"),
    ("(pmid: 123456)", SuggestionKind.PMID, "123456"),
    ("(PMC11413553)", SuggestionKind.PMCID, "PMC11413553"),
    ("(PMCID: PMC11413553)", SuggestionKind.PMCID, "PMC11413553"),
    ("(PMC 1234567)", SuggestionKind.PMCID, "PMC1234567"),
    ("(doi: 10.1101/2024.01.03.574066)", SuggestionKind.DOI, "10.1101/2024.01.03.574066"),
    ("(doi:10.1038/s41586-024-07487-w.)", SuggestionKind.DOI, "10.1038/s41586-024-07487-w"),
    ("(https://doi.org/10.1101/2024.01.03.574066)", SuggestionKind.DOI, "10.1101/2024.01.03.574066"),
    ("(http://dx.doi.org/10.1093/nar/gkab1234)", SuggestionKind.DOI, "10.1093/nar/gkab1234"),
    ("(10.1000/xyz123)", SuggestionKind.DOI, "10.1000/xyz123"),
    ("(bioRxiv doi:10.1101/2024.01.03.574066v2)", SuggestionKind.DOI, "10.1101/2024.01.03.574066v2"),
    ("(bioRxiv preprint, doi: 10.1101/2024.01.03.574066)", SuggestionKind.DOI, "10.1101/2024.01.03.574066"),
    ("(https://www.biorxiv.org/content/10.1101/2024.01.03.574066v2)", SuggestionKind.DOI, "10.1101/2024.01.03.574066v2"),
    ("(doi: 10.1016/S0140-6736(20)30183-5)", SuggestionKind.DOI, "10.1016/S0140-6736(20)30183-5"),
    ("(PMCID: 11413553)", SuggestionKind.PMCID, "PMC11413553"),
    ("(PubMed ID: 32879322)", SuggestionKind.PMID, "32879322"),
    ("(PubMed: 32879322)", SuggestionKind.PMID, "32879322"),
])
def test_identifier_forms(text, kind, value):
    m = one(text)
    assert m.kind == MarkerType.SUGGESTED
    assert kinds(m) == [(kind, value)]
    assert m.text == text


def test_multiple_and_mixed_identifiers():
    m = one("(PMC11413553, PMC3159129)")
    assert kinds(m) == [(SuggestionKind.PMCID, "PMC11413553"), (SuggestionKind.PMCID, "PMC3159129")]

    m = one("(PMID: 32879322; Battison et al. 2024)")
    assert [s.kind for s in m.suggestions] == [SuggestionKind.PMID, SuggestionKind.AUTHOR_YEAR]

    m = one("(e.g., PMID: 1, 2, 3)")
    assert [s.value for s in m.suggestions] == ["1", "2", "3"]

    m = one("(PMID 32879322 and PMID 12345)")
    assert [s.value for s in m.suggestions] == ["32879322", "12345"]


def test_ref_token_inside_suggested_marker_requests_extra_search():
    m = one("(REF, PMID: 32879322)")
    assert m.kind == MarkerType.SUGGESTED
    assert m.extra_search == 1
    assert kinds(m) == [(SuggestionKind.PMID, "32879322")]

    m = one("(REFS, PMID: 5)")
    assert m.extra_search == -1

    m = one("(REF, REF)")
    assert m.kind == MarkerType.REFS and m.suggestions == []


# ── author-year suggestions ───────────────────────────────────────────

@pytest.mark.parametrize("text,author,coauthor,et_al,year,suffix", [
    ("(Battison et al. 2024)", "Battison", "", True, 2024, ""),
    ("(Battison et al., 2024)", "Battison", "", True, 2024, ""),
    ("(Battison et al 2024)", "Battison", "", True, 2024, ""),
    ("(BATTISON ET AL., 2024)", "BATTISON", "", True, 2024, ""),
    ("(Smith et. al. 2020)", "Smith", "", True, 2020, ""),
    ("(Smith, 2020)", "Smith", "", False, 2020, ""),
    ("(Smith 2020a)", "Smith", "", False, 2020, "a"),
    ("(Smith and Jones 2020)", "Smith", "Jones", False, 2020, ""),
    ("(Smith & Jones, 2020)", "Smith", "Jones", False, 2020, ""),
    ("(van der Berg et al. 2019)", "van der Berg", "", True, 2019, ""),
    ("(de la Torre et al., 2021)", "de la Torre", "", True, 2021, ""),
    ("(O'Brien et al. 2021)", "O'Brien", "", True, 2021, ""),
    ("(O’Brien 2021)", "O’Brien", "", False, 2021, ""),
    ("(Müller-Schmidt 2018)", "Müller-Schmidt", "", False, 2018, ""),
    ("(Gómez-Gonzalo et al., 2020)", "Gómez-Gonzalo", "", True, 2020, ""),
    ("(McDonald 2020)", "McDonald", "", False, 2020, ""),
    ("(Al-Hasani et al., 2019)", "Al-Hasani", "", True, 2019, ""),
    ("(Smith J, 2020)", "Smith J", "", False, 2020, ""),
    ("(Smith, J.A., 2020)", "Smith", "", False, 2020, ""),
    ("(Zhang J and Zhang L, 2020)", "Zhang J", "Zhang L", False, 2020, ""),
    ("(GTEx Consortium 2020)", "GTEx Consortium", "", False, 2020, ""),
    # surnames that are also calendar / status words pass with et al. or a co-author
    ("(May et al. 2020)", "May", "", True, 2020, ""),
    ("(Grant et al., 2019)", "Grant", "", True, 2019, ""),
    ("(Box and Jenkins 1976)", "Box", "Jenkins", False, 1976, ""),
    ("(See et al. 2020)", "See", "", True, 2020, ""),
    ("(Šimić et al. 2020)", "Šimić", "", True, 2020, ""),
    ("(see, e.g., Smith 2020)", "Smith", "", False, 2020, ""),
    ("(See Smith 2020)", "Smith", "", False, 2020, ""),
])
def test_author_year_forms(text, author, coauthor, et_al, year, suffix):
    m = one(text)
    assert m.kind == MarkerType.SUGGESTED
    (s,) = m.suggestions
    assert s.kind == SuggestionKind.AUTHOR_YEAR
    assert (s.author, s.coauthor, s.et_al, s.year, s.year_suffix) == (author, coauthor, et_al, year, suffix)


def test_author_year_lists():
    m = one("(Smith et al., 2020a, 2020b)")
    assert [(s.author, s.year, s.year_suffix) for s in m.suggestions] == [("Smith", 2020, "a"), ("Smith", 2020, "b")]

    m = one("(Smith 2019, 2021)")
    assert [(s.author, s.year) for s in m.suggestions] == [("Smith", 2019), ("Smith", 2021)]

    m = one("(Smith, 2020; Jones & Lee, 2021)")
    assert [(s.author, s.coauthor, s.year) for s in m.suggestions] == [("Smith", "", 2020), ("Jones", "Lee", 2021)]

    m = one("(Smith 2020 and Jones 2021)")
    assert [(s.author, s.year) for s in m.suggestions] == [("Smith", 2020), ("Jones", 2021)]

    m = one("(Smith et al. 2020; see also Jones 2021)")
    assert len(m.suggestions) == 2


@pytest.mark.parametrize("lead", ["see ", "see also ", "e.g., ", "e.g. ", "reviewed in ", "but see ", "cf. "])
def test_lead_in_words(lead):
    m = one(f"({lead}Smith et al. 2020)")
    assert m.suggestions[0].author == "Smith"


def test_author_year_label():
    m = one("(Battison et al. 2024)")
    assert m.suggestions[0].label == "Battison et al. 2024"
    m = one("(Smith and Jones 2020a)")
    assert m.suggestions[0].label == "Smith and Jones 2020a"
    assert one("(PMID: 5)").suggestions[0].label == "PMID 5"
    assert one("(doi: 10.1000/x)").suggestions[0].label == "doi:10.1000/x"


# ── things that must NOT be markers ───────────────────────────────────

@pytest.mark.parametrize("text", [
    "(n = 12)", "(n=3-5 mice/group)", "(1:1000)", "(P < 0.05)", "(p = 0.003)", "(3-5 days)",
    "(1, 2, 4 and 8 h)", "(95% CI 1.2-3.4)", "(F(2,45) = 3.2)", "(2)", "(1-3)",
    "(Fig. 2B)", "(Figure 3A, B)", "(Figs. 1-3)", "(Table S2)", "(Supplementary Fig. 4)",
    "(see Methods)", "(Methods)", "(Aim 2)", "(Aims 1-3)",
    "(December 2024)", "(March 2020)", "(Spring 2024)", "(Since 2020)", "(FY2024)", "(2016-2020)",
    "(mean 2020 ms)", "(latency 1850 ms; SD 2010)", "(Cohort 2, 2019-2021)",
    "(CRISPR-Cas9)", "(Cas9)", "(Shank3B-/-)", "(Fmr1 KO)", "(AAV9-hSyn-GFP)", "(GraphPad Prism 10.2)",
    "(Python 3.11)", "(MATLAB R2023b)", "(COVID-19 2020)", "(Rac1 2020)", "(Illumina NovaSeq 6000)",
    "(Duke University, 2024)", "(NIH 2020)", "(WHO, 2020)", "(Allen Brain Atlas, 2022)",
    "(Soderling lab, unpublished)", "(unpublished data)", "(personal communication)",
    "(Smith et al., in press)", "(Smith et al. 2020 in mice)", "(Smith et al., 2020, p. 12)",
    "(10.5 mg/kg)", "(PMID: 1,)", "(TBD)", "(TODO)", "(ref genome)", "(REF Smith)", "(add ref)",
    "()", "( )", "(2020)", "(RRID:AB_2020)",
    "(May 2020)", "(Early 2025)", "(Expected 2025)", "(Baseline 2024)", "(Approved 2020)",
    "(World Health Organization, 2020)", "(American Heart Association 2020)", "(UK Biobank 2018)",
    "(MATLAB 2023a)", "(GraphPad Prism 2023)", "(Adobe Illustrator 2023)",
    "(Smith laboratory, 2020)", "(Smith Lab 2020)", "(Tokyo, Japan 2019)",
    "(R01 MH123456, 2020-2025)", "(NCT04123456)", "(Dec. 2024)",
])
def test_not_markers(text):
    assert find_markers(text) == [], text


def test_all_caps_surname_with_et_al_still_counts():
    assert one("(SMITH et al. 2020)").suggestions[0].author == "SMITH"


# ── configuration switches ────────────────────────────────────────────

def test_config_toggles():
    text = "(PMID: 1) (Smith 2020) (REF)"
    assert [m.kind for m in find_markers(text, MarkerConfig.legacy())] == [MarkerType.REF]
    assert [m.kind for m in find_markers(text, MarkerConfig(detect_ids=True, detect_author_year=False))] == [
        MarkerType.SUGGESTED, MarkerType.REF]
    assert [m.kind for m in find_markers(text, MarkerConfig(detect_ids=False, detect_author_year=True))] == [
        MarkerType.SUGGESTED, MarkerType.REF]
    assert len(find_markers(text, MarkerConfig.all_on())) == 3


# ── multiple markers in one sentence, spans, stripping ────────────────

def test_back_to_back_markers_are_distinct():
    text = "Case 4: Multiple citations in sequence: (Smith et al., 2022) (REF) shows the progression."
    ms = find_markers(text)
    assert [m.kind for m in ms] == [MarkerType.SUGGESTED, MarkerType.REF]
    assert ms[0].end <= ms[1].start
    assert text[ms[0].start:ms[0].end] == "(Smith et al., 2022)"
    assert text[ms[1].start:ms[1].end] == "(REF)"

    ms = find_markers("(Smith et al., 2022)(REF)")
    assert [m.kind for m in ms] == [MarkerType.SUGGESTED, MarkerType.REF]

    ms = find_markers("(Smith et al., 2022) (Fig. 2)")
    assert len(ms) == 1


def test_strip_markers_matches_old_behaviour_for_ref():
    assert strip_markers("Synaptic plasticity is crucial (REF).") == "Synaptic plasticity is crucial."
    assert strip_markers("AlphaMissense(REF) predicts, while PepPrCLIP(REF) models.") == \
        "AlphaMissense predicts, while PepPrCLIP models."
    assert strip_markers("Using BioID (REFS), we identified interactors.") == "Using BioID, we identified interactors."


def test_strip_markers_removes_suggested_markers():
    text = "Prior work (Smith et al., 2022) and (REF) showed X (PMID: 1)."
    assert strip_markers(text) == "Prior work and showed X."
    assert strip_markers(text, MarkerConfig.legacy()) == "Prior work (Smith et al., 2022) and showed X (PMID: 1)."


def test_parse_marker_content_direct():
    assert parse_marker_content("REF").kind == MarkerType.REF
    assert parse_marker_content("") is None
    assert parse_marker_content("nothing here") is None


def test_nested_parenthetical_exposes_inner_markers():
    ms = find_markers("(see Smith et al. (2020) and (PMID: 1))")
    assert [(m.kind, m.text) for m in ms] == [(MarkerType.SUGGESTED, "(PMID: 1)")]
    text = "Outer (with (PMID: 7) inside) end."
    (m,) = find_markers(text)
    assert text[m.start:m.end] == "(PMID: 7)"
