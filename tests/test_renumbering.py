"""Tests for the insert-mode renumbering algorithm and citation identity."""

from src.models.citation import CitationCandidate
from src.models.existing_refs import (
    ExistingBibEntry, ExistingCitationMap, InTextCitation,
)
from src.pipeline.renumbering import (
    CitationKeyIndex, NewMarkerInfo, compute_renumbering,
)


def make_existing(num_entries=3, refs_heading=10, citations=None):
    """ExistingCitationMap with entries 1..n and given in-text citations.

    citations: dict[para_idx, list[(char_offset, number)]]
    """
    bib = {
        i: ExistingBibEntry(
            original_number=i,
            raw_text=f"{i}. Author{i} A. Title {i}. Journal. 2020. doi:10.1000/old{i}",
            body=f"Author{i} A. Title {i}. Journal. 2020. doi:10.1000/old{i}",
            doi=f"10.1000/old{i}",
        )
        for i in range(1, num_entries + 1)
    }
    in_text = {}
    for para_idx, cites in (citations or {}).items():
        in_text[para_idx] = [
            InTextCitation(char_offset=off, number=n) for off, n in cites
        ]
    return ExistingCitationMap(
        bib_entries=bib,
        references_heading_para_idx=refs_heading,
        in_text_citations=in_text,
        max_existing_number=num_entries,
    )


def cand(pmid="", doi="", title="A new paper"):
    return CitationCandidate(pmid=pmid, doi=doi, title=title)


class TestCitationKeyIndex:
    def test_same_pmid_same_key(self):
        ki = CitationKeyIndex()
        assert ki.get_or_assign(pmid="123") == ki.get_or_assign(pmid="123")

    def test_pmid_then_doi_only_resolves_to_same_key(self):
        """The same paper found via PMID first, then DOI-only, is one identity."""
        ki = CitationKeyIndex()
        k1 = ki.get_or_assign(pmid="123", doi="10.1/x", title="Paper X")
        k2 = ki.get_or_assign(doi="10.1/X")  # different case, no pmid
        assert k1 == k2

    def test_title_alias(self):
        ki = CitationKeyIndex()
        k1 = ki.get_or_assign(doi="10.1/y", title="Synaptic Proteomics!")
        k2 = ki.get_or_assign(title="synaptic proteomics")
        assert k1 == k2

    def test_distinct_papers_distinct_keys(self):
        ki = CitationKeyIndex()
        k1 = ki.get_or_assign(pmid="1", title="Alpha")
        k2 = ki.get_or_assign(pmid="2", title="Beta")
        assert k1 != k2

    def test_no_identifiers_empty_key(self):
        assert CitationKeyIndex().get_or_assign() == ""


class TestComputeRenumbering:
    def test_existing_only_keeps_numbers(self):
        existing = make_existing(citations={0: [(0, 1), (10, 2)], 1: [(0, 3)]})
        result = compute_renumbering(existing, [])
        assert result.renumber_map == {1: 1, 2: 2, 3: 3}

    def test_new_marker_before_existing_shifts_numbers(self):
        """A new citation inserted before [1] becomes 1; old 1->2, 2->3."""
        existing = make_existing(
            num_entries=2, citations={1: [(0, 1), (20, 2)]})
        new = [NewMarkerInfo(para_index=0, char_offset=0,
                             citations=[cand(pmid="999")])]
        result = compute_renumbering(existing, new)
        assert result.renumber_map == {1: 2, 2: 3}
        assert result.assignments[1].is_new
        assert result.assignments[2].original_number == 1

    def test_first_occurrence_order_within_paragraph(self):
        """Events in one paragraph are ordered by character offset."""
        existing = make_existing(num_entries=1, citations={0: [(50, 1)]})
        new = [NewMarkerInfo(para_index=0, char_offset=10,
                             citations=[cand(pmid="42")])]
        result = compute_renumbering(existing, new)
        assert result.assignments[1].is_new          # new marker at offset 10
        assert result.assignments[2].original_number == 1

    def test_new_citation_matching_existing_doi_is_merged(self):
        """A new candidate with an existing entry's DOI reuses its number."""
        existing = make_existing(num_entries=2,
                                 citations={0: [(0, 1), (10, 2)]})
        new = [NewMarkerInfo(para_index=1, char_offset=0,
                             citations=[cand(doi="10.1000/old2")])]
        result = compute_renumbering(existing, new)
        # No third entry created
        assert len(result.assignments) == 2
        assert result.number_for_candidate(cand(doi="10.1000/old2")) == 2

    def test_pmid_doi_alias_dedup_across_markers(self):
        """Regression for bug 10: same paper via PMID then DOI-only gets ONE number."""
        existing = make_existing(num_entries=0, citations={})
        paper_full = cand(pmid="123", doi="10.5/z", title="Z Paper")
        paper_doi_only = cand(doi="10.5/z", title="")
        new = [
            NewMarkerInfo(para_index=0, char_offset=0, citations=[paper_full]),
            NewMarkerInfo(para_index=1, char_offset=0, citations=[paper_doi_only]),
        ]
        result = compute_renumbering(existing, new)
        assert len(result.assignments) == 1

    def test_repeated_existing_citation_one_assignment(self):
        existing = make_existing(num_entries=1,
                                 citations={0: [(0, 1)], 2: [(5, 1)]})
        result = compute_renumbering(existing, [])
        assert len(result.assignments) == 1
        assert result.renumber_map == {1: 1}

    def test_number_for_candidate_unknown_returns_none(self):
        existing = make_existing(num_entries=1, citations={0: [(0, 1)]})
        result = compute_renumbering(existing, [])
        assert result.number_for_candidate(cand(pmid="nope")) is None
