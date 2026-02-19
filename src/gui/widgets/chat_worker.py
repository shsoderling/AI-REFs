"""Background worker for Claude-powered citation search chat."""

import json
import logging
from typing import Optional

from PySide6.QtCore import QThread, Signal

import anthropic

from ...models.citation import CitationCandidate
from ...services.pubmed_client import PubMedClient
from ...services.biorxiv_client import BioRxivClient
from ...services.europepmc_client import EuropePMCClient
from ...services.ref_library import ReferenceLibrary
from ...services.tool_executor import ToolExecutor, extract_json
from ...storage.cache_db import CacheDB

logger = logging.getLogger(__name__)

MAX_CHAT_ROUNDS = 15

CHAT_SYSTEM_PROMPT = """\
You are a helpful biomedical citation assistant. The user needs help finding \
a reference for a specific claim in their research paper.

The claim is:
"{claim_text}"

You can search PubMed, bioRxiv, and Europe PMC to find relevant papers. \
Europe PMC indexes PubMed, PMC full-text articles, and preprints with \
full-text keyword search. When you find \
good candidates, present them in a numbered list with this format:

**#1** Authors (Year). Title. Journal.
PMID: 12345678 | DOI: 10.xxxx/yyyy
> Brief explanation of why this paper supports the claim

**#2** Authors (Year). Title. Journal.
PMID: 23456789 | DOI: 10.xxxx/zzzz
> Brief explanation of relevance

After presenting options, ask the user which one(s) they want to use. \
The user can say things like "use #2" or "use the Smith paper".

When the user confirms a selection, respond with ONLY this JSON (no other text):
{{"action": "select", "selections": [{{"pmid": "12345678", "doi": "", "title": ""}}]}}

Be concise and helpful. Present at most 5 candidates per search.

IMPORTANT: You have a maximum of {max_rounds} tool-call rounds per search. \
Be efficient: do 1-2 searches, fetch the most promising results, and present them.
"""

USER_LIBRARY_TOOL = {
    "name": "search_user_library",
    "description": (
        "Search the user's local AI REFs reference library. Use this first when "
        "available, then supplement with external searches."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum results to return (default 10)",
                "default": 10,
            },
        },
        "required": ["query"],
    },
}

TOOLS = [
    {
        "name": "search_pubmed",
        "description": (
            "Search PubMed for articles matching a query. Returns a list of "
            "PubMed IDs (PMIDs) and the total number of results. Use standard "
            "PubMed query syntax: combine terms with AND/OR, use [MeSH] tags, "
            "field tags like [ti] (title), [tiab] (title/abstract), [au] (author). "
            "Example: 'Rac1 AND dendritic spine AND hippocampus'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "PubMed search query",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of PMIDs to return (default 20)",
                    "default": 20,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_articles",
        "description": (
            "Fetch full metadata for one or more PubMed articles by their PMIDs. "
            "Returns title, authors, year, journal, abstract, DOI, MeSH terms "
            "for each article. Use this after search_pubmed to read abstracts "
            "and evaluate relevance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pmids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of PubMed IDs to fetch",
                }
            },
            "required": ["pmids"],
        },
    },
]

BIORXIV_TOOL = {
    "name": "search_biorxiv",
    "description": (
        "Search bioRxiv preprints by keywords. Returns matching preprints with "
        "title, authors, year, abstract, and DOI. Use this to supplement PubMed "
        "results with recent preprints that may not yet be indexed. "
        "Note: bioRxiv keyword search is limited — it only scans recent preprints. "
        "If you know the DOI, use fetch_biorxiv_preprint instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Search keywords",
            },
            "category": {
                "type": "string",
                "description": "Optional bioRxiv category (e.g. 'neuroscience', 'cell_biology')",
                "default": "",
            },
        },
        "required": ["keywords"],
    },
}

BIORXIV_FETCH_TOOL = {
    "name": "fetch_biorxiv_preprint",
    "description": (
        "Fetch full metadata for a specific bioRxiv or medRxiv preprint by its DOI. "
        "Use this when you know the DOI of a preprint (e.g., '10.1101/2024.01.15.123456' "
        "or newer format like '10.64898/2026.01.04.697581'). "
        "Returns title, authors, year, abstract, and DOI."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "doi": {
                "type": "string",
                "description": "The DOI of the preprint (e.g., '10.1101/2024.01.15.123456')",
            },
        },
        "required": ["doi"],
    },
}

EUROPEPMC_SEARCH_TOOL = {
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
            "query": {
                "type": "string",
                "description": "Search query (free text or Europe PMC query syntax)",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default 20)",
                "default": 20,
            },
        },
        "required": ["query"],
    },
}

EUROPEPMC_FETCH_TOOL = {
    "name": "fetch_europepmc_article",
    "description": (
        "Fetch a single article from Europe PMC by DOI or PMID. "
        "Use this when you have a specific identifier and need the full metadata. "
        "Provide either a DOI (e.g., '10.1038/s41586-024-07487-w') or a PMID."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "doi": {
                "type": "string",
                "description": "Article DOI (optional if pmid is given)",
                "default": "",
            },
            "pmid": {
                "type": "string",
                "description": "PubMed ID (optional if doi is given)",
                "default": "",
            },
        },
        "required": [],
    },
}


