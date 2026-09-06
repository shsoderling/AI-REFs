"""bioRxiv/medRxiv API client with caching.

Uses the bioRxiv REST API to search preprints by date range and category,
retrieve full metadata by DOI, and find preprints that have been published
in peer-reviewed journals.

API docs: https://api.biorxiv.org
"""

import time
import logging
import re
from typing import Optional
from datetime import datetime, timedelta

import requests

from ..models.citation import CitationCandidate, Author
from ..storage.cache_db import CacheDB

logger = logging.getLogger(__name__)

BIORXIV_API_BASE = "https://api.biorxiv.org"

# Map inferred domains to bioRxiv categories
DOMAIN_TO_BIORXIV_CATEGORY = {
    "neuroscience": "neuroscience",
    "cell biology": "cell_biology",
    "molecular biology": "molecular_biology",
    "developmental biology": "developmental_biology",
    "immunology": "immunology",
    "biochemistry": "biochemistry",
    "genetics": "genetics",
    "genomics": "genomics",
    "bioinformatics": "bioinformatics",
    "cancer biology": "cancer_biology",
    "microbiology": "microbiology",
    "physiology": "physiology",
    "biophysics": "biophysics",
    "systems biology": "systems_biology",
}


class BioRxivClient:
    """Client for bioRxiv/medRxiv REST API with caching."""

    def __init__(self, cache_db: Optional[CacheDB] = None, server: str = "biorxiv"):
        """
        Args:
            cache_db: Optional SQLite cache (shared with PubMed client).
            server: 'biorxiv' or 'medrxiv'.
        """
        self.cache = cache_db or CacheDB()
        self.server = server
        self._session = requests.Session()
        self._last_request_time = 0.0
        self._min_interval = 0.5  # bioRxiv rate limit is more conservative
        self._max_retries = 3

    def _rate_limit(self):
        """Enforce rate limiting between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()

    def _request(self, url: str) -> Optional[dict]:
        """Make a GET request with rate limiting and retry. Returns parsed JSON."""
        for attempt in range(self._max_retries):
            self._rate_limit()
            try:
                resp = self._session.get(url, timeout=30)
                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 429:
                    wait = 2 ** attempt
                    logger.warning(f"bioRxiv rate limited (429), waiting {wait}s")
                    time.sleep(wait)
                elif resp.status_code >= 500:
                    wait = 2 ** attempt
                    logger.warning(f"bioRxiv server error {resp.status_code}, retry {attempt+1}")
                    time.sleep(wait)
                else:
                    logger.error(f"bioRxiv HTTP {resp.status_code} for {url}")
                    return None
            except requests.exceptions.RequestException as e:
                wait = 2 ** attempt
                logger.warning(f"bioRxiv request error: {e}, retry {attempt+1}")
                time.sleep(wait)

        logger.error(f"bioRxiv failed after {self._max_retries} retries: {url}")
        return None

    # ── Search by date range + category ─────────────────────────────

    def search_recent(self, category: str = "", days: int = 90,
                      max_results: int = 30) -> list[dict]:
        """Search recent preprints by category.

        The bioRxiv API doesn't support keyword search, only date range + category.
        We fetch recent preprints and do local keyword matching.

        Returns raw result dicts with keys: doi, title, authors, date,
        category, abstract_preview.
        """
        date_to = datetime.now().strftime("%Y-%m-%d")
        date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        # Check cache
        cache_key = f"biorxiv:{self.server}:{category}:{date_from}:{date_to}"
        cached = self.cache.get_search(cache_key)
        if cached is not None:
            # cached is (list_of_dois, total_count)
            logger.debug(f"bioRxiv cache hit for {cache_key}")
            return cached  # We store the full result list in cache

        url = (f"{BIORXIV_API_BASE}/details/{self.server}/"
               f"{date_from}/{date_to}/0/json")

        data = self._request(url)
        if not data or "collection" not in data:
            return []

        results = data["collection"]

        # Filter by category if specified
        if category:
            cat_lower = category.lower().replace("_", " ")
            results = [r for r in results
                       if r.get("category", "").lower() == cat_lower]

        results = results[:max_results]
        logger.info(f"bioRxiv search: {len(results)} preprints "
                    f"({category or 'all'}, last {days} days)")

        return results

    def search_by_keywords(self, keywords: list[str],
                           categories: list[str] = None,
                           days: int = 365 * 3,
                           max_results: int = 15) -> list[CitationCandidate]:
        """Search bioRxiv by keyword matching against recent preprints.

        Since bioRxiv API doesn't support keyword search, we:
        1. Fetch recent preprints in relevant categories
        2. Score them by keyword overlap with title + abstract
        3. Return the best matches as CitationCandidates

        Args:
            keywords: Search terms extracted from the claim sentence.
            categories: bioRxiv categories to search (e.g., ['neuroscience']).
            days: How far back to search (default 3 years).
            max_results: Maximum candidates to return.

        Returns:
            List of CitationCandidate objects.
        """
        if not keywords:
            return []

        # Determine which categories to search
        search_cats = []
        if categories:
            for cat in categories:
                mapped = DOMAIN_TO_BIORXIV_CATEGORY.get(cat.lower(), "")
                if mapped:
                    search_cats.append(mapped)

        # If no categories mapped, search without category filter
        if not search_cats:
            search_cats = [""]  # Empty string = all categories

        # Collect preprints from all relevant categories
        all_preprints = []
        seen_dois = set()
        for cat in search_cats[:3]:  # Limit to 3 categories max
            preprints = self.search_recent(
                category=cat, days=days, max_results=100
            )
            for p in preprints:
                doi = p.get("doi", "")
                if doi and doi not in seen_dois:
                    seen_dois.add(doi)
                    all_preprints.append(p)

        if not all_preprints:
            logger.info("bioRxiv: no preprints found in date range")
            return []

        # Score each preprint by keyword overlap
        scored = []
        kw_lower = [k.lower() for k in keywords]

        for p in all_preprints:
            title = p.get("title", "").lower()
            abstract = p.get("abstract", p.get("abstract_preview", "")).lower()
            text = f"{title} {abstract}"

            # Count keyword matches
            matches = 0
            matched_kws = []
            for kw in kw_lower:
                if kw in text:
                    matches += 1
                    matched_kws.append(kw)

            if matches >= 2:  # Require at least 2 keyword matches
                score = matches / len(kw_lower) * 100
                scored.append((score, matched_kws, p))

        # Sort by score descending
        scored.sort(key=lambda x: x[0], reverse=True)

        # Convert top results to CitationCandidates
        candidates = []
        for score, matched_kws, preprint in scored[:max_results]:
            candidate = self._preprint_to_candidate(preprint)
            if candidate:
                candidate.matching_keywords = matched_kws
                candidates.append(candidate)

        logger.info(f"bioRxiv keyword search: {len(candidates)} matches "
                    f"from {len(all_preprints)} preprints")
        return candidates

    # ── Fetch single preprint by DOI ────────────────────────────────

    def fetch_preprint(self, doi: str) -> Optional[CitationCandidate]:
        """Fetch full metadata for a preprint by DOI.

        Args:
            doi: bioRxiv DOI (e.g., '10.1101/2024.01.15.123456').
        """
        # Normalize DOI — strip URL prefixes
        doi = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', doi.strip())

        # Check article cache
        cached = self.cache.get_article(f"biorxiv:{doi}")
        if cached:
            return CitationCandidate(**cached)

        url = f"{BIORXIV_API_BASE}/details/{self.server}/{doi}"
        data = self._request(url)
        if not data or "collection" not in data or not data["collection"]:
            return None

        # Get the latest version
        versions = data["collection"]
        latest = versions[-1] if versions else versions[0]

        candidate = self._preprint_to_candidate(latest)
        if candidate:
            self.cache.put_article(f"biorxiv:{doi}", candidate.model_dump())

        return candidate

    # ── Convert bioRxiv record → CitationCandidate ──────────────────

    def _preprint_to_candidate(self, preprint: dict) -> Optional[CitationCandidate]:
        """Convert a bioRxiv API result dict to a CitationCandidate."""
        doi = preprint.get("doi", "")
        if not doi:
            return None

        title = preprint.get("title", "")
        abstract = preprint.get("abstract", preprint.get("abstract_preview", ""))

        # Parse authors: "Last1, F1; Last2, F2; ..."
        authors = self._parse_author_string(preprint.get("authors", ""))

        # Parse year from date string (YYYY-MM-DD)
        date_str = preprint.get("date", "")
        year = 0
        if date_str and len(date_str) >= 4:
            try:
                year = int(date_str[:4])
            except ValueError:
                pass

        # Journal = bioRxiv or medRxiv
        server = preprint.get("server", self.server)
        journal = "bioRxiv" if "biorxiv" in server.lower() else "medRxiv"

        # DOI of the journal version, when the preprint has been published
        published_doi = preprint.get("published_doi", "") or ""
        if published_doi == "NA":
            published_doi = ""

        return CitationCandidate(
            pmid="",  # Preprints don't have PMIDs
            doi=doi,
            title=title,
            source="biorxiv",
            authors=authors,
            year=year,
            journal=journal,
            journal_abbrev=journal,
            volume=preprint.get("version", ""),
            issue="",
            pages="",
            abstract=abstract,
            mesh_terms=[],
            publication_types=["Preprint"],
            is_retracted=False,
            is_review=False,
            published_doi=published_doi,
        )

    def _parse_author_string(self, author_str: str) -> list[Author]:
        """Parse bioRxiv author string 'Last, F.; Last2, F2.' into Author list."""
        if not author_str:
            return []

        authors = []
        # Split on semicolons
        parts = [p.strip().rstrip(".") for p in author_str.split(";")]
        for part in parts:
            if not part:
                continue
            # "LastName, FirstName M." or "LastName, F. M."
            pieces = [p.strip() for p in part.split(",", 1)]
            last_name = pieces[0] if pieces else ""
            first_name = pieces[1] if len(pieces) > 1 else ""
            initials = ""
            if first_name:
                # Extract initials from first name
                initials = "".join(
                    w[0] for w in first_name.split() if w
                ).upper()

            if last_name:
                authors.append(Author(
                    last_name=last_name,
                    first_name=first_name,
                    initials=initials,
                ))

        return authors
