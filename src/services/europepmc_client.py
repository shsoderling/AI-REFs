"""Europe PMC API client with caching.

Uses the Europe PMC REST API to search articles and preprints by query.
Full-text keyword search returns full metadata in one call with resultType=core.
No API key required.

API docs: https://europepmc.org/RestfulWebService
"""

import time
import json
import logging
import re
from typing import Optional

import requests

from ..models.citation import CitationCandidate, Author
from ..storage.cache_db import CacheDB

logger = logging.getLogger(__name__)

EUROPEPMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


class EuropePMCClient:
    """Client for Europe PMC REST API with caching."""

    def __init__(self, cache_db: Optional[CacheDB] = None):
        """
        Args:
            cache_db: Optional SQLite cache (shared with PubMed / bioRxiv clients).
        """
        self.cache = cache_db or CacheDB()
        self._session = requests.Session()
        self._last_request_time = 0.0
        self._min_interval = 0.5  # Conservative rate limiting
        self._max_retries = 3

    def _rate_limit(self):
        """Enforce rate limiting between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()

    def _request(self, params: dict) -> Optional[dict]:
        """Make a GET request with rate limiting and retry. Returns parsed JSON."""
        for attempt in range(self._max_retries):
            self._rate_limit()
            try:
                resp = self._session.get(
                    EUROPEPMC_SEARCH_URL, params=params, timeout=30
                )
                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 429:
                    wait = 2 ** attempt
                    logger.warning(f"Europe PMC rate limited (429), waiting {wait}s")
                    time.sleep(wait)
                elif resp.status_code >= 500:
                    wait = 2 ** attempt
                    logger.warning(
                        f"Europe PMC server error {resp.status_code}, retry {attempt+1}"
                    )
                    time.sleep(wait)
                else:
                    logger.error(f"Europe PMC HTTP {resp.status_code}")
                    return None
            except requests.exceptions.RequestException as e:
                wait = 2 ** attempt
                logger.warning(f"Europe PMC request error: {e}, retry {attempt+1}")
                time.sleep(wait)

        logger.error(f"Europe PMC failed after {self._max_retries} retries")
        return None

    # ── Search ────────────────────────────────────────────────────

    def search(self, query: str, max_results: int = 20) -> list[CitationCandidate]:
        """Search Europe PMC with a free-text query.

        Returns full metadata (title, authors, abstract, DOI, PMID)
        in a single API call using resultType=core.

        Args:
            query: Search query string.
            max_results: Maximum number of results (1-100).
        """
        if not query.strip():
            return []

        # Check cache
        cache_key = f"europepmc:search:{query}:{max_results}"
        cached = self.cache.get_search(cache_key)
        if cached is not None:
            logger.debug(f"Europe PMC cache hit for: {query}")
            # cached may be a list of dicts — convert to CitationCandidates
            if cached and isinstance(cached[0], dict):
                return [CitationCandidate(**c) for c in cached]
            return cached

        params = {
            "query": query,
            "format": "json",
            "resultType": "core",
            "pageSize": min(max_results, 100),
        }

        data = self._request(params)
        if not data or "resultList" not in data:
            return []

        results = data["resultList"].get("result", [])
        candidates = []
        for r in results:
            candidate = self._result_to_candidate(r)
            if candidate:
                candidates.append(candidate)

        logger.info(f"Europe PMC search: {len(candidates)} results for '{query}'")

        # Cache results
        if candidates:
            try:
                self.cache.put_search(
                    cache_key,
                    [c.model_dump() for c in candidates],
                )
            except Exception as e:
                logger.debug(f"Europe PMC cache put failed: {e}")

        return candidates

    # ── Fetch by DOI ──────────────────────────────────────────────

    def fetch_by_doi(self, doi: str) -> Optional[CitationCandidate]:
        """Fetch a single article by DOI from Europe PMC.

        Args:
            doi: Article DOI (e.g. '10.1038/s41586-024-07487-w').
        """
        # Normalize DOI — strip URL prefixes
        doi = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', doi.strip())

        # Check article cache
        cache_key = f"europepmc:doi:{doi}"
        cached = self.cache.get_article(cache_key)
        if cached:
            return CitationCandidate(**cached)

        results = self.search(f'DOI:"{doi}"', max_results=1)
        if results:
            self.cache.put_article(cache_key, results[0].model_dump())
            return results[0]
        return None

    # ── Fetch by PMID ─────────────────────────────────────────────

    def fetch_by_pmid(self, pmid: str) -> Optional[CitationCandidate]:
        """Fetch a single article by PMID from Europe PMC.

        Args:
            pmid: PubMed ID (e.g. '12345678').
        """
        cache_key = f"europepmc:pmid:{pmid}"
        cached = self.cache.get_article(cache_key)
        if cached:
            return CitationCandidate(**cached)

        results = self.search(f"EXT_ID:{pmid} AND SRC:MED", max_results=1)
        if results:
            self.cache.put_article(cache_key, results[0].model_dump())
            return results[0]
        return None

    # ── Convert Europe PMC result → CitationCandidate ─────────────

    def _result_to_candidate(self, result: dict) -> Optional[CitationCandidate]:
        """Convert a Europe PMC API result dict to a CitationCandidate."""
        title = result.get("title", "")
        if not title:
            return None

        # PMID
        pmid = str(result.get("pmid", "")) if result.get("pmid") else ""

        # DOI
        doi = result.get("doi", "") or ""

        # Year
        year = 0
        pub_year = result.get("pubYear")
        if pub_year:
            try:
                year = int(pub_year)
            except (ValueError, TypeError):
                pass

        # Abstract
        abstract = result.get("abstractText", "") or ""

        # Journal
        journal = ""
        journal_abbrev = ""
        journal_info = result.get("journalInfo")
        if journal_info:
            journal_obj = journal_info.get("journal", {})
            journal = journal_obj.get("title", "")
            journal_abbrev = journal_obj.get("medlineAbbreviation", "") or journal_obj.get("isoabbreviation", "")
            if not journal_abbrev:
                journal_abbrev = journal
        if not journal:
            journal = result.get("journalTitle", "") or ""

        # Volume, issue, pages
        volume = ""
        issue = ""
        pages = ""
        if journal_info:
            volume = str(journal_info.get("volume", "") or "")
            issue = str(journal_info.get("issue", "") or "")
        pages = result.get("pageInfo", "") or ""

        # Authors — structured list preferred, fallback to authorString
        authors = self._parse_authors(result)

        # Publication types and preprint detection
        publication_types = []
        source = result.get("source", "")
        if source == "PPR":
            publication_types.append("Preprint")
            # Use the preprint source as journal name
            if not journal:
                book_or_repo = result.get("bookOrReportDetails", {})
                publisher = book_or_repo.get("publisher", "")
                journal = publisher if publisher else "Preprint"
                journal_abbrev = journal

        # Is review
        pub_type_list = result.get("pubTypeList", {}).get("pubType", [])
        is_review = "review" in [pt.lower() for pt in pub_type_list] if pub_type_list else False

        return CitationCandidate(
            pmid=pmid,
            doi=doi,
            title=title.rstrip("."),  # Europe PMC sometimes adds trailing period
            source="europepmc",
            authors=authors,
            year=year,
            journal=journal,
            journal_abbrev=journal_abbrev or journal,
            volume=volume,
            issue=issue,
            pages=pages,
            abstract=abstract,
            mesh_terms=[],  # Europe PMC doesn't return MeSH in the same way
            publication_types=publication_types,
            is_retracted=False,
            is_review=is_review,
        )

    def _parse_authors(self, result: dict) -> list[Author]:
        """Parse authors from Europe PMC result.

        Prefers structured authorList when available, falls back to authorString.
        """
        authors = []

        # Try structured author list first
        author_list = result.get("authorList", {}).get("author", [])
        if author_list and isinstance(author_list, list):
            for a in author_list:
                if not isinstance(a, dict):
                    continue
                last = a.get("lastName", "") or ""
                first = a.get("firstName", "") or ""
                initials = a.get("initials", "") or ""

                # Some entries only have fullName
                if not last and a.get("fullName"):
                    parts = a["fullName"].rsplit(" ", 1)
                    if len(parts) == 2:
                        first, last = parts
                    else:
                        last = parts[0]

                if last:
                    authors.append(Author(
                        last_name=last,
                        first_name=first,
                        initials=initials,
                    ))

        # Fallback to authorString if no structured authors
        if not authors:
            author_string = result.get("authorString", "")
            if author_string:
                authors = self._parse_author_string(author_string)

        return authors

    @staticmethod
    def _parse_author_string(author_str: str) -> list[Author]:
        """Parse comma-separated author string 'Last FM, Last2 FM2, ...'."""
        if not author_str:
            return []

        authors = []
        # Remove trailing period
        author_str = author_str.rstrip(".")

        # Split on comma but be careful with "Last FM" pattern
        parts = [p.strip() for p in author_str.split(",") if p.strip()]
        for part in parts:
            # Pattern: "LastName Initials" e.g. "Smith JA"
            tokens = part.rsplit(" ", 1)
            if len(tokens) == 2:
                last_name = tokens[0]
                initials = tokens[1].rstrip(".")
            else:
                last_name = tokens[0]
                initials = ""

            if last_name:
                authors.append(Author(
                    last_name=last_name,
                    first_name="",
                    initials=initials,
                ))

        return authors
