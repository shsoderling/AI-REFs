"""Shared tool-execution logic for Claude tool-use agents.

Both the pipeline LLMCitationAgent and the chat ChatSearchWorker need
to dispatch the same set of PubMed / bioRxiv / Europe PMC tool calls.
This module provides a single implementation so the dispatch table and
article serialisation live in one place.
"""

import json
import logging
from typing import Optional, Callable

from ..models.citation import CitationCandidate
from ..services.pubmed_client import PubMedClient
from ..services.biorxiv_client import BioRxivClient
from ..services.europepmc_client import EuropePMCClient
from ..services.ref_library import ReferenceLibrary
from ..storage.cache_db import CacheDB

logger = logging.getLogger(__name__)


def candidate_key(article: CitationCandidate) -> str:
    """Stable key used to track an article across tool calls: PMID > DOI > title."""
    return article.pmid or article.doi or article.title


def _article_summary(article: CitationCandidate, include_mesh: bool = False) -> dict:
    """Serialise a CitationCandidate to the dict format expected by Claude."""
    d = {
        "pmid": article.pmid,
        "pmcid": article.pmcid,
        "doi": article.doi,
        "title": article.title,
        "source": article.source,
        "authors": ", ".join(
            au.display for au in article.authors[:6]
        ) + (" et al." if len(article.authors) > 6 else ""),
        "year": article.year,
        "journal": article.journal_abbrev or article.journal,
        "abstract": article.abstract[:2000],
    }
    if include_mesh:
        d["mesh_terms"] = article.mesh_terms[:10]
        d["is_review"] = article.is_review
        d["is_retracted"] = article.is_retracted
    return d


def _biorxiv_summary(candidate: CitationCandidate) -> dict:
    """Serialise a bioRxiv candidate (no PMID, no MeSH)."""
    return {
        "doi": candidate.doi,
        "title": candidate.title,
        "source": candidate.source,
        "authors": ", ".join(
            au.display for au in candidate.authors[:6]
        ) + (" et al." if len(candidate.authors) > 6 else ""),
        "year": candidate.year,
        "journal": candidate.journal,
        "abstract": candidate.abstract[:2000],
    }


def _europepmc_summary(candidate: CitationCandidate) -> dict:
    """Serialise a Europe PMC candidate."""
    return {
        "pmid": candidate.pmid,
        "doi": candidate.doi,
        "title": candidate.title,
        "source": candidate.source,
        "authors": ", ".join(
            au.display for au in candidate.authors[:6]
        ) + (" et al." if len(candidate.authors) > 6 else ""),
        "year": candidate.year,
        "journal": candidate.journal_abbrev or candidate.journal,
        "abstract": candidate.abstract[:2000],
        "is_review": candidate.is_review,
    }


