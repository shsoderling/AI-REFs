"""Tests for parsing/enriching existing bibliography entries (no network)."""

from src.models.citation import Author, CitationCandidate
from src.models.existing_refs import ExistingBibEntry, ExistingCitationMap
from src.pipeline.existing_enrichment import (
    ExistingRefEnricher, parse_entry_fields,
)
from src.storage.cache_db import CacheDB


class TestParseEntryFields:
    def test_full_nlm_entry(self):
        f = parse_entry_fields(
            "Smith J, Doe A. A great paper on synapses. "
            "J Neurosci. 2020;40(3):100-110.")
        assert f["first_author_last"] == "Smith"
        assert f["title"] == "A great paper on synapses"
        assert f["journal"] == "J Neurosci"
        assert f["year"] == 2020
        assert f["volume"] == "40"
        assert f["first_page"] == "100"

    def test_volume_without_issue(self):
        f = parse_entry_fields("Jones B. Title here. Nature. 2019;567:1-5.")
        assert f["volume"] == "567"
        assert f["first_page"] == "1"

    def test_missing_fields_safe(self):
        f = parse_entry_fields("Some unstructured text without structure")
        assert f["year"] == 0
        assert f["volume"] == ""


from tests.fakes import FakePubMed as _FakePubMed  # noqa: E402


def _entry(num, body):
    return ExistingBibEntry(original_number=num, raw_text=f"{num}. {body}", body=body)


def _article(pmid, title, year, doi=""):
    return CitationCandidate(pmid=pmid, doi=doi, title=title, year=year,
                             authors=[Author(last_name="Smith", initials="J")])


class TestEnricher:
    def test_citation_match_path(self):
        body = "Smith J. The role of Rac1 in dendritic spines. J Neurosci. 2020;40(3):100-110."
        entry = _entry(1, body)
        existing = ExistingCitationMap(bib_entries={1: entry},
                                       references_heading_para_idx=5)
        fake = _FakePubMed(
            by_citation={("J Neurosci", 2020, "40", "100", "Smith"): "12345"},
            articles={"12345": _article(
                "12345", "The role of Rac1 in dendritic spines", 2020,
                doi="10.1/rac1")},
        )
        n = ExistingRefEnricher(fake, cache=fake.cache).enrich(existing)
        assert n == 1
        assert entry.pmid == "12345"
        assert entry.doi == "10.1/rac1"

    def test_title_search_fallback(self):
        title = "Comprehensive analysis of synaptic proteomes in mouse cortex"
        body = f"Doe A. {title}. Nature. 2021."
        entry = _entry(2, body)
        existing = ExistingCitationMap(bib_entries={2: entry},
                                       references_heading_para_idx=5)
        fake = _FakePubMed(
            by_search={f'"{title}"[Title] AND ("2020"[PDAT] : "2022"[PDAT])':
                       (["999"], 1)},
            articles={"999": _article("999", title, 2021, doi="10.2/syn")},
        )
        n = ExistingRefEnricher(fake, cache=fake.cache).enrich(existing)
        assert n == 1
        assert entry.pmid == "999"

    def test_rejects_low_similarity_match(self):
        entry = _entry(3, "Roe C. Totally different subject matter. J. 2020;1:1-2.")
        existing = ExistingCitationMap(bib_entries={3: entry},
                                       references_heading_para_idx=5)
        fake = _FakePubMed(
            by_citation={("J", 2020, "1", "1", "Roe"): "555"},
            articles={"555": _article("555", "An unrelated paper about birds", 2020)},
        )
        n = ExistingRefEnricher(fake, cache=fake.cache).enrich(existing)
        assert n == 0
        assert entry.pmid == ""

    def test_skips_entries_with_existing_ids(self):
        entry = ExistingBibEntry(original_number=1, raw_text="1. X", body="X",
                                 pmid="111")
        existing = ExistingCitationMap(bib_entries={1: entry},
                                       references_heading_para_idx=5)
        fake = _FakePubMed()
        n = ExistingRefEnricher(fake, cache=fake.cache).enrich(existing)
        assert n == 0

    def test_miss_is_cached(self):
        entry = _entry(4, "Lee D. Some title that won't match. J. 2020;2:5-9.")
        existing = ExistingCitationMap(bib_entries={4: entry},
                                       references_heading_para_idx=5)
        fake = _FakePubMed()
        enricher = ExistingRefEnricher(fake, cache=fake.cache)
        enricher.enrich(existing)
        # Second run should hit the cached miss (no exceptions, still 0)
        assert enricher.enrich(existing) == 0
