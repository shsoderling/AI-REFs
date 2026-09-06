"""Background worker for Claude-powered citation search chat."""

import logging
from typing import Optional

from PySide6.QtCore import QThread, Signal

import anthropic

from ...models.citation import CitationCandidate
from ...services.pubmed_client import PubMedClient
from ...services.biorxiv_client import BioRxivClient
from ...services.europepmc_client import EuropePMCClient
from ...services.ref_library import ReferenceLibrary
from ...services.claude_client import ClaudeCaller, make_client
from ...services.search_tools import SELECT_CITATIONS, build_tool_list, find_tool_use
from ...services.tool_executor import ToolExecutor
from ...pipeline.llm_citation_agent import build_preference_text
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

When the user confirms a selection, call the `select_citations` tool with the \
chosen PMIDs/DOIs. Never call it before the user has confirmed.

Be concise and helpful. Present at most 5 candidates per search.

IMPORTANT: You have a maximum of {max_rounds} tool-call rounds per search. \
Be efficient: do 1-2 searches, fetch the most promising results, and present them.
"""

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
        client=None,
        prefer_reviews: bool = False,
        recency_bias: bool = True,
        context_text: str = "",
    ):
        super().__init__(parent)
        self._client = client  # injected in tests; built in run() otherwise
        self.preferences = build_preference_text(prefer_reviews, recency_bias)
        self.context_text = (context_text or "").strip()
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
        self._cancelled = False

    def cancel(self):
        """Request cancellation; checked between tool-use rounds."""
        self._cancelled = True

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

            client = self._client or make_client(self.api_key)
            caller = ClaudeCaller(client, self.model)

            tools = build_tool_list(
                user_library=user_library is not None,
                biorxiv=bool(self.search_biorxiv and biorxiv),
                europepmc=bool(self.search_europepmc and europepmc),
                fulltext=bool(self.search_europepmc and europepmc),
                final_tool=SELECT_CITATIONS,
            )

            system = CHAT_SYSTEM_PROMPT.format(
                claim_text=self.claim_text,
                max_rounds=MAX_CHAT_ROUNDS,
            )
            system += f"\n{self.preferences}"
            if self.context_text:
                system += f"\n\n{self.context_text}"
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
                if self._cancelled:
                    logger.info("ChatSearchWorker cancelled")
                    return
                response = caller.create(system=system, messages=messages, tools=tools)

                selection = find_tool_use(response, SELECT_CITATIONS["name"])
                if selection is not None:
                    if self._cancelled:
                        return
                    prose = "".join(getattr(b, "text", "") for b in response.content)
                    if prose.strip():
                        self.status_update.emit(prose.strip()[:80])
                    self.selection_made.emit(self._resolve_selection(selection.input))
                    return

                if response.stop_reason == "tool_use":
                    tool_results = []
                    for block in response.content:
                        if block.type == "tool_use":
                            if self._cancelled:
                                logger.info("ChatSearchWorker cancelled mid-round")
                                return
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
                    if self._cancelled:
                        logger.info("ChatSearchWorker cancelled before reply")
                        return
                    text = ""
                    for block in response.content:
                        if hasattr(block, "text"):
                            text += block.text
                    self.assistant_message.emit(text)

                    # Emit candidates found during tool calls
                    if self.all_candidates:
                        self.candidates_found.emit(list(self.all_candidates.values()))
                    return
                else:
                    break

            # Exited loop without returning — emit any candidates found and a message
            if self._cancelled:
                return
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
            if not self._cancelled:
                self.error.emit(
                    "Anthropic API authentication failed. Check your API key in the Input tab."
                )
        except anthropic.APIError as e:
            if not self._cancelled:
                self.error.emit(f"API error: {e}")
        except Exception as e:
            logger.error(f"ChatSearchWorker error: {e}", exc_info=True)
            if not self._cancelled:
                self.error.emit(str(e))
        finally:
            if user_library:
                try:
                    user_library.close()
                except Exception:
                    pass

    def _resolve_selection(self, data: dict) -> list:
        """Map the select_citations input to fetched CitationCandidates.

        Unknown identifiers are dropped; an empty list tells the panel the
        selection could not be matched.
        """
        result = []
        for sel in (data or {}).get("selections", []) or []:
            if not isinstance(sel, dict):
                continue
            pmid = str(sel.get("pmid", "") or "").strip()
            doi = str(sel.get("doi", "") or "").strip()
            title = str(sel.get("title", "") or "").strip()
            key = pmid or doi or title
            if key and key in self.all_candidates:
                result.append(self.all_candidates[key])
        return result