class ToolExecutor:
    """Execute PubMed / bioRxiv / Europe PMC tool calls.

    Tracks all candidates seen so far in ``all_candidates`` (a dict keyed
    by PMID or DOI).

    Parameters
    ----------
    pubmed : PubMedClient
    biorxiv : BioRxivClient or None
    europepmc : EuropePMCClient or None
    all_candidates : mutable dict that accumulates every article seen
    status_callback : optional ``(str) -> None`` for progress messages
    """

    def __init__(
        self,
        pubmed: PubMedClient,
        biorxiv: Optional[BioRxivClient] = None,
        europepmc: Optional[EuropePMCClient] = None,
        user_library: Optional[ReferenceLibrary] = None,
        all_candidates: Optional[dict[str, CitationCandidate]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ):
        self.pubmed = pubmed
        self.biorxiv = biorxiv
        self.europepmc = europepmc
        self.user_library = user_library
        self.all_candidates = all_candidates if all_candidates is not None else {}
        self._status = status_callback or (lambda _: None)

    # ── public entry point ───────────────────────────────────────────

    def execute(self, tool_name: str, tool_input: dict) -> str:
        """Dispatch *tool_name* and return the JSON result string."""
        handler = self._dispatch.get(tool_name)
        if handler is None:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        try:
            return handler(self, tool_input)
        except Exception as e:
            logger.error(f"Tool execution error ({tool_name}): {e}")
            return json.dumps({"error": str(e)})

    # ── individual handlers ──────────────────────────────────────────

    def _search_pubmed(self, inp: dict) -> str:
        query = inp["query"]
        max_results = inp.get("max_results", 20)
        self._status(f"Searching PubMed: {query}")
        pmids, total = self.pubmed.search(query, max_results=max_results)
        return json.dumps({"pmids": pmids, "total_results": total, "returned": len(pmids)})

    def _fetch_articles(self, inp: dict) -> str:
        pmids = inp["pmids"]
        self._status(f"Fetching {len(pmids)} article(s)...")
        articles = self.pubmed.fetch_articles(pmids)
        for a in articles:
            if a.pmid and a.pmid not in self.all_candidates:
                self.all_candidates[a.pmid] = a
        return json.dumps([_article_summary(a, include_mesh=True) for a in articles])

    def _search_biorxiv(self, inp: dict) -> str:
        if not self.biorxiv:
            return json.dumps({"error": "bioRxiv search not available"})
        keywords = inp["keywords"]
        category = inp.get("category", "")
        self._status(f"Searching bioRxiv: {', '.join(keywords)}")
        categories = [category] if category else None
        candidates = self.biorxiv.search_by_keywords(
            keywords=keywords, categories=categories, days=365 * 3, max_results=10,
        )
        for c in candidates:
            key = c.doi or c.title
            if key and key not in self.all_candidates:
                self.all_candidates[key] = c
        return json.dumps([_biorxiv_summary(c) for c in candidates])

    def _fetch_biorxiv_preprint(self, inp: dict) -> str:
        doi = inp["doi"]
        self._status(f"Fetching bioRxiv preprint: {doi}")
        candidate = None
        for server in ("biorxiv", "medrxiv"):
            try:
                client = BioRxivClient(cache_db=CacheDB(), server=server)
                candidate = client.fetch_preprint(doi)
                if candidate:
                    break
            except Exception:
                pass
        if candidate:
            key = candidate.doi or candidate.title
            if key:
                self.all_candidates[key] = candidate
            return json.dumps(_biorxiv_summary(candidate))
        return json.dumps({"error": f"Preprint DOI '{doi}' not found on bioRxiv or medRxiv."})

    def _search_europepmc(self, inp: dict) -> str:
        if not self.europepmc:
            return json.dumps({"error": "Europe PMC search not available"})
        query = inp["query"]
        max_results = inp.get("max_results", 20)
        self._status(f"Searching Europe PMC: {query}")
        candidates = self.europepmc.search(query, max_results=max_results)
        for c in candidates:
            key = c.pmid or c.doi or c.title
            if key and key not in self.all_candidates:
                self.all_candidates[key] = c
        return json.dumps([_europepmc_summary(c) for c in candidates])

    def _fetch_europepmc_article(self, inp: dict) -> str:
        if not self.europepmc:
            return json.dumps({"error": "Europe PMC not available"})
        doi = inp.get("doi", "").strip()
        pmid = inp.get("pmid", "").strip()
        if doi:
            self._status(f"Fetching from Europe PMC: {doi}")
            candidate = self.europepmc.fetch_by_doi(doi)
        elif pmid:
            self._status(f"Fetching from Europe PMC: PMID {pmid}")
            candidate = self.europepmc.fetch_by_pmid(pmid)
        else:
            return json.dumps({"error": "Please provide either a DOI or PMID."})
        if candidate:
            key = candidate.pmid or candidate.doi or candidate.title
            if key:
                self.all_candidates[key] = candidate
            return json.dumps(_europepmc_summary(candidate))
        identifier = doi or pmid
        return json.dumps({"error": f"Article '{identifier}' not found on Europe PMC."})

    def _search_user_library(self, inp: dict) -> str:
        if not self.user_library:
            return json.dumps({"error": "User library search not available"})
        query = inp["query"]
        max_results = inp.get("max_results", 10)
        self._status(f"Searching user library: {query}")
        candidates = self.user_library.search(query, max_results=max_results)
        for c in candidates:
            key = c.pmid or c.doi or c.title
            if key and key not in self.all_candidates:
                self.all_candidates[key] = c
        return json.dumps([_article_summary(c, include_mesh=True) for c in candidates])

    # ── dispatch table ───────────────────────────────────────────────

    _dispatch: dict[str, Callable] = {
        "search_user_library": _search_user_library,
        "search_pubmed": _search_pubmed,
        "fetch_articles": _fetch_articles,
        "search_biorxiv": _search_biorxiv,
        "fetch_biorxiv_preprint": _fetch_biorxiv_preprint,
        "search_europepmc": _search_europepmc,
        "fetch_europepmc_article": _fetch_europepmc_article,
    }


def extract_json(text: str) -> dict:
    """Extract a JSON object from text that may contain surrounding prose.

    Tries:
    1. Parse entire text as JSON.
    2. Strip markdown fences and parse.
    3. Find the first ``{ ... }`` block via brace matching.
    """
    cleaned = text.strip()

    # Strategy 1: direct parse
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: strip markdown fences
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        try:
            return json.loads("\n".join(lines))
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy 3: brace matching
    start = cleaned.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except (json.JSONDecodeError, ValueError):
                        pass
                    break

    raise ValueError("No valid JSON object found in response")
