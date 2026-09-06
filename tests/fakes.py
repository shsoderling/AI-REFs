"""Network-free stand-ins for the Anthropic client and the literature clients.

Every test that exercises the citation agent, the chat worker, the verifier
or the orchestrator goes through these fakes; nothing here touches the
network.  ``FakeAnthropic`` replays a script of responses so a test can
drive the tool-use loop deterministically and then inspect the kwargs of
every ``messages.create`` call it received.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable, Optional

import anthropic
import httpx

from src.models.citation import Author, CitationCandidate
from src.storage.cache_db import CacheDB


# ── Anthropic message/block builders ─────────────────────────────────

def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def thinking_block(thinking: str = "...") -> SimpleNamespace:
    return SimpleNamespace(type="thinking", thinking=thinking, signature="sig")


def tool_use_block(name: str, input: dict, id: str = "tu_1") -> SimpleNamespace:
    # Deliberately no ``text`` attribute: the loops use hasattr(block, "text").
    return SimpleNamespace(type="tool_use", name=name, input=dict(input), id=id)


def message(*blocks: SimpleNamespace, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


def tool_use_msg(*calls: tuple[str, dict]) -> SimpleNamespace:
    """A response asking for one or more tool calls."""
    blocks = [tool_use_block(name, inp, id=f"tu_{i + 1}") for i, (name, inp) in enumerate(calls)]
    return message(*blocks, stop_reason="tool_use")


def submit_msg(selected: list[dict], confidence: str = "HIGH", score: int = 85,
               status: str = "verified", snippets: tuple[str, ...] = (),
               queries: tuple[str, ...] = (), considered: tuple[str, ...] = (),
               rationale: str = "clear match", text: str = "") -> SimpleNamespace:
    """The agent's final answer through the ``submit_citations`` tool."""
    payload = {
        "selected": [
            {"pmid": s.get("pmid", ""), "doi": s.get("doi", ""),
             "title": s.get("title", ""), "why": s.get("why", "supports the claim")}
            for s in selected
        ],
        "confidence": confidence,
        "confidence_score": score,
        "confidence_rationale": rationale,
        "verification_status": status,
        "supporting_snippets": list(snippets),
        "search_queries_used": list(queries),
        "all_pmids_considered": list(considered),
    }
    blocks = ([text_block(text)] if text else []) + [tool_use_block("submit_citations", payload, id="tu_submit")]
    return message(*blocks, stop_reason="tool_use")


def verdict_msg(verdict: str, quote: str, reason: str = "") -> SimpleNamespace:
    """The verifier's answer through the ``record_verdict`` tool."""
    return message(
        tool_use_block("record_verdict", {"verdict": verdict, "quote": quote, "reason": reason},
                       id="tu_verdict"),
        stop_reason="tool_use",
    )


def bad_request(msg: str) -> anthropic.BadRequestError:
    """A 400 error shaped like the SDK raises it."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(400, request=request)
    return anthropic.BadRequestError(msg, response=response, body={"error": {"message": msg}})


class FakeAnthropic:
    """Scriptable ``anthropic.Anthropic`` replacement.

    ``script`` items are consumed in order by ``messages.create``: a message
    object is returned, an exception is raised, a callable is invoked with
    the call kwargs and its return value is used.  Every call's kwargs are
    recorded in ``calls``.  Running past the end of the script raises.
    """

    def __init__(self, script: Optional[list[Any]] = None):
        self.script = list(script or [])
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError(
                f"FakeAnthropic script exhausted after {len(self.calls)} call(s)")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(kwargs)
        return item

    @property
    def last_call(self) -> dict:
        return self.calls[-1]


# ── Literature clients ───────────────────────────────────────────────

def article(pmid: str, title: str, year: int = 2020, doi: str = "", abstract: str = "",
            pmcid: str = "", **extra) -> CitationCandidate:
    return CitationCandidate(pmid=pmid, doi=doi, title=title, year=year, abstract=abstract,
                             pmcid=pmcid, journal="J Test",
                             authors=[Author(last_name="Smith", initials="J")], **extra)


class FakePubMed:
    """Stand-in for PubMedClient — no network."""

    def __init__(self, by_citation=None, by_search=None, articles=None, api_key: str = ""):
        self.by_citation = by_citation or {}
        self.by_search = by_search or {}     # query -> (pmids, total)
        self.articles = articles or {}       # pmid -> CitationCandidate
        self.api_key = api_key
        self.cache = CacheDB(":memory:")
        self.search_calls: list[str] = []

    def citation_match(self, journal, year, volume, first_page, author_last):
        return self.by_citation.get((journal, year, volume, first_page, author_last))

    def search(self, query, max_results=20):
        self.search_calls.append(query)
        return self.by_search.get(query, ([], 0))

    def fetch_article(self, pmid):
        return self.articles.get(pmid)

    def fetch_articles(self, pmids):
        return [self.articles[p] for p in pmids if p in self.articles]


class FakeEuropePMC:
    """Stand-in for EuropePMCClient."""

    def __init__(self, by_search=None, by_doi=None, by_pmid=None, fulltext=None):
        self.by_search = by_search or {}     # query -> list[CitationCandidate]
        self.by_doi = by_doi or {}
        self.by_pmid = by_pmid or {}
        self.fulltext = fulltext or {}       # PMCID -> list[(section, paragraph)]
        self.fulltext_calls: list[str] = []
        self.cache = CacheDB(":memory:")

    def search(self, query, max_results=20):
        return list(self.by_search.get(query, []))[:max_results]

    def fetch_by_doi(self, doi):
        return self.by_doi.get(doi)

    def fetch_by_pmid(self, pmid):
        return self.by_pmid.get(pmid)

    def fetch_full_text(self, pmcid):
        self.fulltext_calls.append(pmcid)
        return self.fulltext.get(pmcid)


class FakeBioRxiv:
    """Stand-in for BioRxivClient."""

    def __init__(self, by_keywords=None, preprints=None):
        self.by_keywords = by_keywords or {}  # tuple(keywords) -> list[CitationCandidate]
        self.preprints = preprints or {}      # doi -> CitationCandidate
        self.cache = CacheDB(":memory:")

    def search_by_keywords(self, keywords, categories=None, days=0, max_results=10):
        return list(self.by_keywords.get(tuple(keywords), []))[:max_results]

    def fetch_preprint(self, doi):
        return self.preprints.get(doi)


class FakeLibrary:
    """Stand-in for ReferenceLibrary.search/close."""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.closed = False
        self.queries: list[str] = []

    def search(self, query, max_results=10):
        self.queries.append(query)
        return self.results[:max_results]

    def close(self):
        self.closed = True


# Sentinel that tests can plug into orchestrator-level fakes
def noop_log(*_args: Any) -> None:
    return None


LogCallback = Callable[[str, str], None]
