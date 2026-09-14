"""The skill's CSL bibliography renderer.

Run from the repository root: ``python3 -m pytest skills/ai-refs-citations/tests -q``
(the app's own suite is unaffected; nothing here imports ``src``).
"""

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import common  # noqa: E402,F401  (puts the vendored airefs package on the path)
import csl_bibliography as cb  # noqa: E402

from airefs.models.citation import Author, CitationCandidate  # noqa: E402
from airefs.models.project import AUTHOR_DATE_STYLES, CitationStyle, get_csl_path  # noqa: E402
from airefs.pipeline.csl_mapping import to_csl_item  # noqa: E402


def _author(last, first="", initials=""):
    return Author(last_name=last, first_name=first, initials=initials)


ARTICLE = CitationCandidate(
    pmid="32879322", pmcid="PMC7467931", doi="10.1038/s41467-020-18074-8",
    title="Interneuron-specific plasticity at parvalbumin and somatostatin inhibitory synapses",
    authors=[_author("Udakis", "Matt", "M"), _author("Pedrosa", "Victor", "V"),
             _author("Chamberlain", "Sophie E L", "SEL"), _author("Clopath", "Claudia", "C"),
             _author("Mellor", "Jack R", "JR")],
    year=2020, journal="Nature communications", journal_abbrev="Nat Commun",
    volume="11", issue="1", pages="4395", source="pubmed")

MANY_AUTHORS = CitationCandidate(
    pmid="38577773", doi="10.1093/brain/awae106",
    title="Tiam1-mediated maladaptive plasticity underlying morphine tolerance",
    authors=[_author("Yao", "Changqun", "C"), _author("Fang", "Xing", "X"),
             _author("Ru", "Qin", "Q"), _author("Li", "Wei", "W"), _author("Li", "Jun", "J"),
             _author("Mehsein", "Zeinab", "Z"), _author("Tolias", "Kimberley F", "KF"),
             _author("Li", "Lingyong", "L")],
    year=2024, journal="Brain", journal_abbrev="Brain", volume="147", issue="7",
    pages="2507-2521", source="pubmed")

PREPRINT = CitationCandidate(
    doi="10.1101/2024.01.03.574066",
    title="Synaptic proteomes of cortical interneuron classes",
    authors=[_author("Battison", "Alexandra", "AS"), _author("LoTurco", "Joseph", "J")],
    year=2024, journal="bioRxiv", journal_abbrev="bioRxiv", source="biorxiv")

INITIALS_ONLY = CitationCandidate(
    pmid="11111111", title="A paper whose authors arrived as initials",
    authors=[_author("Smith", "", "JA"), _author("Lee", "", "B")],
    year=2020, journal="Cell", journal_abbrev="Cell", volume="182", pages="1-10")


def render(candidate, style, number=3):
    bib = cb.CslBibliography(str(get_csl_path(style)))
    return bib.render(to_csl_item(candidate), number)


# ── every bundled style ──────────────────────────────────────────────

@pytest.mark.parametrize("style", list(CitationStyle), ids=lambda s: s.value)
def test_every_style_renders_a_journal_article(style):
    text = render(ARTICLE, style)
    assert "Udakis" in text
    assert "2020" in text
    assert "Interneuron-specific plasticity" in text.replace("Plasticity", "plasticity")
    lowered = text.lower()
    assert "nat commun" in lowered or "nature communications" in lowered
    assert "{" not in text and "None" not in text


@pytest.mark.parametrize("style", list(CitationStyle), ids=lambda s: s.value)
def test_numbering_matches_the_style_class(style):
    text = render(ARTICLE, style, number=7)
    if style in AUTHOR_DATE_STYLES:
        assert not text.lstrip().startswith("7")
    else:
        assert text.lstrip()[:3] in ("7. ", "(7)", "[7]"), text[:20]


