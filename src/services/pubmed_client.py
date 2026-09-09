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
# NCBI PMC ID Converter: maps PMCID / DOI / manuscript IDs to PMIDs.
# https://www.ncbi.nlm.nih.gov/pmc/tools/id-converter-api/
IDCONV_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"


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

    # ── Identifier lookups (used for author-suggested citations) ────

    def find_pmids_by_doi(self, doi: str, max_results: int = 3) -> list[str]:
        """Return candidate PMIDs for a DOI via ESearch (``[doi]``, then ``[aid]``).

        More than one PMID can come back (errata, comments); callers should
        fetch the records and keep the one whose DOI matches exactly.
        """
        doi = (doi or "").strip()
        if not doi:
            return []
        for field in ("doi", "aid"):
            pmids, _ = self.search(f"{doi}[{field}]", max_results=max_results)
            if pmids:
                return pmids
        return []

    def find_pmid_by_doi(self, doi: str) -> str:
        """Return the first PMID for a DOI, or '' when PubMed has no record."""
        pmids = self.find_pmids_by_doi(doi, max_results=1)
        return pmids[0] if pmids else ""

    def pmcid_to_pmid(self, pmcid: str) -> str:
        """Convert a PMCID (e.g. 'PMC11413553') to a PMID.

        Tries the NCBI ID Converter first, then ELink (pmc -> pubmed).
        Returns '' when no PubMed record is linked.
        """
        pmcid = (pmcid or "").strip().upper()
        if not pmcid:
            return ""
        if not pmcid.startswith("PMC"):
            pmcid = f"PMC{pmcid}"
        digits = pmcid[3:]
        if not digits.isdigit():
            return ""

        cache_key = f"pubmed:pmcid2pmid:{pmcid}"
        cached = self.cache.get_search(cache_key)
        if cached is not None:
            return cached or ""

        pmid = ""
        # 1) ID converter
        self._rate_limit()
        try:
            params = {
                **self._base_params(),
                "ids": pmcid,
                "format": "json",
                "tool": "airefs",
            }
            resp = self._session.get(IDCONV_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for rec in data.get("records", []):
                if str(rec.get("pmcid", "")).upper() == pmcid and rec.get("pmid"):
                    pmid = str(rec["pmid"])
                    break
        except Exception as e:
            logger.warning(f"PMC ID converter failed for {pmcid}: {e}")

        # 2) ELink fallback
        if not pmid:
            self._rate_limit()
            try:
                # Only the article's own PubMed record ("pmc_pubmed"), never the
                # papers it cites ("pmc_refs_pubmed"), which ELink also returns.
                params = {
                    **self._base_params(),
                    "dbfrom": "pmc",
                    "db": "pubmed",
                    "id": digits,
                    "linkname": "pmc_pubmed",
                    "retmode": "json",
                }
                resp = self._session.get(f"{EUTILS_BASE}/elink.fcgi", params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                for linkset in data.get("linksets", []):
                    for linksetdb in linkset.get("linksetdbs", []):
                        if linksetdb.get("linkname", "pmc_pubmed") != "pmc_pubmed":
                            continue
                        links = linksetdb.get("links", [])
                        if links:
                            pmid = str(links[0])
                            break
                    if pmid:
                        break
            except Exception as e:
                logger.warning(f"ELink pmc->pubmed failed for {pmcid}: {e}")

        if pmid:
            self.cache.put_search(cache_key, pmid)
        return pmid

    def search_author_year(
        self, last_name: str, year: int, coauthor: str = "", max_results: int = 10,
    ) -> tuple[list[str], int]:
        """Find papers whose first author is *last_name* published in *year*.

        Returns ``(pmids, total_count)``.  Uses the first-author field first and
        falls back to any-author when that yields nothing.
        """
        last_name = (last_name or "").strip()
        if not last_name or not year:
            return [], 0
        extra = f" AND {coauthor}[au]" if coauthor else ""
        # [dp] is the print publication date; an e-pub in December and a print
        # date the next year are common, so widen to +-1 year when the exact
        # year finds nothing.
        date_terms = (f"{year}[dp]", f"{year - 1}:{year + 1}[dp]")
        for field in ("1au", "au"):
            for date_term in date_terms:
                query = f"{last_name}[{field}] AND {date_term}{extra}"
                pmids, total = self.search(query, max_results=max_results)
                if pmids:
                    return pmids, total
        return [], 0

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
        # Also check PubmedData ArticleIdList (DOI fallback and PMCID)
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
            pmcid=pmcid,
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
        )

    def _get_text(self, elem) -> str:
        """Get all text content from an element, including mixed content
        (e.g. <ArticleTitle>Role of <i>Rac1</i> in...</ArticleTitle>)."""
        if elem is None:
            return ""
        return "".join(elem.itertext()).strip()
