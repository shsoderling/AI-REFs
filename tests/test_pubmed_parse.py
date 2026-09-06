"""PubMed EFetch XML parsing picks up the PMC id and DOI fallback."""

from src.services.pubmed_client import PubMedClient
from src.storage.cache_db import CacheDB

EFETCH = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>11</PMID>
      <Article>
        <Journal><Title>Journal of Tests</Title><ISOAbbreviation>J Test</ISOAbbreviation>
          <JournalIssue><Volume>1</Volume><Issue>2</Issue><PubDate><Year>2020</Year></PubDate></JournalIssue></Journal>
        <ArticleTitle>Rac1 in <i>spines</i></ArticleTitle>
        <Abstract><AbstractText Label="RESULTS">Spines grew.</AbstractText></Abstract>
        <AuthorList><Author><LastName>Smith</LastName><ForeName>Jane</ForeName><Initials>J</Initials></Author></AuthorList>
        <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">11</ArticleId>
        <ArticleId IdType="doi">10.1/rac1</ArticleId>
        <ArticleId IdType="pmc">PMC7000000</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


def test_parse_efetch_records_pmcid_doi_and_labelled_abstract():
    client = PubMedClient(email="x@y.z", cache_db=CacheDB(":memory:"))
    [article] = client._parse_efetch_xml(EFETCH)
    assert article.pmid == "11"
    assert article.pmcid == "PMC7000000"
    assert article.doi == "10.1/rac1"
    assert article.title == "Rac1 in spines"
    assert article.abstract == "RESULTS: Spines grew."
    assert article.journal_abbrev == "J Test" and article.year == 2020
