"""Tool schemas shared by the citation agent, the verifier and the chat worker.

Every literature tool Claude can call is defined once here; ``ToolExecutor``
dispatches the search/fetch tools, while the *final-answer* tools
(``submit_citations``, ``select_citations``, ``record_verdict``) are consumed
by the calling loop itself and never reach the executor.
"""

from __future__ import annotations

import copy
from typing import Optional

SEARCH_USER_LIBRARY = {
    "name": "search_user_library",
    "description": (
        "Search the user's local AI REFs reference library. Use this first when "
        "available. Returns full citation metadata for matching references."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query for the user library"},
            "max_results": {"type": "integer", "description": "Maximum results to return (default 10)",
                            "default": 10},
        },
        "required": ["query"],
    },
}

SEARCH_PUBMED = {
    "name": "search_pubmed",
    "description": (
        "Search PubMed for articles matching a query. Returns a list of "
        "PubMed IDs (PMIDs) and the total number of results. Use standard "
        "PubMed query syntax: combine terms with AND/OR, use [MeSH] tags, "
        "field tags like [ti] (title), [tiab] (title/abstract), [au] (author). "
        "Example: 'Rac1 AND dendritic spine AND hippocampus'. "
        "A result carrying an \"error\" key means the request failed; retry "
        "or use search_europepmc instead of treating it as no literature."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "PubMed search query"},
            "max_results": {"type": "integer", "description": "Maximum number of PMIDs to return (default 20)",
                            "default": 20},
        },
        "required": ["query"],
    },
}

FETCH_ARTICLES = {
    "name": "fetch_articles",
    "description": (
        "Fetch full metadata for one or more PubMed articles by their PMIDs. "
        "Returns title, authors, year, journal, abstract, DOI, MeSH terms, "
        "retraction flag and whether open-access full text exists (pmcid) for "
        "each article. Use this after search_pubmed to read abstracts and "
        "evaluate relevance."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pmids": {"type": "array", "items": {"type": "string"},
                      "description": "List of PubMed IDs to fetch"},
        },
        "required": ["pmids"],
    },
}

SEARCH_BIORXIV = {
    "name": "search_biorxiv",
    "description": (
        "Search bioRxiv preprints by keywords. Returns matching preprints with "
        "title, authors, year, abstract, and DOI. Use this to supplement PubMed "
        "results with recent preprints that may not yet be indexed. "
        "Note: bioRxiv keyword search only scans recent preprints. "
        "If you know the DOI, use fetch_biorxiv_preprint instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "keywords": {"type": "array", "items": {"type": "string"}, "description": "Search keywords"},
            "category": {"type": "string",
                         "description": "Optional bioRxiv category (e.g. 'neuroscience', 'cell_biology')",
                         "default": ""},
        },
        "required": ["keywords"],
    },
}

FETCH_BIORXIV_PREPRINT = {
    "name": "fetch_biorxiv_preprint",
    "description": (
        "Fetch full metadata for a specific bioRxiv or medRxiv preprint by its DOI. "
        "Use this when you know the DOI of a preprint (e.g., '10.1101/2024.01.15.123456' "
        "or newer format like '10.64898/2026.01.04.697581'). "
        "Returns title, authors, year, abstract, DOI and the journal DOI if it was published."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "doi": {"type": "string", "description": "The DOI of the preprint"},
        },
        "required": ["doi"],
    },
}

SEARCH_EUROPEPMC = {
    "name": "search_europepmc",
    "description": (
        "Search Europe PMC for articles and preprints. Europe PMC indexes PubMed, "
        "PMC full-text articles, and preprints. Supports full-text keyword search "
        "and returns full metadata (PMID, DOI, title, authors, abstract) in one call. "
        "Use this to supplement PubMed results or to find articles not yet in PubMed. "
        "Example: 'CRISPR base editing liver disease therapy'"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query (free text or Europe PMC query syntax)"},
            "max_results": {"type": "integer", "description": "Maximum number of results to return (default 20)",
                            "default": 20},
        },
        "required": ["query"],
    },
}

FETCH_EUROPEPMC_ARTICLE = {
    "name": "fetch_europepmc_article",
    "description": (
        "Fetch a single article from Europe PMC by DOI or PMID. "
        "Use this when you have a specific identifier and need the full metadata. "
        "Provide either a DOI (e.g., '10.1038/s41586-024-07487-w') or a PMID."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "doi": {"type": "string", "description": "Article DOI (optional if pmid is given)", "default": ""},
            "pmid": {"type": "string", "description": "PubMed ID (optional if doi is given)", "default": ""},
        },
        "required": [],
    },
}

