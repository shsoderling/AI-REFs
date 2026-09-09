"""Resolve author-suggested citations to concrete article records.

Given a :class:`SuggestedCitation` (PMID, PMCID, DOI, or author-year) the
resolver looks the reference up, in this order:

1. the user's local reference library (when configured),
2. PubMed (with the local article cache),
3. Europe PMC,
4. bioRxiv / medRxiv (DOIs only).

Identifier suggestions resolve to at most one candidate.  Author-year
suggestions can resolve to several candidates (the same first author may
have published several papers that year); the LLM agent then picks the one
that matches the claim.  ``total_matches`` records how many papers PubMed
reported so the agent and the reviewer know when the pick was ambiguous.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..models.citation import CitationCandidate
from ..models.markers import SuggestedCitation, SuggestionKind
from .pubmed_client import PubMedClient
from .europepmc_client import EuropePMCClient
from .biorxiv_client import BioRxivClient
from .ref_library import ReferenceLibrary
from .tool_executor import candidate_key

logger = logging.getLogger(__name__)

# How many author-year matches to hand to the agent.
AUTHOR_YEAR_MAX_CANDIDATES = 10


@dataclass
class ResolvedSuggestion:
    """Outcome of looking up one suggestion."""
    suggestion: SuggestedCitation
    candidates: list[CitationCandidate] = field(default_factory=list)
    source: str = ""            # where the first candidate came from
    total_matches: int = 0      # author-year: number of papers matching author+year
    error: str = ""             # human-readable reason when nothing was found

    @property
    def resolved(self) -> bool:
        return bool(self.candidates)

    @property
    def ambiguous(self) -> bool:
        return self.suggestion.kind == SuggestionKind.AUTHOR_YEAR and (
            len(self.candidates) > 1 or self.total_matches > 1
        )


class SuggestionResolver:
    """Look up author-suggested citations in the library and literature databases."""

    def __init__(
        self,
        pubmed: PubMedClient,
        europepmc: Optional[EuropePMCClient] = None,
        biorxiv: Optional[BioRxivClient] = None,
        medrxiv: Optional[BioRxivClient] = None,
        user_library: Optional[ReferenceLibrary] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ):
        self.pubmed = pubmed
        self.europepmc = europepmc
        self.biorxiv = biorxiv
        self.medrxiv = medrxiv
        self.user_library = user_library
        self._status = status_callback or (lambda _msg: None)

    # ── Public API ──────────────────────────────────────────────────────

    def resolve_all(self, suggestions: list[SuggestedCitation]) -> list[ResolvedSuggestion]:
        return [self.resolve(s) for s in suggestions]

    def resolve(self, suggestion: SuggestedCitation) -> ResolvedSuggestion:
        try:
            if suggestion.kind == SuggestionKind.PMID:
                return self._resolve_pmid(suggestion)
            if suggestion.kind == SuggestionKind.PMCID:
                return self._resolve_pmcid(suggestion)
            if suggestion.kind == SuggestionKind.DOI:
                return self._resolve_doi(suggestion)
            if suggestion.kind == SuggestionKind.AUTHOR_YEAR:
                return self._resolve_author_year(suggestion)
        except Exception as exc:  # network / parsing problems must not kill the pipeline
            logger.warning(f"Resolving {suggestion.label} failed: {exc}")
            return ResolvedSuggestion(suggestion=suggestion, error=f"Lookup failed: {exc}")
        return ResolvedSuggestion(suggestion=suggestion, error="Unsupported suggestion kind")

    # ── Per-kind resolution ─────────────────────────────────────────────

    def _resolve_pmid(self, s: SuggestedCitation) -> ResolvedSuggestion:
        pmid = s.value
        self._status(f"Looking up PMID {pmid}")
        if self.user_library:
            hit = self.user_library.find_by_pmid(pmid)
            if hit:
                return ResolvedSuggestion(s, [hit], source="user_library")

        article = self.pubmed.fetch_article(pmid)
        if article:
            return ResolvedSuggestion(s, [article], source="pubmed")

        if self.europepmc:
            article = self.europepmc.fetch_by_pmid(pmid)
            if article:
                return ResolvedSuggestion(s, [article], source="europepmc")

        return ResolvedSuggestion(s, error=f"PMID {pmid} was not found in your library, PubMed, or Europe PMC")

    def _resolve_pmcid(self, s: SuggestedCitation) -> ResolvedSuggestion:
        pmcid = s.value
        self._status(f"Looking up {pmcid}")
        if self.user_library:
            hit = self.user_library.find_by_pmcid(pmcid)
            if hit:
                return ResolvedSuggestion(s, [hit], source="user_library")

        pmid = self.pubmed.pmcid_to_pmid(pmcid)
        if pmid:
            article = self.pubmed.fetch_article(pmid)
            if article:
                if not article.pmcid:
                    article.pmcid = pmcid
                return ResolvedSuggestion(s, [article], source="pubmed")

        if self.europepmc:
            article = self.europepmc.fetch_by_pmcid(pmcid)
            if article:
                if not article.pmcid:
                    article.pmcid = pmcid
                # Prefer the richer PubMed record when a PMID is known
                if article.pmid:
                    full = self.pubmed.fetch_article(article.pmid)
                    if full:
                        if not full.pmcid:
                            full.pmcid = pmcid
                        return ResolvedSuggestion(s, [full], source="pubmed")
                return ResolvedSuggestion(s, [article], source="europepmc")

        return ResolvedSuggestion(s, error=f"{pmcid} was not found in your library, PubMed Central, or Europe PMC")

    def _resolve_doi(self, s: SuggestedCitation) -> ResolvedSuggestion:
        doi = s.value
        # bioRxiv/medRxiv DOIs are often written with a version suffix
        # ("...574066v2"); the databases index the unversioned DOI.
        lookup_doi = re.sub(r"v\d+$", "", doi) if doi.lower().startswith("10.1101/") else doi
        self._status(f"Looking up doi:{doi}")
        if self.user_library:
            hit = self.user_library.find_by_doi(lookup_doi) or self.user_library.find_by_doi(doi)
            if hit:
                return ResolvedSuggestion(s, [hit], source="user_library")

        for pmid in self.pubmed.find_pmids_by_doi(lookup_doi):
            article = self.pubmed.fetch_article(pmid)
            if article and self._doi_matches(article.doi, lookup_doi):
                return ResolvedSuggestion(s, [article], source="pubmed")

        if self.europepmc:
            article = self.europepmc.fetch_by_doi(lookup_doi)
            if article and (not article.doi or self._doi_matches(article.doi, lookup_doi)):
                return ResolvedSuggestion(s, [article], source="europepmc")

        for client, name in ((self.biorxiv, "biorxiv"), (self.medrxiv, "medrxiv")):
            if not client:
                continue
            try:
                preprint = client.fetch_preprint(lookup_doi)
            except Exception as exc:
                logger.debug(f"{name} lookup failed for {doi}: {exc}")
                continue
            if preprint:
                return ResolvedSuggestion(s, [preprint], source=name)

        return ResolvedSuggestion(
            s, error=f"doi:{doi} was not found in your library, PubMed, Europe PMC, bioRxiv, or medRxiv",
        )

    @staticmethod
    def _doi_matches(a: str, b: str) -> bool:
        norm = lambda d: re.sub(r"^https?://(?:dx\.)?doi\.org/", "", (d or "").strip(), flags=re.I).lower().rstrip(".")
        return bool(a) and bool(b) and norm(a) == norm(b)

    def _resolve_author_year(self, s: SuggestedCitation) -> ResolvedSuggestion:
        self._status(f"Looking up {s.label}")
        candidates: list[CitationCandidate] = []
        seen: set[str] = set()
        source = ""

        def _add(cands: list[CitationCandidate], src: str):
            nonlocal source
            for c in cands:
                key = candidate_key(c)
                if key and key not in seen:
                    seen.add(key)
                    candidates.append(c)
                    if not source:
                        source = src

        if self.user_library:
            _add(self.user_library.find_by_author_year(s.author, s.year, s.coauthor), "user_library")

        total = 0
        pmids, total = self.pubmed.search_author_year(
            s.author, s.year, coauthor=s.coauthor, max_results=AUTHOR_YEAR_MAX_CANDIDATES,
        )
        if pmids:
            _add(self.pubmed.fetch_articles(pmids), "pubmed")

        if not candidates and self.europepmc:
            _add(self.europepmc.search_author_year(
                s.author, s.year, coauthor=s.coauthor, max_results=AUTHOR_YEAR_MAX_CANDIDATES,
            ), "europepmc")

        if not candidates:
            return ResolvedSuggestion(
                s, total_matches=total,
                error=f"No paper by {s.author} ({s.year}) was found in your library, PubMed, or Europe PMC",
            )
        return ResolvedSuggestion(
            s, candidates[:AUTHOR_YEAR_MAX_CANDIDATES], source=source,
            total_matches=max(total, len(candidates)),
        )
