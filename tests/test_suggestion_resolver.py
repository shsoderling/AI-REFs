"""SuggestionResolver lookup order with stub clients."""

from src.models.citation import Author, CitationCandidate
from src.models.markers import SuggestedCitation, SuggestionKind
from src.services.suggestion_resolver import SuggestionResolver


def cand(pmid="", doi="", title="T", pmcid="", authors=(), year=2020, source="pubmed"):
    return CitationCandidate(
        pmid=pmid, doi=doi, pmcid=pmcid, title=title, year=year, source=source,
        authors=[Author(last_name=a) for a in authors],
    )


class StubPubMed:
    def __init__(self, articles=None, doi_map=None, pmcid_map=None, author_hits=None, author_total=0):
        self.articles = articles or {}
        self.doi_map = doi_map or {}
        self.pmcid_map = pmcid_map or {}
        self.author_hits = author_hits or []
        self.author_total = author_total
        self.calls = []

    def fetch_article(self, pmid):
        self.calls.append(("fetch", pmid))
        return self.articles.get(pmid)

    def fetch_articles(self, pmids):
        return [self.articles[p] for p in pmids if p in self.articles]

    def find_pmids_by_doi(self, doi, max_results=3):
        self.calls.append(("doi", doi))
        return list(self.doi_map.get(doi.lower(), []))

    def pmcid_to_pmid(self, pmcid):
        self.calls.append(("pmcid", pmcid))
        return self.pmcid_map.get(pmcid.upper(), "")

    def search_author_year(self, last_name, year, coauthor="", max_results=10):
        self.calls.append(("author", last_name, year, coauthor))
        return list(self.author_hits), self.author_total


class StubEPMC:
    def __init__(self, by_pmid=None, by_doi=None, by_pmcid=None, by_author=None):
        self.by_pmid = by_pmid or {}
        self.by_doi = by_doi or {}
        self.by_pmcid = by_pmcid or {}
        self.by_author = by_author or []

    def fetch_by_pmid(self, pmid):
        return self.by_pmid.get(pmid)

    def fetch_by_doi(self, doi):
        return self.by_doi.get(doi.lower())

    def fetch_by_pmcid(self, pmcid):
        return self.by_pmcid.get(pmcid.upper())

    def search_author_year(self, last_name, year, coauthor="", max_results=10):
        return list(self.by_author)


class StubBioRxiv:
    def __init__(self, by_doi=None):
        self.by_doi = by_doi or {}
        self.seen = []

    def fetch_preprint(self, doi):
        self.seen.append(doi)
        return self.by_doi.get(doi.lower())


class StubLibrary:
    def __init__(self, **kw):
        self.kw = kw

    def find_by_pmid(self, pmid):
        return self.kw.get("pmid", {}).get(pmid)

    def find_by_doi(self, doi):
        return self.kw.get("doi", {}).get(doi.lower())

    def find_by_pmcid(self, pmcid):
        return self.kw.get("pmcid", {}).get(pmcid.upper())

    def find_by_author_year(self, last, year, coauthor=""):
        return list(self.kw.get("author", []))


def sug(kind, value, **kw):
    return SuggestedCitation(kind=kind, raw=value, value=value, **kw)


def test_pmid_prefers_library_then_pubmed_then_epmc():
    lib_hit = cand(pmid="1", title="lib", source="user_library")
    pm_hit = cand(pmid="2", title="pubmed")
    ep_hit = cand(pmid="3", title="epmc", source="europepmc")
    r = SuggestionResolver(
        pubmed=StubPubMed(articles={"2": pm_hit}),
        europepmc=StubEPMC(by_pmid={"3": ep_hit}),
        user_library=StubLibrary(pmid={"1": lib_hit}),
    )
    assert r.resolve(sug(SuggestionKind.PMID, "1")).source == "user_library"
    assert r.resolve(sug(SuggestionKind.PMID, "2")).source == "pubmed"
    assert r.resolve(sug(SuggestionKind.PMID, "3")).source == "europepmc"
    missing = r.resolve(sug(SuggestionKind.PMID, "4"))
    assert not missing.resolved and "PMID 4" in missing.error


