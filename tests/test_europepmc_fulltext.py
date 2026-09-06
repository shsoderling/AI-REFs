"""Europe PMC: PMC ids and open-access flags on results, cached full text."""

from types import SimpleNamespace

from src.services.europepmc_client import (
    EUROPEPMC_REST_BASE, EuropePMCClient, normalize_pmcid,
)
from src.storage.cache_db import CacheDB
from tests.test_jats import JATS


def _client():
    return EuropePMCClient(cache_db=CacheDB(":memory:"))


def test_result_to_candidate_records_pmcid_and_open_access():
    c = _client()._result_to_candidate({
        "title": "Rac1 in spines.", "pmid": "11", "doi": "10.1/x", "pubYear": "2020",
        "pmcid": "PMC7000000", "isOpenAccess": "Y", "authorString": "Smith J",
    })
    assert c.pmcid == "PMC7000000" and c.is_open_access is True
    closed = _client()._result_to_candidate({"title": "T", "isOpenAccess": "N"})
    assert closed.pmcid == "" and closed.is_open_access is False


def test_normalize_pmcid():
    assert normalize_pmcid(" pmc123 ") == "PMC123"
    assert normalize_pmcid("123") == "PMC123"
    assert normalize_pmcid("") == ""


def test_fetch_full_text_parses_caches_and_records_misses(monkeypatch):
    client = _client()
    client._min_interval = 0
    calls = []

    def fake_get(url, params=None, timeout=30):
        calls.append(url)
        if url.endswith("/PMC1/fullTextXML"):
            return SimpleNamespace(status_code=200, text=JATS)
        return SimpleNamespace(status_code=404, text="")

    monkeypatch.setattr(client._session, "get", fake_get)

    paragraphs = client.fetch_full_text("pmc1")
    assert [t for t, _ in paragraphs] == ["Introduction", "Results", "Results > Time course", "Discussion"]
    assert calls == [f"{EUROPEPMC_REST_BASE}/PMC1/fullTextXML"]

    again = client.fetch_full_text("PMC1")               # served from the cache
    assert again == paragraphs and len(calls) == 1

    assert client.fetch_full_text("PMC404") is None      # 404: no retry, miss cached
    assert client.fetch_full_text("PMC404") is None
    assert len(calls) == 2
    assert client.fetch_full_text("") is None
