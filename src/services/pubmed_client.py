"""PubMed E-utilities client with caching.

Uses NCBI's free, public E-utilities API to search PubMed and fetch
article metadata. Requires an email address (NCBI policy). An API key
is optional but increases the rate limit from 3 to 10 requests/sec.

API docs: https://www.ncbi.nlm.nih.gov/books/NBK25497/
"""

import time
import logging
import xml.etree.ElementTree as ET
from typing import Optional

import requests

from ..models.citation import CitationCandidate, Author
from ..storage.cache_db import CacheDB

logger = logging.getLogger(__name__)

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


class PubMedClient:
    """Client for NCBI PubMed E-utilities with local caching."""

    def __init__(self, email: str, api_key: str = "", cache_db: Optional[CacheDB] = None):
        self.email = email
        self.api_key = api_key
        self.cache = cache_db or CacheDB()
        self._session = requests.Session()
        self._last_request_time = 0.0
        # NCBI allows 3 req/s without key, 10 req/s with key
        self._min_interval = 0.11 if api_key else 0.35

    def _rate_limit(self):
        """Enforce NCBI rate limiting."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()

    def _base_params(self) -> dict:
        """Common parameters for all E-utility requests."""
        params = {"email": self.email}
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    # ── ESearch: keyword search ─────────────────────────────────────

    def search(self, query: str, max_results: int = 20) -> tuple[list[str], int]:
        """Search PubMed and return (list_of_PMIDs, total_result_count)."""
        if not query:
            return [], 0

        # Check cache
        cache_key = f"pubmed:search:{query}:{max_results}"
        cached = self.cache.get_search(cache_key)
        if cached is not None:
            logger.debug(f"PubMed search cache hit: {query}")
            return cached

        self._rate_limit()
        params = {
            **self._base_params(),
            "db": "pubmed",
            "term": query,
            "retmax": max_results,
            "retmode": "json",
            "sort": "relevance",
        }

        try:
            resp = self._session.get(f"{EUTILS_BASE}/esearch.fcgi", params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            result = data.get("esearchresult", {})
            pmids = result.get("idlist", [])
            total = int(result.get("count", 0))

            logger.info(f"PubMed search '{query}': {len(pmids)} PMIDs (total {total})")

            # Cache the result
            self.cache.put_search(cache_key, (pmids, total))
            return pmids, total

        except Exception as e:
            logger.error(f"PubMed search error: {e}")
            return [], 0

    # ── ECitMatch: resolve a bibliographic citation to a PMID ────────

    def citation_match(self, journal: str, year: int, volume: str,
                       first_page: str, author_last: str) -> Optional[str]:
        """Match a citation to a PMID via NCBI's citation matcher.

        All fields are required by ecitmatch; returns the PMID string or
        None if not found / ambiguous.
        """
        if not (journal and year and volume and first_page and author_last):
            return None

        bdata = f"{journal}|{year}|{volume}|{first_page}|{author_last}|key|"
        cache_key = f"pubmed:ecitmatch:{bdata}"
        cached = self.cache.get_search(cache_key)
        if cached is not None:
            return cached or None  # "" caches a miss

        self._rate_limit()
        params = {
            **self._base_params(),
            "db": "pubmed",
            "retmode": "xml",
            "bdata": bdata,
        }
        try:
            resp = self._session.get(f"{EUTILS_BASE}/ecitmatch.cgi",
                                     params=params, timeout=30)
            resp.raise_for_status()
            pmid = ""
            # Response is one line per citation: the PMID (or NOT_FOUND /
            # AMBIGUOUS) is the field after the supplied key.
            for line in resp.text.strip().splitlines():
                parts = line.strip().split("|")
                if len(parts) >= 7 and parts[6].strip().isdigit():
                    pmid = parts[6].strip()
                    break
            self.cache.put_search(cache_key, pmid)
            logger.info(f"ECitMatch '{journal} {year};{volume}:{first_page}' "
                        f"-> {pmid or 'not found'}")
            return pmid or None
        except Exception as e:
            logger.warning(f"ECitMatch error: {e}")
            return None

    # ── EFetch: get full article metadata ───────────────────────────

    def fetch_article(self, pmid: str) -> Optional[CitationCandidate]:
        """Fetch a single article by PMID. Returns CitationCandidate or None."""
        articles = self.fetch_articles([pmid])
        return articles[0] if articles else None

    def fetch_articles(self, pmids: list[str]) -> list[CitationCandidate]:
        """Fetch multiple articles by PMID list (batched)."""
        if not pmids:
            return []

        # Check cache for each PMID
        results = []
        uncached_pmids = []
        for pmid in pmids:
            cached = self.cache.get_article(f"pubmed:{pmid}")
            if cached:
                results.append(CitationCandidate(**cached))
            else:
                uncached_pmids.append(pmid)

        if not uncached_pmids:
            return results

        # Fetch in batches of 50 (NCBI recommendation)
        for i in range(0, len(uncached_pmids), 50):
            batch = uncached_pmids[i:i + 50]
            fetched = self._efetch_batch(batch)
            for article in fetched:
                # Cache each article
                self.cache.put_article(f"pubmed:{article.pmid}", article.model_dump())
                results.append(article)

        # Return in original order
        pmid_order = {p: i for i, p in enumerate(pmids)}
        results.sort(key=lambda a: pmid_order.get(a.pmid, 999))
        return results

    def _efetch_batch(self, pmids: list[str]) -> list[CitationCandidate]:
        """Fetch a batch of articles via EFetch XML."""
        self._rate_limit()
        params = {
            **self._base_params(),
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "xml",
        }

        try:
            resp = self._session.get(f"{EUTILS_BASE}/efetch.fcgi", params=params, timeout=60)
            resp.raise_for_status()
            return self._parse_efetch_xml(resp.text)
        except Exception as e:
            logger.error(f"PubMed EFetch error: {e}")
            return []

    def _parse_efetch_xml(self, xml_text: str) -> list[CitationCandidate]:
        """Parse EFetch PubmedArticle XML into CitationCandidate objects."""
        articles = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.error(f"XML parse error: {e}")
            return []

        for article_elem in root.findall(".//PubmedArticle"):
            try:
                candidate = self._parse_single_article(article_elem)
                if candidate:
                    articles.append(candidate)
            except Exception as e:
                logger.warning(f"Error parsing article: {e}")

        logger.info(f"Parsed {len(articles)} articles from EFetch XML")
        return articles

    def _parse_single_article(self, elem) -> Optional[CitationCandidate]:
        """Parse a single PubmedArticle XML element."""
        medline = elem.find(".//MedlineCitation")
        if medline is None:
            return None

        # PMID
        pmid_elem = medline.find("PMID")
        pmid = pmid_elem.text if pmid_elem is not None else ""

        article = medline.find("Article")
        if article is None:
            return None

        # Title
        title_elem = article.find("ArticleTitle")
        title = self._get_text(title_elem)

        # Abstract
        abstract_parts = []
        abstract_elem = article.find("Abstract")
        if abstract_elem is not None:
            for abs_text in abstract_elem.findall("AbstractText"):
                label = abs_text.get("Label", "")
                text = self._get_text(abs_text)
                if label:
                    abstract_parts.append(f"{label}: {text}")
                else:
                    abstract_parts.append(text)
        abstract = " ".join(abstract_parts)

        # Authors
        authors = []
        author_list = article.find("AuthorList")
        if author_list is not None:
            for auth_elem in author_list.findall("Author"):
                last = auth_elem.findtext("LastName", "")
                first = auth_elem.findtext("ForeName", "")
                initials = auth_elem.findtext("Initials", "")
                if last:
                    authors.append(Author(
                        last_name=last,
                        first_name=first,
                        initials=initials,
                    ))

        # Journal
        journal_elem = article.find("Journal")
        journal = ""
        journal_abbrev = ""
        volume = ""
        issue = ""
        year = 0

        if journal_elem is not None:
            journal = journal_elem.findtext("Title", "")
            journal_abbrev = journal_elem.findtext("ISOAbbreviation", "")
            ji = journal_elem.find("JournalIssue")
            if ji is not None:
                volume = ji.findtext("Volume", "")
                issue = ji.findtext("Issue", "")
                # Year from various locations
                pub_date = ji.find("PubDate")
                if pub_date is not None:
                    y = pub_date.findtext("Year", "")
                    if y:
                        try:
                            year = int(y)
                        except ValueError:
                            pass

        # If year not found in journal, try ArticleDate
        if year == 0:
            for ad in article.findall("ArticleDate"):
                y = ad.findtext("Year", "")
                if y:
                    try:
                        year = int(y)
                        break
                    except ValueError:
                        pass

        # Pages
        pages = article.findtext(".//Pagination/MedlinePgn", "")

        # DOI
        doi = ""
        for id_elem in article.findall(".//ELocationID"):
            if id_elem.get("EIdType") == "doi":
                doi = id_elem.text or ""
                break
        # PubmedData ArticleIdList: DOI fallback and the PMC id
        pmcid = ""
        pubmed_data = elem.find("PubmedData")
        if pubmed_data is not None:
            for aid in pubmed_data.findall(".//ArticleId"):
                id_type = aid.get("IdType")
                if id_type == "doi" and not doi:
                    doi = aid.text or ""
                elif id_type == "pmc" and not pmcid:
                    pmcid = (aid.text or "").strip()

        # MeSH terms
        mesh_terms = []
        mesh_list = medline.find("MeshHeadingList")
        if mesh_list is not None:
            for mh in mesh_list.findall("MeshHeading"):
                desc = mh.findtext("DescriptorName", "")
                if desc:
                    mesh_terms.append(desc)

        # Publication types
        pub_types = []
        type_list = article.find("PublicationTypeList")
        if type_list is not None:
            for pt in type_list.findall("PublicationType"):
                if pt.text:
                    pub_types.append(pt.text)

        # Flags
        is_review = any("review" in t.lower() for t in pub_types)
        is_retracted = any("retract" in t.lower() for t in pub_types)

        return CitationCandidate(
            pmid=pmid,
            doi=doi,
            title=title,
            source="pubmed",
            authors=authors,
            year=year,
            journal=journal,
            journal_abbrev=journal_abbrev,
            volume=volume,
            issue=issue,
            pages=pages,
            abstract=abstract,
            mesh_terms=mesh_terms,
            publication_types=pub_types,
            is_review=is_review,
            is_retracted=is_retracted,
            pmcid=pmcid,
        )

    def _get_text(self, elem) -> str:
        """Get all text content from an element, including mixed content
        (e.g. <ArticleTitle>Role of <i>Rac1</i> in...</ArticleTitle>)."""
        if elem is None:
            return ""
        return "".join(elem.itertext()).strip()
