"""LLM-powered citation agent using Claude tool-use.

Replaces the deterministic claim extraction, candidate retrieval,
ranking, and verification stages with a single Claude agent loop
that searches PubMed iteratively and selects the best citations.
"""

import json
import logging
import os
import re
from typing import Optional, Callable

import anthropic
import certifi
import httpx

from ..models.sentence import SentenceRecord, MarkerType
from ..models.citation import CitationCandidate
from ..models.evidence import (
    EvidenceRecord, ConfidenceLevel, VerificationStatus,
)
from ..services.pubmed_client import PubMedClient
from ..services.biorxiv_client import BioRxivClient
from ..services.europepmc_client import EuropePMCClient
from ..services.ref_library import ReferenceLibrary
from ..services.tool_executor import ToolExecutor, extract_json

logger = logging.getLogger(__name__)

MAX_AGENT_ROUNDS = 6

SYSTEM_PROMPT = """\
You are a biomedical citation-finding assistant. Find references that \
support a given claim from a research document.

You can search PubMed, bioRxiv, and Europe PMC (which indexes PubMed, PMC, \
and preprints with full-text search).

IMPORTANT WORKFLOW — you have a MAXIMUM of {max_rounds} tool-call rounds. \
Follow this efficient workflow:

Step 1 (round 1): If available, search the user library first. Then search PubMed \
with 1-2 well-crafted queries. Optionally also search Europe PMC for broader coverage.
Step 2 (round 2): Fetch articles for the most promising PMIDs from Step 1.
Step 3 (round 3): Read the abstracts. If you found good matches, STOP and \
return your JSON answer. If not, do ONE more targeted search.
Step 4 (round 4 max): Fetch any new articles, then STOP and return your JSON answer.

DO NOT keep searching endlessly. After 2-3 searches and 1-2 fetches, you should \
have enough information to make a selection. Select the best available match even \
if it is not perfect.

For a (REF) marker, select exactly 1 best reference. For a (REFS) marker, \
select up to {max_refs} references.

Prefer peer-reviewed primary research over reviews. Prefer recent publications \
when relevance is equal.

If the tool `search_user_library` is available, call it in round 1 before
external searches and strongly prefer those results when they support the claim.

CRITICAL: When you are ready to give your answer, respond with ONLY a JSON \
object. No other text before or after. No markdown fencing. Just the raw JSON:
{{
  "selected": [
    {{
      "pmid": "12345678",
      "doi": "",
      "title": "",
      "why": "Brief explanation of why this paper supports the claim"
    }}
  ],
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "confidence_score": 0-100,
  "confidence_rationale": "Why you are this confident",
  "verification_status": "verified" | "partial" | "indirect" | "weak",
  "supporting_snippets": ["Relevant quote from abstract..."],
  "search_queries_used": ["query1", "query2"],
  "all_pmids_considered": ["12345678", "23456789"]
}}

If no supporting references exist, return the same JSON with "selected": [] \
and "confidence": "LOW".
"""

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
                    "description": "PubMed search query"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of PMIDs to return (default 20)",
                    "default": 20
                }
            },
            "required": ["query"]
        }
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
                    "description": "List of PubMed IDs to fetch"
                }
            },
            "required": ["pmids"]
        }
    },
]

USER_LIBRARY_TOOL = {
    "name": "search_user_library",
    "description": (
        "Search the user's local AI REFs library. Use this first when available. "
        "Returns full citation metadata for matching references."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query for the user library"
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum results to return (default 10)",
                "default": 10
            }
        },
        "required": ["query"]
    }
}

BIORXIV_TOOL = {
    "name": "search_biorxiv",
    "description": (
        "Search bioRxiv preprints by keywords. Returns matching preprints with "
        "title, authors, year, abstract, and DOI. Use this to supplement PubMed "
        "results with recent preprints that may not yet be indexed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Search keywords"
            },
            "category": {
                "type": "string",
                "description": "Optional bioRxiv category (e.g. 'neuroscience', 'cell_biology')",
                "default": ""
            }
        },
        "required": ["keywords"]
    }
}

EUROPEPMC_TOOL = {
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
                "description": "Search query (free text or Europe PMC query syntax)"
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default 20)",
                "default": 20
            }
        },
        "required": ["query"]
    }
}