@pytest.mark.parametrize("style", list(CitationStyle), ids=lambda s: s.value)
def test_no_orphaned_punctuation_when_volume_and_pages_are_missing(style):
    text = render(PREPRINT, style)
    assert ";." not in text and ",." not in text and ":." not in text
    assert ".." not in text
    assert not text.rstrip().endswith(";")
    assert "Battison" in text


# ── style-specific behaviour ─────────────────────────────────────────

def test_nih_grant_keeps_the_pmcid_nih_asks_for():
    assert "PMC7467931" in render(ARTICLE, CitationStyle.NIH_GRANT)


def test_apa_inverts_names_and_uses_an_ampersand():
    text = render(ARTICLE, CitationStyle.APA)
    assert text.startswith("Udakis, M., Pedrosa, V.")
    assert "& Mellor, J. R." in text
    assert "(2020)" in text


def test_nature_truncates_to_the_first_author():
    text = render(MANY_AUTHORS, CitationStyle.NATURE)
    assert "Yao, C. et al." in text
    assert "Fang" not in text


def test_vancouver_lists_six_authors_then_et_al():
    text = render(MANY_AUTHORS, CitationStyle.VANCOUVER)
    assert "Mehsein Z, et al." in text
    assert "Tolias" not in text


def test_ieee_quotes_the_title_and_labels_pages():
    text = render(MANY_AUTHORS, CitationStyle.IEEE)
    assert "“Tiam1-mediated" in text
    assert "vol. 147" in text and "pp. 2507-2521" in text


def test_author_date_styles_carry_the_year_without_a_number():
    for style in (CitationStyle.APA, CitationStyle.ELIFE, CitationStyle.CHICAGO_AUTHOR_DATE):
        text = render(ARTICLE, style, number=4)
        assert "2020" in text
        assert not text.lstrip().startswith("4")


def test_initials_arriving_as_one_blob_are_split():
    text = render(INITIALS_ONLY, CitationStyle.NIH_GRANT)
    assert "Smith JA" in text, text
    assert "Smith J." not in text


def test_a_style_that_uses_page_first_gets_it():
    text = render(MANY_AUTHORS, CitationStyle.APS)
    assert "2507" in text


# ── installation over the app's formatter ────────────────────────────

def test_install_replaces_the_formatter_everywhere_it_is_bound():
    from airefs.pipeline import bib_format, docx_export, tracked_renumber
    original = bib_format.format_bib_entry
    try:
        cb.install()
        assert docx_export.format_bib_entry is cb.format_bib_entry
        assert tracked_renumber.format_bib_entry is cb.format_bib_entry
        entry = docx_export.format_bib_entry(ARTICLE, 2, CitationStyle.APA)
        assert entry.startswith("Udakis, M.")
    finally:
        cb.uninstall()
    assert bib_format.format_bib_entry is original
    assert docx_export.format_bib_entry is original


def test_append_ids_adds_identifiers_a_style_omits():
    try:
        cb.install(append_identifiers=True)
        entry = cb.format_bib_entry(ARTICLE, 1, CitationStyle.VANCOUVER)
        assert "PMID: 32879322" in entry
        assert "doi:10.1038/s41467-020-18074-8" in entry
    finally:
        cb.uninstall()


def test_a_broken_style_file_falls_back_to_the_app_format(monkeypatch, tmp_path):
    """A style that cannot be parsed must not produce a half-built entry."""
    broken = tmp_path / "broken.csl"
    broken.write_text("<style><not-csl></style>", encoding="utf-8")
    monkeypatch.setattr(cb, "_CACHE", {})
    monkeypatch.setattr("airefs.models.project.get_csl_path", lambda style: broken)
    entry = cb.format_bib_entry(ARTICLE, 5, CitationStyle.VANCOUVER)
    assert entry.startswith("5. Udakis")
    assert "Nat Commun" in entry
    monkeypatch.setattr(cb, "_CACHE", {})


def test_rendering_never_raises_for_a_record_stripped_to_its_title():
    bare = CitationCandidate(title="A title and nothing else")
    for style in CitationStyle:
        text = cb.format_bib_entry(bare, 1, style)   # falls back when too thin
        assert text