GET_FULLTEXT_PASSAGES = {
    "name": "get_fulltext_passages",
    "description": (
        "Read the open-access full text of an article from Europe PMC and return "
        "the paragraphs that best match your keywords (results, methods, "
        "discussion). Only works for articles with a PMC id and open-access "
        "full text (fetched articles report full_text_available). Use it when "
        "an abstract is ambiguous about whether the paper supports the claim."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pmcid": {"type": "string", "description": "PMC id, e.g. PMC7000000 (preferred)", "default": ""},
            "pmid": {"type": "string", "description": "PubMed id, if the PMC id is unknown", "default": ""},
            "doi": {"type": "string", "description": "DOI, if neither id is known", "default": ""},
            "keywords": {"type": "array", "items": {"type": "string"},
                         "description": "3-6 keywords or short phrases to locate the relevant passages"},
        },
        "required": ["keywords"],
    },
}

CONFIDENCE_LEVELS = ["HIGH", "MEDIUM", "LOW"]
VERIFICATION_STATUSES = ["verified", "partial", "indirect", "weak"]

SUBMIT_CITATIONS = {
    "name": "submit_citations",
    "description": (
        "Submit your final answer. Call this exactly once, when you have "
        "read enough abstracts to choose. Select the best available match "
        "even if it is not perfect; submit an empty selection only when no "
        "supporting reference exists."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "selected": {
                "type": "array",
                "description": "The chosen reference(s), best first",
                "items": {
                    "type": "object",
                    "properties": {
                        "pmid": {"type": "string", "description": "PubMed id, if any", "default": ""},
                        "doi": {"type": "string", "description": "DOI, if there is no PMID", "default": ""},
                        "title": {"type": "string", "default": ""},
                        "why": {"type": "string",
                                "description": "Brief explanation of why this paper supports the claim"},
                    },
                    "required": ["why"],
                },
            },
            "confidence": {"type": "string", "enum": CONFIDENCE_LEVELS},
            "confidence_score": {"type": "integer", "minimum": 0, "maximum": 100},
            "confidence_rationale": {"type": "string", "description": "Why you are this confident"},
            "verification_status": {"type": "string", "enum": VERIFICATION_STATUSES},
            "supporting_snippets": {"type": "array", "items": {"type": "string"},
                                    "description": "Relevant quotes from the abstracts"},
            "search_queries_used": {"type": "array", "items": {"type": "string"}},
            "all_pmids_considered": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["selected", "confidence", "confidence_score", "confidence_rationale",
                     "verification_status"],
    },
}

SELECT_CITATIONS = {
    "name": "select_citations",
    "description": (
        "Apply the user's confirmed choice of reference(s) to the sentence. "
        "Call this only after the user has confirmed which candidate(s) to use."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "selections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "pmid": {"type": "string", "default": ""},
                        "doi": {"type": "string", "default": ""},
                        "title": {"type": "string", "default": ""},
                    },
                },
            },
        },
        "required": ["selections"],
    },
}

VERDICTS = ["supports", "partial", "not_supported"]

RECORD_VERDICT = {
    "name": "record_verdict",
    "description": (
        "Record whether the paper's text supports the claim. Call exactly once. "
        "The quote must be copied verbatim from the provided text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": VERDICTS,
                        "description": "supports: the text directly supports the claim; "
                                       "partial: related but not the specific claim; "
                                       "not_supported: the text does not support or contradicts it"},
            "quote": {"type": "string",
                      "description": "One verbatim sentence or clause from the text that best supports "
                                     "the claim (empty when not_supported)"},
            "reason": {"type": "string", "description": "One-sentence justification"},
        },
        "required": ["verdict", "quote", "reason"],
    },
}

FINAL_TOOL_NAMES = {SUBMIT_CITATIONS["name"], SELECT_CITATIONS["name"], RECORD_VERDICT["name"]}


def build_tool_list(*, user_library: bool = False, biorxiv: bool = False, europepmc: bool = False,
                    fulltext: bool = False, final_tool: Optional[dict] = None,
                    cache: bool = True) -> list[dict]:
    """Assemble the tool list for one agent run.

    Order: user library, PubMed pair, bioRxiv pair, Europe PMC pair, full
    text, then the final-answer tool.  With ``cache`` the last tool carries
    a prompt-cache breakpoint so the (identical) tool prefix is reused
    across sentences when the model supports caching.
    """
    tools: list[dict] = []
    if user_library:
        tools.append(SEARCH_USER_LIBRARY)
    tools += [SEARCH_PUBMED, FETCH_ARTICLES]
    if biorxiv:
        tools += [SEARCH_BIORXIV, FETCH_BIORXIV_PREPRINT]
    if europepmc:
        tools += [SEARCH_EUROPEPMC, FETCH_EUROPEPMC_ARTICLE]
    if fulltext and europepmc:
        tools.append(GET_FULLTEXT_PASSAGES)
    if final_tool is not None:
        tools.append(final_tool)
    tools = [copy.deepcopy(t) for t in tools]
    if cache and tools:
        tools[-1]["cache_control"] = {"type": "ephemeral"}
    return tools


def find_tool_use(response, name: str):
    """Return the first ``tool_use`` block called *name* in a response, or None."""
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == name:
            return block
    return None
