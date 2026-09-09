"""Tests for the insert-mode renumbering algorithm and citation identity."""

from src.models.citation import CitationCandidate
from src.models.existing_refs import (
    ExistingBibEntry, ExistingCitationMap, InTextCitation,
)
from src.pipeline.renumbering import (
    CitationKeyIndex, NewMarkerInfo, build_events, compute_renumbering,
    compute_renumbering_from_events,
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


class TestEventsEngine:
    def test_marker_after_heading_gets_a_number(self):
        existing = make_existing(num_entries=1, refs_heading=2, citations={0: [(0, 1)]})
        new = [NewMarkerInfo(para_index=5, char_offset=0, citations=[cand(pmid="9")])]
        result = compute_renumbering(existing, new)
        assert result.number_for_candidate(cand(pmid="9")) == 2

    def test_uncited_parsed_entries_are_seeded_and_reported(self):
        existing = make_existing(num_entries=4, refs_heading=3, citations={0: [(0, 2)]})
        result = compute_renumbering(existing, [])
        assert sorted(result.assignments) == [1, 2, 3, 4]
        assert result.assignments[1].original_number == 2       # cited first
        assert [result.assignments[n].original_number for n in (2, 3, 4)] == [1, 3, 4]
        assert result.seeded_uncited == [1, 3, 4]
        # Seeded entries are existing citations too: their old -> new mapping
        # is recorded so the preview and export stats see where they moved.
        assert result.renumber_map == {2: 1, 1: 2, 3: 3, 4: 4}
        assert result.next_number == 5

    def test_seeding_can_be_disabled(self):
        existing = make_existing(num_entries=3, refs_heading=3, citations={0: [(0, 1)]})
        result = compute_renumbering(existing, [], seed_entries=False)
        assert sorted(result.assignments) == [1]
        assert result.seeded_uncited == []

    def test_uncited_duplicate_of_a_cited_paper_is_merged_not_seeded(self):
        """An uncited entry with a cited entry's DOI maps to that number."""
        existing = make_existing(num_entries=2, refs_heading=3, citations={0: [(0, 1)]})
        existing.bib_entries[2].doi = existing.bib_entries[1].doi
        result = compute_renumbering(existing, [])
        assert sorted(result.assignments) == [1]
        assert result.renumber_map == {1: 1, 2: 1}
        assert result.seeded_uncited == []

    def test_uncited_entry_refound_by_a_new_marker_merges_into_it(self):
        """A new candidate carrying an uncited entry's DOI owns that number."""
        existing = make_existing(num_entries=2, refs_heading=3, citations={0: [(0, 1)]})
        refound = cand(doi="10.1000/old2")
        new = [NewMarkerInfo(para_index=1, char_offset=0, citations=[refound])]
        result = compute_renumbering(existing, new)
        assert sorted(result.assignments) == [1, 2]
        assert result.assignments[2].is_new
        assert result.number_for_candidate(refound) == 2
        assert result.renumber_map == {1: 1, 2: 2}
        assert result.seeded_uncited == []

    def test_events_are_ordered_and_existing_wins_ties(self):
        existing = make_existing(num_entries=2, refs_heading=4, citations={1: [(5, 2)], 0: [(0, 1)]})
        new = [NewMarkerInfo(para_index=1, char_offset=5, citations=[cand(pmid="7")])]
        ev = build_events(existing, new)
        assert [(e.para_index, e.char_offset, e.kind) for e in ev] == [
            (0, 0, "existing"), (1, 5, "existing"), (1, 5, "new")]
        assert [e.number for e in ev[:2]] == [1, 2]
        assert ev[2].citations == [cand(pmid="7")]

    def test_existing_events_stop_at_the_heading(self):
        existing = make_existing(num_entries=2, refs_heading=1,
                                 citations={0: [(0, 1)], 1: [(0, 2)], 3: [(0, 2)]})
        assert [(e.para_index, e.number) for e in build_events(existing, [])] == [(0, 1)]

    def test_no_heading_walks_every_paragraph(self):
        existing = make_existing(num_entries=2, refs_heading=-1,
                                 citations={0: [(0, 1)], 7: [(0, 2)]})
        assert [(e.para_index, e.number) for e in build_events(existing, [])] == [(0, 1), (7, 2)]

    def test_from_events_matches_wrapper(self):
        existing = make_existing(num_entries=2, refs_heading=4, citations={0: [(0, 2), (3, 1)]})
        new = [NewMarkerInfo(para_index=0, char_offset=1, citations=[cand(doi="10.1/n")])]
        a = compute_renumbering(existing, new)
        b = compute_renumbering_from_events(existing, build_events(existing, new))
        assert a.renumber_map == b.renumber_map
        assert {n: x.bib_key for n, x in a.assignments.items()} == {n: x.bib_key for n, x in b.assignments.items()}
