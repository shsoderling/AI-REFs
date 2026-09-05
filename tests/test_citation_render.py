"""CSL citation layout parsing and cluster rendering (Task 13)."""
import pytest

from src.models.citation import Author, CitationCandidate
from src.models.project import CitationStyle
from src.pipeline.citation_render import (
    CitationLayout, layout_for_existing_shape, parse_csl_layout, render_author_date,
    render_cluster, render_numbers,
)


@pytest.mark.parametrize("style,kind,collapse", [
    (CitationStyle.NIH_GRANT, "numeric-superscript", True),
    (CitationStyle.NATURE, "numeric-superscript", True),
    (CitationStyle.VANCOUVER, "numeric-paren", True),    # vancouver.csl wraps in parentheses
    (CitationStyle.NSF_GRANT, "numeric-bracket", True),
    (CitationStyle.IEEE, "numeric-bracket", False),
    (CitationStyle.SCIENCE, "numeric-paren", True),
    (CitationStyle.APA, "author-date", False),
])
def test_layout_kind_and_collapse(style, kind, collapse):
    layout = parse_csl_layout(style)
    assert layout.render_kind == kind
    assert layout.collapse is collapse


def test_all_styles_parse():
    for style in CitationStyle:
        layout = parse_csl_layout(style)
        assert isinstance(layout, CitationLayout)
        assert layout.render_kind in ("numeric-superscript", "numeric-bracket",
                                      "numeric-paren", "author-date")


def test_author_date_layout_gets_parenthesised_defaults():
    layout = parse_csl_layout(CitationStyle.APA)
    assert (layout.prefix, layout.suffix, layout.delimiter) == ("(", ")", "; ")


def test_render_numbers_collapses_only_when_csl_says_so():
    sup = CitationLayout("", "", ",", False, True, True)
    assert render_numbers([5, 3, 4, 3, 9], sup) == "3-5,9"
    bracket = CitationLayout("[", "]", ", ", False, False, False)
    assert render_numbers([3, 4, 5], bracket) == "[3, 4, 5]"
    ieee = parse_csl_layout(CitationStyle.IEEE)          # brackets on each number, no collapse
    assert render_numbers([3, 4, 5], ieee) == "[3], [4], [5]"
    vanc = CitationLayout("[", "]", ", ", False, False, True)
    assert render_numbers([1, 2, 3, 7], vanc) == "[1-3, 7]"
    assert render_numbers([1, 2], sup) == "1,2"            # two consecutive: no range
    assert render_numbers([], sup) == "[?]"


def test_render_cluster_author_date_and_unresolved():
    ad = parse_csl_layout(CitationStyle.APA)
    c1 = CitationCandidate(title="a", year=2020,
                           authors=[Author(last_name="Smith"), Author(last_name="Doe"), Author(last_name="Roe")])
    c2 = CitationCandidate(title="b", year=2021, authors=[Author(last_name="Lee")])
    assert render_cluster([c1, c2], [], ad) == "(Smith et al., 2020; Lee, 2021)"
    assert render_author_date([c1, c1], ad) == "(Smith et al., 2020)"   # deduplicated
    assert render_cluster([], [], ad) == "[?]"
    sup = parse_csl_layout(CitationStyle.NIH_GRANT)
    assert render_cluster([c1, c2], [4, 2], sup) == "2,4"


def test_existing_shape_override():
    base = parse_csl_layout(CitationStyle.SCIENCE)
    assert layout_for_existing_shape(base, True).render_kind == "numeric-superscript"
    assert layout_for_existing_shape(base, False).render_kind == "numeric-bracket"
    assert layout_for_existing_shape(base, False).collapse is base.collapse
    ad = parse_csl_layout(CitationStyle.APA)
    assert layout_for_existing_shape(ad, True) is ad