class ChatSearchWorker(QThread):
    """Background worker for a single Claude chat turn with tool-use.

    Runs the Claude tool-use loop, executing PubMed/bioRxiv searches as
    needed, and emits the assistant's final response back to the UI.
    """

    status_update = Signal(str)       # "Searching PubMed..."
    assistant_message = Signal(str)   # Claude's response text
    candidates_found = Signal(list)   # list[CitationCandidate]
    selection_made = Signal(list)     # list[CitationCandidate] — user confirmed
    error = Signal(str)

    def __init__(
        self,
        messages: list[dict],
        claim_text: str,
        anthropic_api_key: str,
        model: str,
        ncbi_email: str,
        ncbi_api_key: str = "",
        search_biorxiv: bool = True,
        search_europepmc: bool = True,
        reference_library_path: str = "",
        prefer_user_library: bool = True,
        max_library_results: int = 10,
        prior_candidates: Optional[dict] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.messages = messages
        self.claim_text = claim_text
        self.api_key = anthropic_api_key
        self.model = model
        self.ncbi_email = ncbi_email
        self.ncbi_api_key = ncbi_api_key
        self.search_biorxiv = search_biorxiv
        self.search_europepmc = search_europepmc
        self.reference_library_path = reference_library_path
        self.prefer_user_library = prefer_user_library
        self.max_library_results = max(3, min(max_library_results, 50))
        # Seed with candidates from previous turns so selections can resolve
        self.all_candidates: dict[str, CitationCandidate] = dict(prior_candidates) if prior_candidates else {}

    def run(self):
        user_library = None
        try:
            cache = CacheDB()
            pubmed = PubMedClient(
                email=self.ncbi_email,
                api_key=self.ncbi_api_key,
                cache_db=cache,
            )
            biorxiv = None
            if self.search_biorxiv:
                biorxiv = BioRxivClient(cache_db=cache, server="biorxiv")
            europepmc = None
            if self.search_europepmc:
                europepmc = EuropePMCClient(cache_db=cache)
            if self.reference_library_path:
                user_library = ReferenceLibrary(self.reference_library_path)

            executor = ToolExecutor(
                pubmed=pubmed,
                biorxiv=biorxiv,
                europepmc=europepmc,
                user_library=user_library,
                all_candidates=self.all_candidates,
                status_callback=lambda msg: self.status_update.emit(msg),
            )

            client = anthropic.Anthropic(api_key=self.api_key)

            tools = list(TOOLS)
            if user_library:
                tools.insert(0, USER_LIBRARY_TOOL)
            if self.search_biorxiv and biorxiv:
                tools.append(BIORXIV_TOOL)
                tools.append(BIORXIV_FETCH_TOOL)
            if self.search_europepmc and europepmc:
                tools.append(EUROPEPMC_SEARCH_TOOL)
                tools.append(EUROPEPMC_FETCH_TOOL)

            system = CHAT_SYSTEM_PROMPT.format(
                claim_text=self.claim_text,
                max_rounds=MAX_CHAT_ROUNDS,
            )
            if user_library:
                if self.prefer_user_library:
                    system += (
                        "\nA user library is available. Call search_user_library first and "
                        "prefer those references when relevance is similar. "
                        f"Use max_results={self.max_library_results}."
                    )
                else:
                    system += (
                        "\nA user library is available. Call search_user_library first, then "
                        "balance those options with external results."
                    )

            messages = list(self.messages)

            for round_num in range(MAX_CHAT_ROUNDS):
                response = client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=system,
                    tools=tools,
                    messages=messages,
                )

                if response.stop_reason == "tool_use":
                    tool_results = []
                    for block in response.content:
                        if block.type == "tool_use":
                            self.status_update.emit(f"Calling {block.name}...")
                            result = executor.execute(block.name, block.input)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result,
                            })

                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": tool_results})

                elif response.stop_reason == "end_turn":
                    text = ""
                    for block in response.content:
                        if hasattr(block, "text"):
                            text += block.text

                    # Check if Claude returned a selection JSON
                    selection = self._check_for_selection(text)
                    if selection:
                        self.selection_made.emit(selection)
                    else:
                        self.assistant_message.emit(text)

                    # Emit candidates found during tool calls
                    if self.all_candidates:
                        self.candidates_found.emit(list(self.all_candidates.values()))
                    return
                else:
                    break

            # Exited loop without returning — emit any candidates found and a message
            if self.all_candidates:
                self.candidates_found.emit(list(self.all_candidates.values()))
                self.assistant_message.emit(
                    "The search took many steps. Here are the articles I found so far. "
                    "You can ask me to present them or try a more specific query."
                )
            else:
                self.assistant_message.emit(
                    "I wasn't able to complete the search. Please try a more specific query."
                )

        except anthropic.AuthenticationError:
            self.error.emit(
                "Anthropic API authentication failed. Check your API key in the Input tab."
            )
        except anthropic.APIError as e:
            self.error.emit(f"API error: {e}")
        except Exception as e:
            logger.error(f"ChatSearchWorker error: {e}", exc_info=True)
            self.error.emit(str(e))
        finally:
            if user_library:
                try:
                    user_library.close()
                except Exception:
                    pass

    def _check_for_selection(self, text: str) -> Optional[list]:
        """Check if Claude's response contains a selection JSON.

        Returns list of CitationCandidate if selection found, else None.
        """
        try:
            data = extract_json(text)
        except (ValueError, json.JSONDecodeError):
            return None

        if data.get("action") != "select":
            return None

        selections = data.get("selections", [])
        result = []
        for sel in selections:
            pmid = str(sel.get("pmid", "")).strip()
            doi = str(sel.get("doi", "")).strip()
            title = str(sel.get("title", "")).strip()
            key = pmid or doi or title
            if key and key in self.all_candidates:
                result.append(self.all_candidates[key])
        return result if result else None
