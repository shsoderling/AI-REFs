"""Enrich existing bibliography entries with PMIDs/DOIs via PubMed.

Old documents (especially from other tools) often cite papers without
printing a DOI or PMID.  Without an identifier, an existing entry cannot be
deduplicated against newly found candidates — the same paper would get two
bibliography numbers.  This pass recovers identifiers by matching each bare
entry against PubMed:

1. NCBI citation matcher (ecitmatch) using heuristically parsed
   journal/year/volume/page/author fields
2. Fallback: title phrase search, year-filtered

A match is only accepted after verification (title similarity + year), and
results — including misses — are cached by entry-text hash.
"""

import hashlib
import logging
import re
from difflib import SequenceMatcher
from typing import Callable, Optional

from ..models.citation import CitationCandidate
from ..models.existing_refs import ExistingBibEntry, ExistingCitationMap
from ..services.pubmed_client import PubMedClient
from ..storage.cache_db import CacheDB

logger = logging.getLogger(__name__)

TITLE_MATCH_THRESHOLD = 0.85


def parse_entry_fields(body: str) -> dict:
    """Best-effort split of an NLM-style bibliography entry into fields.

    Expected shape: "Smith J, Doe A. Title of the paper. J Neurosci.
    2020;12(3):100-110. ..." — but tolerant of variations.
    """
    fields = {
        "authors": "",
        "first_author_last": "",
        "title": "",
        "journal": "",
        "year": 0,
        "volume": "",
        "first_page": "",
    }

    year_m = re.search(r'\b((?:19|20)\d{2})\b', body)
    if year_m:
        fields["year"] = int(year_m.group(1))

    # "...;12(3):100-110" or "...;12:100"
    vol_m = re.search(r';\s*(\d+[A-Za-z]?)\s*(?:\([^)]*\))?\s*:\s*(\d+)', body)
    if vol_m:
        fields["volume"] = vol_m.group(1)
        fields["first_page"] = vol_m.group(2)

    segments = [s.strip() for s in re.split(r'(?<=[.?!])\s+', body) if s.strip()]
    if segments:
        fields["authors"] = segments[0].rstrip('.')
        author_m = re.match(r"([A-Z][\w'\-]+)", segments[0])
        if author_m:
            fields["first_author_last"] = author_m.group(1)
    if len(segments) >= 2:
        fields["title"] = segments[1].rstrip('.')
    if len(segments) >= 3:
        fields["journal"] = segments[2].rstrip('.')

    return fields


def _title_similarity(a: str, b: str) -> float:
    norm = lambda s: re.sub(r'[^a-z0-9 ]+', '', s.lower()).strip()
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


class ExistingRefEnricher:
    """Recover PMIDs/DOIs for bibliography entries that lack them."""

    def __init__(self, pubmed: PubMedClient, cache: Optional[CacheDB] = None,
                 log_callback: Optional[Callable[[str, str], None]] = None):
        self.pubmed = pubmed
        self.cache = cache or pubmed.cache
        self._log = log_callback or (lambda *a: None)

    def enrich(self, existing: ExistingCitationMap,
               should_cancel: Optional[Callable[[], bool]] = None) -> int:
        """Enrich all entries lacking identifiers. Returns count enriched."""
        enriched = 0
        targets = [
            e for e in existing.bib_entries.values()
            if not e.pmid and not e.doi
        ]
        for i, entry in enumerate(targets):
            if should_cancel and should_cancel():
                break
            self._log("info",
                      f"    Matching entry {entry.original_number} "
                      f"({i + 1}/{len(targets)})...")
            if self._enrich_entry(entry):
                enriched += 1
        return enriched

    # ── Internals ────────────────────────────────────────────────────

    @staticmethod
    def _cache_key(entry: ExistingBibEntry) -> str:
        digest = hashlib.sha256(entry.raw_text.encode("utf-8")).hexdigest()
        return f"enrich:{digest}"

    def _enrich_entry(self, entry: ExistingBibEntry) -> bool:
        cache_key = self._cache_key(entry)
        cached = self.cache.get_article(cache_key)
        if cached is not None:
            if cached.get("miss"):
                return False
            self._apply(entry, CitationCandidate(**cached))
            return True

        candidate = self._match(entry)
        if candidate:
            self.cache.put_article(cache_key, candidate.model_dump())
            self._apply(entry, candidate)
            return True

        self.cache.put_article(cache_key, {"miss": True})
        return False

    def _match(self, entry: ExistingBibEntry) -> Optional[CitationCandidate]:
        fields = parse_entry_fields(entry.body or entry.raw_text)

        # Strategy 1: NCBI citation matcher
        pmid = self.pubmed.citation_match(
            journal=fields["journal"],
            year=fields["year"],
            volume=fields["volume"],
            first_page=fields["first_page"],
            author_last=fields["first_author_last"],
        )
        if pmid:
            article = self.pubmed.fetch_article(pmid)
            if article and self._verify(fields, article):
                return article

        # Strategy 2: title phrase search
        title = fields["title"]
        if title and len(title) > 20:
            query = f'"{title}"[Title]'
            if fields["year"]:
                query += (f' AND ("{fields["year"] - 1}"[PDAT] : '
                          f'"{fields["year"] + 1}"[PDAT])')
            pmids, _ = self.pubmed.search(query, max_results=3)
            for article in self.pubmed.fetch_articles(pmids):
                if self._verify(fields, article):
                    return article

        return None

    @staticmethod
    def _verify(fields: dict, article: CitationCandidate) -> bool:
        """Only accept a match that agrees on title and year."""
        title = fields["title"]
        if not title or not article.title:
            return False
        if _title_similarity(title, article.title) < TITLE_MATCH_THRESHOLD:
            return False
        if fields["year"] and article.year and abs(fields["year"] - article.year) > 1:
            return False
        return True

    @staticmethod
    def _apply(entry: ExistingBibEntry, article: CitationCandidate):
        entry.pmid = article.pmid or ""
        entry.doi = article.doi or ""
        if article.title:
            entry.title = article.title
        if article.year:
            entry.year = article.year
        if article.journal:
            entry.journal = article.journal
        entry.matched_candidate = article
        logger.info(
            f"Enriched entry {entry.original_number}: "
            f"PMID={entry.pmid or '-'} DOI={entry.doi or '-'}"
        )