def test_pmcid_conversion_and_fallback():
    art = cand(pmid="10", title="via pubmed")
    pubmed = StubPubMed(articles={"10": art}, pmcid_map={"PMC5": "10"})
    r = SuggestionResolver(pubmed=pubmed, europepmc=StubEPMC(by_pmcid={"PMC6": cand(pmcid="PMC6", title="epmc only")}))
    res = r.resolve(sug(SuggestionKind.PMCID, "PMC5"))
    assert res.source == "pubmed" and res.candidates[0].pmcid == "PMC5"

    res = r.resolve(sug(SuggestionKind.PMCID, "PMC6"))
    assert res.source == "europepmc" and res.candidates[0].title == "epmc only"

    assert not r.resolve(sug(SuggestionKind.PMCID, "PMC7")).resolved


def test_doi_verifies_pubmed_match_and_falls_back_to_preprints():
    wrong = cand(pmid="20", doi="10.1000/other", title="erratum")
    right = cand(pmid="21", doi="10.1000/ABC", title="right one")
    pubmed = StubPubMed(articles={"20": wrong, "21": right}, doi_map={"10.1000/abc": ["20", "21"]})
    r = SuggestionResolver(pubmed=pubmed)
    res = r.resolve(sug(SuggestionKind.DOI, "10.1000/abc"))
    assert res.source == "pubmed" and res.candidates[0].title == "right one"

    # Not in PubMed: bioRxiv, with a version suffix stripped for the lookup
    pre = cand(doi="10.1101/2024.01.03.574066", title="preprint", source="biorxiv")
    bio = StubBioRxiv(by_doi={"10.1101/2024.01.03.574066": pre})
    r = SuggestionResolver(pubmed=StubPubMed(), biorxiv=bio)
    res = r.resolve(sug(SuggestionKind.DOI, "10.1101/2024.01.03.574066v2"))
    assert res.source == "biorxiv"
    assert bio.seen == ["10.1101/2024.01.03.574066"]

    assert not SuggestionResolver(pubmed=StubPubMed()).resolve(sug(SuggestionKind.DOI, "10.9/none")).resolved


def test_author_year_collects_library_and_pubmed_and_reports_total():
    lib = cand(pmid="30", title="from lib", authors=["Battison"], year=2024, source="user_library")
    a = cand(pmid="31", title="pubmed a", authors=["Battison"], year=2024)
    b = cand(pmid="30", title="dup of lib", authors=["Battison"], year=2024)
    pubmed = StubPubMed(articles={"30": b, "31": a}, author_hits=["31", "30"], author_total=12)
    r = SuggestionResolver(pubmed=pubmed, user_library=StubLibrary(author=[lib]))
    res = r.resolve(sug(SuggestionKind.AUTHOR_YEAR, "Battison 2024", author="Battison", year=2024, et_al=True))
    assert [c.title for c in res.candidates] == ["from lib", "pubmed a"]  # deduplicated on PMID
    assert res.total_matches == 12 and res.ambiguous and res.source == "user_library"

    # Nothing anywhere
    r = SuggestionResolver(pubmed=StubPubMed(), europepmc=StubEPMC())
    res = r.resolve(sug(SuggestionKind.AUTHOR_YEAR, "Nobody 2024", author="Nobody", year=2024))
    assert not res.resolved and "Nobody" in res.error

    # Europe PMC only consulted when PubMed finds nothing
    ep = cand(pmid="40", title="epmc", authors=["Rare"], year=2021, source="europepmc")
    r = SuggestionResolver(pubmed=StubPubMed(), europepmc=StubEPMC(by_author=[ep]))
    res = r.resolve(sug(SuggestionKind.AUTHOR_YEAR, "Rare 2021", author="Rare", year=2021))
    assert res.source == "europepmc" and not res.ambiguous


def test_exceptions_become_errors_not_crashes():
    class Boom(StubPubMed):
        def fetch_article(self, pmid):
            raise RuntimeError("network down")

    r = SuggestionResolver(pubmed=Boom())
    res = r.resolve(sug(SuggestionKind.PMID, "1"))
    assert not res.resolved and "network down" in res.error