class LLMCitationAgent:
    """Claude-powered citation finder using tool-use."""

    def __init__(
        self,
        anthropic_api_key: str,
        model: str,
        pubmed_client: PubMedClient,
        biorxiv_client: Optional[BioRxivClient] = None,
        europepmc_client: Optional[EuropePMCClient] = None,
        reference_library: Optional[ReferenceLibrary] = None,
        search_biorxiv: bool = True,
        search_europepmc: bool = True,
        prefer_user_library: bool = True,
        max_library_results: int = 10,
        max_refs: int = 3,
        orcid_id: Optional[str] = None,
        log_callback: Optional[Callable[[str, str], None]] = None,
    ):
        # Allow corporate TLS-inspecting proxies (e.g. Zscaler) to work by
        # honoring SSL_CERT_FILE / REQUESTS_CA_BUNDLE if set, otherwise fall
        # back to certifi's bundled CA list. Never disable verification.
        ca_bundle = (
            os.environ.get("SSL_CERT_FILE")
            or os.environ.get("REQUESTS_CA_BUNDLE")
            or certifi.where()
        )
        self.client = anthropic.Anthropic(
            api_key=anthropic_api_key,
            http_client=httpx.Client(verify=ca_bundle),
        )
        self.model = model
        self.pubmed = pubmed_client
        self.biorxiv = biorxiv_client
        self.europepmc = europepmc_client
        self.reference_library = reference_library
        self.search_biorxiv = search_biorxiv and biorxiv_client is not None
        self.search_europepmc = search_europepmc and europepmc_client is not None
        self.prefer_user_library = prefer_user_library
        self.max_library_results = max(3, min(max_library_results, 50))
        self.max_refs = max_refs
        self.orcid_id = orcid_id
        self._log = log_callback or (lambda *a: None)

        # Build tool list
        self.tools = list(TOOLS)
        if self.reference_library:
            self.tools.insert(0, USER_LIBRARY_TOOL)
        if self.search_biorxiv:
            self.tools.append(BIORXIV_TOOL)
        if self.search_europepmc:
            self.tools.append(EUROPEPMC_TOOL)

    def _emit(self, level: str, msg: str):
        getattr(logger, level, logger.info)(msg)
        self._log(level, msg)

    def find_citations(
        self,
        sentence: SentenceRecord,
        domain_context: list[str] = None,
    ) -> EvidenceRecord:
        """Run the agent loop for a single sentence. Returns an EvidenceRecord."""
        marker_type = sentence.marker_type or MarkerType.REF
        # Multi-(REF) sentences are now handled by the orchestrator which
        # calls find_citations once per marker with marker_count=1.
        if marker_type == MarkerType.REFS:
            num_refs = self.max_refs
        else:
            num_refs = 1

        system = SYSTEM_PROMPT.format(max_refs=num_refs, max_rounds=MAX_AGENT_ROUNDS)

        domain_info = ""
        if domain_context:
            domain_info = f"\nDocument research domains: {', '.join(domain_context)}"

        user_library_info = ""
        if self.reference_library:
            if self.prefer_user_library:
                user_library_info = (
                    "\nA user reference library is available. You should search it first "
                    f"and prefer those papers when relevance is comparable. Use "
                    f"`max_results={self.max_library_results}` when calling search_user_library."
                )
            else:
                user_library_info = (
                    "\nA user reference library is available; search it first, then balance "
                    f"against external literature. Use `max_results={self.max_library_results}` "
                    "when calling search_user_library."
                )

        # Detect self-referencing language (our, we, our lab, etc.)
        orcid_info = ""
        if self.orcid_id:
            self_ref_patterns = re.compile(
                r'\b(our|we|our lab|our group|our previous|our prior|our recent|'
                r'our earlier|our extensive|we previously|we have shown|'
                r'we developed|we reported)\b', re.IGNORECASE
            )
            if self_ref_patterns.search(sentence.clean_text):
                orcid_info = (
                    f"\nIMPORTANT: This sentence references the document author's OWN work. "
                    f"The author's ORCID is {self.orcid_id}. "
                    f"Search PubMed using the author's ORCID: {self.orcid_id}[auid] "
                    f"combined with relevant topic keywords. This will help find the "
                    f"correct self-cited paper."
                )

        user_message = (
            f"Find {'1 reference' if num_refs == 1 else f'{num_refs} references'} "
            f"that support this claim:\n\n"
            f"\"{sentence.clean_text}\"{domain_info}{user_library_info}{orcid_info}"
        )

        messages = [{"role": "user", "content": user_message}]

        # Track all candidates seen across rounds
        all_pmids_seen: dict[str, CitationCandidate] = {}
        queries_used = []

        executor = ToolExecutor(
            pubmed=self.pubmed,
            biorxiv=self.biorxiv if self.search_biorxiv else None,
            europepmc=self.europepmc if self.search_europepmc else None,
            user_library=self.reference_library,
            all_candidates=all_pmids_seen,
            status_callback=lambda msg: self._emit("info", f"    {msg}"),
        )

        evidence = EvidenceRecord(sentence_id=sentence.id)

        try:
            for round_num in range(MAX_AGENT_ROUNDS):
                self._emit("info", f"    Agent round {round_num + 1}...")

                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=system,
                    tools=self.tools,
                    messages=messages,
                )

                # Check if Claude wants to use tools
                if response.stop_reason == "tool_use":
                    # Process all tool calls in this response
                    tool_results = []
                    for block in response.content:
                        if block.type == "tool_use":
                            tool_result = executor.execute(
                                block.name, block.input,
                            )
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": tool_result,
                            })

                            # Track search queries
                            if block.name == "search_pubmed":
                                queries_used.append(block.input.get("query", ""))

                    # Add assistant response and tool results to conversation
                    messages.append({"role": "assistant", "content": response.content})

                    # Inject deadline nudge after round 3 to prevent endless searching
                    remaining = MAX_AGENT_ROUNDS - round_num - 1
                    if round_num >= 3 and remaining <= 3:
                        nudge = {
                            "type": "text",
                            "text": (
                                f"[SYSTEM: You have {remaining} round(s) left. "
                                "You MUST return your final JSON answer now. "
                                "Select the best match from what you have found so far, "
                                "even if it is not perfect. Do NOT call any more tools. "
                                "Respond with ONLY the JSON object.]"
                            ),
                        }
                        tool_results.append(nudge)

                    messages.append({"role": "user", "content": tool_results})

                elif response.stop_reason == "end_turn":
                    # Claude is done — parse the final response
                    text = ""
                    for block in response.content:
                        if hasattr(block, "text"):
                            text += block.text

                    self._emit("debug", f"    Raw final response ({len(text)} chars): {text[:300]}")
                    evidence = self._parse_response(
                        text, sentence, all_pmids_seen, queries_used
                    )
                    self._emit(
                        "info",
                        f"    Result: {len(evidence.selected)} ref(s), "
                        f"confidence={evidence.confidence_level.value}"
                    )
                    return evidence

                else:
                    # Unexpected stop reason
                    self._emit(
                        "warning",
                        f"    Unexpected stop_reason: {response.stop_reason}"
                    )
                    break

            # Hit max rounds — force one final answer from Claude
            self._emit("warning", f"    Hit max rounds ({MAX_AGENT_ROUNDS}), forcing final answer...")
            try:
                # Build a summary of what we've seen and ask for final answer
                seen_summary = []
                for pmid, cand in list(all_pmids_seen.items())[:20]:
                    seen_summary.append(f"- PMID {pmid}: {cand.title} ({cand.year})")
                summary_text = "\n".join(seen_summary) if seen_summary else "(no articles fetched)"

                force_msg = (
                    f"DEADLINE REACHED. You must answer NOW.\n\n"
                    f"Articles you have seen:\n{summary_text}\n\n"
                    f"Select the best match(es) from the articles above and return "
                    f"your JSON answer immediately. If none are relevant, return "
                    f'an empty "selected" list. Respond with ONLY the JSON object.'
                )
                messages.append({"role": "user", "content": force_msg})

                final_response = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=system,
                    tools=[],  # No tools — force text-only response
                    messages=messages,
                )

                final_text = ""
                for block in final_response.content:
                    if hasattr(block, "text"):
                        final_text += block.text

                if final_text.strip():
                    self._emit("debug", f"    Forced final response: {final_text[:200]}")
                    evidence = self._parse_response(
                        final_text, sentence, all_pmids_seen, queries_used
                    )
                    self._emit(
                        "info",
                        f"    Forced result: {len(evidence.selected)} ref(s), "
                        f"confidence={evidence.confidence_level.value}"
                    )
                    return evidence
            except Exception as e:
                self._emit("warning", f"    Forced final answer failed: {e}")

            # True fallback if forced answer also fails
            evidence.candidates = list(all_pmids_seen.values())
            evidence.search_query = " | ".join(queries_used)
            evidence.confidence_level = ConfidenceLevel.LOW
            evidence.confidence_rationale = (
                f"Agent hit {MAX_AGENT_ROUNDS}-round limit without completing"
            )
            if not evidence.selected:
                evidence.retrieval_error = "AI agent found no supporting citations"
            return evidence

        except anthropic.AuthenticationError as e:
            raise RuntimeError(
                f"Anthropic API authentication failed. Check your API key. ({e})"
            ) from e
        except anthropic.APIError as e:
            self._emit("error", f"    Anthropic API error: {e}")
            evidence.retrieval_error = f"API error: {e}"
            evidence.confidence_level = ConfidenceLevel.UNRESOLVED
            return evidence
        except Exception as e:
            self._emit("error", f"    Agent error: {e}")
            evidence.retrieval_error = str(e)
            evidence.confidence_level = ConfidenceLevel.UNRESOLVED
            return evidence

    def _parse_response(
        self,
        text: str,
        sentence: SentenceRecord,
        all_candidates: dict[str, CitationCandidate],
        queries_used: list[str],
    ) -> EvidenceRecord:
        """Parse Claude's final JSON response into an EvidenceRecord."""
        evidence = EvidenceRecord(sentence_id=sentence.id)

        try:
            data = extract_json(text)
        except (json.JSONDecodeError, ValueError) as e:
            self._emit("error", f"    Failed to parse agent response: {e}")
            self._emit("debug", f"    Raw response: {text[:500]}")
            evidence.retrieval_error = f"Failed to parse agent response: {e}"
            evidence.confidence_level = ConfidenceLevel.UNRESOLVED
            evidence.candidates = list(all_candidates.values())
            evidence.search_query = " | ".join(queries_used)
            return evidence

        # Selected citations — support both PMID and DOI keys
        selected_keys = []
        for sel in data.get("selected", []):
            pmid = str(sel.get("pmid", "")).strip()
            doi = str(sel.get("doi", "")).strip()
            title = str(sel.get("title", "")).strip()
            if pmid:
                selected_keys.append(pmid)
            elif doi:
                selected_keys.append(doi)
            elif title:
                selected_keys.append(title)

        # Fetch any selected PMIDs we haven't seen yet (DOIs are already tracked)
        missing_pmids = [
            k for k in selected_keys
            if k not in all_candidates and k.isdigit()
        ]
        if missing_pmids:
            fetched = self.pubmed.fetch_articles(missing_pmids)
            for a in fetched:
                all_candidates[a.pmid] = a

        # Build selected list
        evidence.selected = [
            all_candidates[k] for k in selected_keys if k in all_candidates
        ]

        # All candidates considered (PMIDs and DOIs)
        considered_ids = data.get("all_pmids_considered", [])
        for p in considered_ids:
            if p not in all_candidates and str(p).isdigit():
                fetched = self.pubmed.fetch_articles([p])
                for a in fetched:
                    all_candidates[a.pmid] = a
        evidence.candidates = list(all_candidates.values())

        # Confidence
        conf_str = data.get("confidence", "LOW").upper()
        conf_map = {
            "HIGH": ConfidenceLevel.HIGH,
            "MEDIUM": ConfidenceLevel.MEDIUM,
            "LOW": ConfidenceLevel.LOW,
        }
        evidence.confidence_level = conf_map.get(conf_str, ConfidenceLevel.LOW)
        try:
            evidence.confidence_score = float(data.get("confidence_score", 0))
        except (TypeError, ValueError):
            evidence.confidence_score = 0.0
        evidence.confidence_rationale = data.get("confidence_rationale", "")

        # Verification
        ver_str = data.get("verification_status", "not_checked").lower()
        ver_map = {
            "verified": VerificationStatus.VERIFIED,
            "partial": VerificationStatus.PARTIAL,
            "indirect": VerificationStatus.INDIRECT,
            "weak": VerificationStatus.WEAK,
        }
        evidence.verification_status = ver_map.get(
            ver_str, VerificationStatus.NOT_CHECKED
        )

        # Snippets and queries
        evidence.abstract_snippets = data.get("supporting_snippets", [])
        evidence.search_query = " | ".join(
            data.get("search_queries_used", queries_used)
        )
        evidence.search_result_count = len(all_candidates)

        return evidence
