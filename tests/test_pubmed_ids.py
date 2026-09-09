"""PubMed / Europe PMC identifier helpers with mocked HTTP sessions."""

import json
from unittest.mock import MagicMock

from src.services.pubmed_client import PubMedClient, IDCONV_URL, EUTILS_BASE
from src.services.europepmc_client import EuropePMCClient
from src.storage.cache_db import CacheDB


EFETCH_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
 <PubmedArticle>
  <MedlineCitation><PMID>32879322</PMID>
   <Article>
    <Journal><Title>Journal of Things</Title><ISOAbbreviation>J Things</ISOAbbreviation>
     <JournalIssue><Volume>5</Volume><Issue>2</Issue><PubDate><Year>2020</Year></PubDate></JournalIssue></Journal>
    <ArticleTitle>A paper about spines</ArticleTitle>
    <Pagination><MedlinePgn>1-10</MedlinePgn></Pagination>
    <Abstract><AbstractText>Spines grow.</AbstractText></Abstract>
    <AuthorList><Author><LastName>Battison</LastName><ForeName>Anna</ForeName><Initials>A</Initials></Author></AuthorList>
    <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
   </Article>
  </MedlineCitation>
  <PubmedData><ArticleIdList>
   <ArticleId IdType="pubmed">32879322</ArticleId>
   <ArticleId IdType="doi">10.1000/spines</ArticleId>
   <ArticleId IdType="pmc">PMC7000001</ArticleId>
  </ArticleIdList></PubmedData>
 </PubmedArticle>
</PubmedArticleSet>
"""


class FakeResponse:
    def __init__(self, payload=None, text="", status=200):
        self._payload = payload
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def make_client(router):
    client = PubMedClient(email="t@example.com", cache_db=CacheDB(":memory:"))
    client._min_interval = 0
    session = MagicMock()

    def get(url, params=None, timeout=None):
        return router(url, params or {})

    session.get.side_effect = get
    client._session = session
    return client, session


def test_efetch_parses_pmcid():
    def router(url, params):
        assert url.endswith("efetch.fcgi")
        return FakeResponse(text=EFETCH_XML)

    client, _ = make_client(router)
    art = client.fetch_article("32879322")
    assert art.pmcid == "PMC7000001"
    assert art.doi == "10.1000/spines"
    assert art.authors[0].last_name == "Battison"


def test_pmcid_to_pmid_uses_idconv_then_cache():
    calls = []

    def router(url, params):
        calls.append(url)
        if url == IDCONV_URL:
            assert params["ids"] == "PMC7000001"
            return FakeResponse(payload={"records": [{"pmcid": "PMC7000001", "pmid": "32879322"}]})
        raise AssertionError(f"unexpected call {url}")

    client, _ = make_client(router)
    assert client.pmcid_to_pmid("PMC7000001") == "32879322"
    assert client.pmcid_to_pmid("pmc7000001") == "32879322"  # cached, no new call
    assert client.pmcid_to_pmid("7000001") == "32879322"
    assert len(calls) == 1


def test_pmcid_to_pmid_falls_back_to_elink():
    def router(url, params):
        if url == IDCONV_URL:
            return FakeResponse(payload={"records": [{"pmcid": "PMC1", "status": "error", "errmsg": "invalid article id"}]})
        if url.endswith("elink.fcgi"):
            assert params["dbfrom"] == "pmc" and params["db"] == "pubmed" and params["id"] == "1"
            return FakeResponse(payload={"linksets": [{"linksetdbs": [{"dbto": "pubmed", "links": ["424242"]}]}]})
        raise AssertionError(url)

    client, _ = make_client(router)
    assert client.pmcid_to_pmid("PMC1") == "424242"


def test_pmcid_to_pmid_not_found_returns_empty_and_is_not_cached():
    calls = []

    def router(url, params):
        calls.append(url)
        if url == IDCONV_URL:
            return FakeResponse(status=500)
        if url.endswith("elink.fcgi"):
            return FakeResponse(payload={"linksets": [{"linksetdbs": []}]})
        raise AssertionError(url)

    client, _ = make_client(router)
    assert client.pmcid_to_pmid("PMC2") == ""
    assert client.pmcid_to_pmid("PMC2") == ""
    assert len(calls) == 4  # both routes retried on the second call (nothing cached)


def test_find_pmids_by_doi_tries_doi_then_aid():
    seen = []

    def router(url, params):
        assert url.endswith("esearch.fcgi")
        seen.append(params["term"])
        if params["term"].endswith("[doi]"):
            return FakeResponse(payload={"esearchresult": {"idlist": [], "count": "0"}})
        return FakeResponse(payload={"esearchresult": {"idlist": ["5", "6"], "count": "2"}})

    client, _ = make_client(router)
    assert client.find_pmids_by_doi("10.1000/x") == ["5", "6"]
    assert seen == ["10.1000/x[doi]", "10.1000/x[aid]"]
    assert client.find_pmid_by_doi("") == ""


def test_search_author_year_query_ladder():
    seen = []

    def router(url, params):
        seen.append(params["term"])
        if params["term"] == "Battison[au] AND 2023:2025[dp]":
            return FakeResponse(payload={"esearchresult": {"idlist": ["9"], "count": "1"}})
        return FakeResponse(payload={"esearchresult": {"idlist": [], "count": "0"}})

    client, _ = make_client(router)
    pmids, total = client.search_author_year("Battison", 2024)
    assert pmids == ["9"] and total == 1
    assert seen == [
        "Battison[1au] AND 2024[dp]",
        "Battison[1au] AND 2023:2025[dp]",
        "Battison[au] AND 2024[dp]",
        "Battison[au] AND 2023:2025[dp]",
    ]

    seen.clear()
    client.search_author_year("Smith", 2020, coauthor="Jones")
    assert seen[0] == "Smith[1au] AND 2020[dp] AND Jones[au]"


def test_europepmc_pmcid_and_author_year():
    client = EuropePMCClient(cache_db=CacheDB(":memory:"))
    client._min_interval = 0
    queries = []

    def get(url, params=None, timeout=None):
        queries.append(params["query"])
        return FakeResponse(payload={"resultList": {"result": [{
            "title": "Paper.", "pmid": "77", "pmcid": "PMC77", "doi": "10.1/e", "pubYear": "2024",
            "authorString": "Battison A, Smith B.",
        }]}})

    client._session = MagicMock()
    client._session.get.side_effect = get
    art = client.fetch_by_pmcid("pmc77")
    assert art.pmcid == "PMC77" and art.pmid == "77" and art.title == "Paper"
    assert queries[-1] == "PMCID:PMC77"

    res = client.search_author_year("Battison", 2024, coauthor="Smith")
    assert res and queries[-1] == 'AUTH:"Battison" AND PUB_YEAR:2024 AND AUTH:"Smith"'
