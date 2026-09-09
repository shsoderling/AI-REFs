"""LLM-powered citation agent using Claude tool-use.

Replaces the deterministic claim extraction, candidate retrieval,
ranking, and verification stages with a single Claude agent loop
that searches PubMed iteratively and selects the best citations.

Two entry points:

* :meth:`LLMCitationAgent.find_citations` -- search for references that
  support a claim (``(REF)`` / ``(REFS)`` markers).
* :meth:`LLMCitationAgent.evaluate_suggested` -- the author already named
  the reference(s) (``(PMID: ...)``, ``(Battison et al. 2024)``, ...); the
  agent confirms each one and scores how well it supports the claim.
"""

import json
import logging
import re
from typing import Optional, Callable

import anthropic

from ..models.sentence import SentenceRecord, MarkerType
from ..models.markers import MarkerSpec, SuggestedCitation, SuggestionKind
from ..models.citation import CitationCandidate
from ..models.evidence import (
    EvidenceRecord, ConfidenceLevel, VerificationStatus, Warning,
)
from ..services.pubmed_client import PubMedClient
from ..services.biorxiv_client import BioRxivClient
from ..services.europepmc_client import EuropePMCClient
from ..services.ref_library import ReferenceLibrary
from ..services.suggestion_resolver import ResolvedSuggestion
from ..services.tool_executor import (
    ToolExecutor, extract_json, candidate_key, _article_summary,
)

logger = logging.getLogger(__name__)

MAX_AGENT_ROUNDS = 6

# Below this per-citation score an author-suggested citation gets a warning.
WEAK_MATCH_THRESHOLD = 50

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

SUGGESTED_SYSTEM_PROMPT = """\
You are a biomedical citation-checking assistant. The author of a research \
document has already named the reference(s) they want to cite for a claim. \
Your job is to confirm each suggested reference and judge how well it supports \
the claim, so the author can review your assessment.

You have a MAXIMUM of {max_rounds} tool-call rounds. Usually you need none: the \
suggested references have already been looked up and their metadata is provided \
below. Use tools only when:
- an author-year citation matched several papers and you need abstracts to \
decide which one the author meant (prefer the paper whose topic matches the claim);
- an author-year citation matched nothing: search PubMed with the author's last \
name, the year, and topic keywords from the claim, e.g. \
"Battison[au] AND 2024[dp] AND synapse";
- a provided record lacks an abstract and you need it to judge support.

Rules:
1. Keep every suggested reference that resolves to a real paper in "selected", \
ONE entry per suggestion, even if it supports the claim poorly. Give it a low \
score and explain why in "why". NEVER replace an author's suggestion with a \
different paper.
2. Only use pmid/doi values that appear in the provided records or in tool \
results. Never invent identifiers.
3. If a suggestion cannot be resolved to any real paper, list it under \
"unresolved" with a reason.
4. If a suggestion supports the claim poorly and you know or find a better \
paper, you may list that paper under "alternatives" (it will be offered to the \
author, not substituted).
{extra_search_rule}
5. Score each reference 0-100 for how directly it supports the claim \
(100 = the paper's own findings state the claim; 50 = related but indirect; \
10 = unrelated). Overall confidence: HIGH if every suggestion scores >= 70, \
MEDIUM if all score >= 40, otherwise LOW.

CRITICAL: When you are ready to give your answer, respond with ONLY a JSON \
object. No other text before or after. No markdown fencing. Just the raw JSON:
{{
  "selected": [
    {{
      "suggestion": "<the suggestion exactly as listed, e.g. 'PMID: 32879322'>",
      "pmid": "12345678",
      "doi": "",
      "title": "",
      "score": 0-100,
      "why": "How this paper supports (or fails to support) the claim"
    }}
  ],
  "unresolved": [
    {{"suggestion": "<suggestion as listed>", "reason": "Why it could not be resolved"}}
  ],
  "alternatives": [
    {{"pmid": "", "doi": "", "title": "", "why": "Why this would be a better citation"}}
  ],
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "confidence_score": 0-100,
  "confidence_rationale": "Why you are this confident",
  "verification_status": "verified" | "partial" | "indirect" | "weak",
  "supporting_snippets": ["Relevant quote from abstract..."],
  "search_queries_used": ["query1"],
  "all_pmids_considered": ["12345678"]
}}
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

BIORXIV_FETCH_TOOL = {
    "name": "fetch_biorxiv_preprint",
    "description": (
        "Fetch full metadata for a specific bioRxiv or medRxiv preprint by its DOI "
        "(e.g. '10.1101/2024.01.15.123456'). Returns title, authors, year, abstract, DOI."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "doi": {
                "type": "string",
                "description": "The DOI of the preprint",
            },
        },
        "required": ["doi"],
    },
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

EUROPEPMC_FETCH_TOOL = {
    "name": "fetch_europepmc_article",
    "description": (
        "Fetch a single article from Europe PMC by DOI or PMID when you have a "
        "specific identifier and need the full metadata."
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
        self.client = anthropic.Anthropic(api_key=anthropic_api_key)
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
            self.tools.append(BIORXIV_FETCH_TOOL)
        if self.search_europepmc:
            self.tools.append(EUROPEPMC_TOOL)
            self.tools.append(EUROPEPMC_FETCH_TOOL)

    def _emit(self, level: str, msg: str):
        getattr(logger, level, logger.info)(msg)
        self._log(level, msg)

    # ── Shared helpers ──────────────────────────────────────────────

    def _make_executor(self, all_candidates: dict[str, CitationCandidate]) -> ToolExecutor:
        return ToolExecutor(
            pubmed=self.pubmed,
            biorxiv=self.biorxiv if self.search_biorxiv else None,
            europepmc=self.europepmc if self.search_europepmc else None,
            user_library=self.reference_library,
            all_candidates=all_candidates,
            status_callback=lambda msg: self._emit("info", f"    {msg}"),
        )

    def _context_notes(self, sentence: SentenceRecord, domain_context: list[str] = None) -> str:
        """Domain / library / ORCID hints appended to the user message."""
        notes = ""
        if domain_context:
            notes += f"\nDocument research domains: {', '.join(domain_context)}"

        if self.reference_library:
            if self.prefer_user_library:
                notes += (
                    "\nA user reference library is available. You should search it first "
                    f"and prefer those papers when relevance is comparable. Use "
                    f"`max_results={self.max_library_results}` when calling search_user_library."
                )
            else:
                notes += (
                    "\nA user reference library is available; search it first, then balance "
                    f"against external literature. Use `max_results={self.max_library_results}` "
                    "when calling search_user_library."
                )

        # Detect self-referencing language (our, we, our lab, etc.)
        if self.orcid_id:
            self_ref_patterns = re.compile(
                r'\b(our|we|our lab|our group|our previous|our prior|our recent|'
                r'our earlier|our extensive|we previously|we have shown|'
                r'we developed|we reported)\b', re.IGNORECASE
            )
            if self_ref_patterns.search(sentence.clean_text):
                notes += (
                    f"\nIMPORTANT: This sentence references the document author's OWN work. "
                    f"The author's ORCID is {self.orcid_id}. "
                    f"Search PubMed using the author's ORCID: {self.orcid_id}[auid] "
                    f"combined with relevant topic keywords. This will help find the "
                    f"correct self-cited paper."
                )
        return notes

    def _run_agent_loop(
        self,
        system: str,
        messages: list[dict],
        executor: ToolExecutor,
        all_candidates: dict[str, CitationCandidate],
        queries_used: list[str],
    ) -> Optional[str]:
        """Run the tool-use loop and return Claude's final text answer.

        Returns None when the round limit was hit and even the forced final
        answer failed.  API errors propagate to the caller.
        """
        for round_num in range(MAX_AGENT_ROUNDS):
            self._emit("info", f"    Agent round {round_num + 1}...")

            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system,
                tools=self.tools,
                messages=messages,
            )

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        tool_result = executor.execute(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": tool_result,
                        })
                        if block.name in ("search_pubmed", "search_europepmc"):
                            queries_used.append(block.input.get("query", ""))

                messages.append({"role": "assistant", "content": response.content})

                # Inject deadline nudge after round 3 to prevent endless searching
                remaining = MAX_AGENT_ROUNDS - round_num - 1
                if round_num >= 3 and remaining <= 3:
                    tool_results.append({
                        "type": "text",
                        "text": (
                            f"[SYSTEM: You have {remaining} round(s) left. "
                            "You MUST return your final JSON answer now. "
                            "Select the best match from what you have found so far, "
                            "even if it is not perfect. Do NOT call any more tools. "
                            "Respond with ONLY the JSON object.]"
                        ),
                    })
                messages.append({"role": "user", "content": tool_results})

            elif response.stop_reason == "end_turn":
                text = "".join(
                    block.text for block in response.content if hasattr(block, "text")
                )
                self._emit("debug", f"    Raw final response ({len(text)} chars): {text[:300]}")
                return text

            else:
                self._emit("warning", f"    Unexpected stop_reason: {response.stop_reason}")
                break

        # Hit max rounds — force one final answer from Claude
        self._emit("warning", f"    Hit max rounds ({MAX_AGENT_ROUNDS}), forcing final answer...")
        try:
            seen_summary = []
            for key, cand in list(all_candidates.items())[:20]:
                ident = f"PMID {cand.pmid}" if cand.pmid else f"DOI {cand.doi}" if cand.doi else key
                seen_summary.append(f"- {ident}: {cand.title} ({cand.year})")
            summary_text = "\n".join(seen_summary) if seen_summary else "(no articles fetched)"

            messages.append({"role": "user", "content": (
                f"DEADLINE REACHED. You must answer NOW.\n\n"
                f"Articles you have seen:\n{summary_text}\n\n"
                f"Select the best match(es) from the articles above and return "
                f"your JSON answer immediately. If none are relevant, return "
                f'an empty "selected" list. Respond with ONLY the JSON object.'
            )})

            final_response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system,
                tools=[],  # No tools — force text-only response
                messages=messages,
            )
            final_text = "".join(
                block.text for block in final_response.content if hasattr(block, "text")
            )
            if final_text.strip():
                self._emit("debug", f"    Forced final response: {final_text[:200]}")
                return final_text
        except Exception as e:
            self._emit("warning", f"    Forced final answer failed: {e}")
        return None

    # ── (REF) / (REFS): search for citations ────────────────────────

    def find_citations(
        self,
        sentence: SentenceRecord,
        domain_context: list[str] = None,
    ) -> EvidenceRecord:
        """Run the agent loop for a single sentence. Returns an EvidenceRecord."""
        marker_type = sentence.marker_type or MarkerType.REF
        # Multi-marker sentences are handled by the orchestrator, which calls
        # find_citations once per marker with marker_count=1.
        if marker_type == MarkerType.REFS:
            num_refs = self.max_refs
        else:
            num_refs = 1

        system = SYSTEM_PROMPT.format(max_refs=num_refs, max_rounds=MAX_AGENT_ROUNDS)

        user_message = (
            f"Find {'1 reference' if num_refs == 1 else f'{num_refs} references'} "
            f"that support this claim:\n\n"
            f"\"{sentence.clean_text}\"{self._context_notes(sentence, domain_context)}"
        )
        messages = [{"role": "user", "content": user_message}]

        all_pmids_seen: dict[str, CitationCandidate] = {}
        queries_used: list[str] = []
        executor = self._make_executor(all_pmids_seen)
        evidence = EvidenceRecord(sentence_id=sentence.id)

        try:
            text = self._run_agent_loop(system, messages, executor, all_pmids_seen, queries_used)
            if text is not None:
                evidence = self._parse_response(text, sentence, all_pmids_seen, queries_used)
                self._emit(
                    "info",
                    f"    Result: {len(evidence.selected)} ref(s), "
                    f"confidence={evidence.confidence_level.value}"
                )
                return evidence

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

    # ── Author-suggested citations: verify and score ────────────────

    def evaluate_suggested(
        self,
        sentence: SentenceRecord,
        marker: MarkerSpec,
        resolved: list[ResolvedSuggestion],
        domain_context: list[str] = None,
        context_text: str = "",
    ) -> EvidenceRecord:
        """Confirm the author's suggested citation(s) for one marker and score them.

        Args:
            sentence: sentence whose ``clean_text`` is the claim to check.
            marker: the suggested marker (its ``suggestions`` drive the prompt).
            resolved: resolver output, one entry per suggestion, same order.
            domain_context: inferred document domains.
            context_text: full sentence when the claim is a sub-clause of it.
        """
        extra_refs = self._extra_search_count(marker)
        extra_rule = ""
        if extra_refs:
            extra_rule = (
                f"4b. The author ALSO asked for {extra_refs} additional supporting "
                f"reference(s) (a (REF) marker inside the same parentheses). Search for "
                f"them as you would for a normal (REF) marker and include each in "
                f'"selected" with "suggestion": "REF".'
            )
        system = SUGGESTED_SYSTEM_PROMPT.format(
            max_rounds=MAX_AGENT_ROUNDS, extra_search_rule=extra_rule,
        )

        # Pre-load the resolved records so the agent's identifiers resolve later.
        all_candidates: dict[str, CitationCandidate] = {}
        for r in resolved:
            for cand in r.candidates:
                key = candidate_key(cand)
                if key and key not in all_candidates:
                    all_candidates[key] = cand

        user_message = self._build_suggested_message(
            sentence, marker, resolved, domain_context, context_text,
        )
        messages = [{"role": "user", "content": user_message}]
        queries_used: list[str] = []
        executor = self._make_executor(all_candidates)

        try:
            text = self._run_agent_loop(system, messages, executor, all_candidates, queries_used)
            if text is None:
                self._emit("warning", "    Agent did not answer; using resolved records directly")
                return self._fallback_from_resolved(
                    sentence, marker, resolved, all_candidates,
                    "AI evaluation did not complete; citation resolved by identifier only",
                )
            try:
                data = extract_json(text)
                if not isinstance(data, dict):
                    raise ValueError("response is not a JSON object")
            except (json.JSONDecodeError, ValueError) as e:
                self._emit("error", f"    Failed to parse agent response: {e}")
                self._emit("debug", f"    Raw response: {text[:500]}")
                return self._fallback_from_resolved(
                    sentence, marker, resolved, all_candidates,
                    f"AI response could not be parsed ({e}); citation resolved by identifier only",
                )
            evidence = self._evidence_from_suggested_data(
                data, sentence, marker, resolved, all_candidates, queries_used,
            )
            self._emit(
                "info",
                f"    Result: {len(evidence.selected)} ref(s) for {marker.text}, "
                f"confidence={evidence.confidence_level.value}"
            )
            return evidence

        except anthropic.AuthenticationError as e:
            raise RuntimeError(
                f"Anthropic API authentication failed. Check your API key. ({e})"
            ) from e
        except anthropic.APIError as e:
            self._emit("error", f"    Anthropic API error: {e}")
            return self._fallback_from_resolved(
                sentence, marker, resolved, all_candidates, f"API error: {e}",
            )
        except Exception as e:
            self._emit("error", f"    Agent error: {e}")
            return self._fallback_from_resolved(
                sentence, marker, resolved, all_candidates, f"Agent error: {e}",
            )

    def _extra_search_count(self, marker: MarkerSpec) -> int:
        if marker.extra_search < 0:
            return self.max_refs
        return marker.extra_search

    def _build_suggested_message(
        self,
        sentence: SentenceRecord,
        marker: MarkerSpec,
        resolved: list[ResolvedSuggestion],
        domain_context: list[str],
        context_text: str,
    ) -> str:
        lines = [f'Claim: "{sentence.clean_text}"']
        if context_text and context_text.strip() != sentence.clean_text.strip():
            lines.append(f'Full sentence for context: "{context_text}"')
        notes = self._context_notes(sentence, domain_context)
        if notes:
            lines.append(notes.strip())
        lines.append("")
        lines.append(f"The author wrote the marker {marker.text} here. Suggested references:")
        lines.append("")

        for i, r in enumerate(resolved, 1):
            s = r.suggestion
            lines.append(f"[{i}] Suggestion: {s.raw}   (kind: {s.kind.value}, label: {s.label})")
            if not r.candidates:
                lines.append(f"    NOT FOUND: {r.error or 'no matching record'}")
                if s.kind == SuggestionKind.AUTHOR_YEAR:
                    lines.append(
                        f"    You may search for it: first author '{s.author}', year {s.year}"
                        + (f", co-author '{s.coauthor}'" if s.coauthor else "")
                        + ". If nothing plausible exists, list it under \"unresolved\"."
                    )
                else:
                    lines.append(
                        "    You may try one lookup with the tools; otherwise list it under "
                        "\"unresolved\"."
                    )
            elif s.kind == SuggestionKind.AUTHOR_YEAR and (len(r.candidates) > 1 or r.total_matches > 1):
                lines.append(
                    f"    AMBIGUOUS: {r.total_matches} paper(s) by first author "
                    f"'{s.author}' in {s.year}. Records shown: {len(r.candidates)}. "
                    "Choose the one whose topic matches the claim; search with topic "
                    "keywords if none of these fits."
                )
                for cand in r.candidates:
                    lines.append("    - " + json.dumps(_article_summary(cand), ensure_ascii=False))
            else:
                cand = r.candidates[0]
                lines.append(f"    Resolved via {r.source or 'lookup'}: "
                             + json.dumps(_article_summary(cand, include_mesh=True), ensure_ascii=False))
                if cand.is_retracted:
                    lines.append("    WARNING: this record is flagged as RETRACTED.")
            lines.append("")

        extra = self._extra_search_count(marker)
        if extra:
            lines.append(
                f"The author also asked for {extra} additional supporting reference(s); "
                f"search for them and include them with \"suggestion\": \"REF\"."
            )
        lines.append("Evaluate the suggested reference(s) against the claim and answer with the JSON object.")
        return "\n".join(lines)

    # ── Response parsing ────────────────────────────────────────────

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
            if not isinstance(data, dict):
                raise ValueError("response is not a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            self._emit("error", f"    Failed to parse agent response: {e}")
            self._emit("debug", f"    Raw response: {text[:500]}")
            evidence.retrieval_error = f"Failed to parse agent response: {e}"
            evidence.confidence_level = ConfidenceLevel.UNRESOLVED
            evidence.candidates = list(all_candidates.values())
            evidence.search_query = " | ".join(queries_used)
            return evidence

        # Selected citations — support both PMID and DOI keys
        raw_selected = data.get("selected") or []
        if not isinstance(raw_selected, list):
            raw_selected = []
        evidence.selected = [
            cand for cand in (
                self._lookup_selection(sel, all_candidates) for sel in raw_selected
            ) if cand is not None
        ]
        self._apply_common_fields(evidence, data, all_candidates, queries_used)
        return evidence

    def _lookup_selection(
        self, sel: dict, all_candidates: dict[str, CitationCandidate],
    ) -> Optional[CitationCandidate]:
        """Find the candidate an agent 'selected' entry refers to, fetching by PMID if needed."""
        if not isinstance(sel, dict):
            return None
        pmid = str(sel.get("pmid", "") or "").strip()
        doi = str(sel.get("doi", "") or "").strip()
        title = str(sel.get("title", "") or "").strip()

        by_pmid = {c.pmid: c for c in all_candidates.values() if c.pmid}
        by_doi = {c.doi.lower(): c for c in all_candidates.values() if c.doi}
        by_title = {c.title.strip().lower(): c for c in all_candidates.values() if c.title}

        if pmid and pmid in by_pmid:
            return by_pmid[pmid]
        if doi and doi.lower() in by_doi:
            return by_doi[doi.lower()]
        if title and title.lower() in by_title:
            return by_title[title.lower()]
        if pmid and pmid.isdigit():
            fetched = self.pubmed.fetch_articles([pmid])
            for a in fetched:
                all_candidates[a.pmid] = a
                return a
        if pmid and pmid in all_candidates:
            return all_candidates[pmid]
        if doi and doi in all_candidates:
            return all_candidates[doi]
        if title and title in all_candidates:
            return all_candidates[title]
        return None

    def _apply_common_fields(
        self,
        evidence: EvidenceRecord,
        data: dict,
        all_candidates: dict[str, CitationCandidate],
        queries_used: list[str],
    ):
        """Confidence, verification, snippets, queries and candidate list."""
        considered_ids = data.get("all_pmids_considered") or []
        if not isinstance(considered_ids, list):
            considered_ids = []
        for p in considered_ids:
            p = str(p)
            if p.isdigit() and p not in all_candidates:
                fetched = self.pubmed.fetch_articles([p])
                for a in fetched:
                    all_candidates[a.pmid] = a
        evidence.candidates = list(all_candidates.values())

        conf_str = str(data.get("confidence", "LOW")).upper()
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
        evidence.confidence_rationale = str(data.get("confidence_rationale", "") or "")

        ver_str = str(data.get("verification_status", "not_checked")).lower()
        ver_map = {
            "verified": VerificationStatus.VERIFIED,
            "partial": VerificationStatus.PARTIAL,
            "indirect": VerificationStatus.INDIRECT,
            "weak": VerificationStatus.WEAK,
        }
        evidence.verification_status = ver_map.get(ver_str, VerificationStatus.NOT_CHECKED)

        snippets = data.get("supporting_snippets") or []
        if not isinstance(snippets, list):
            snippets = [snippets]
        evidence.abstract_snippets = [str(s) for s in snippets if s]
        queries = data.get("search_queries_used") or queries_used
        if not isinstance(queries, list):
            queries = [queries]
        evidence.search_query = " | ".join(str(q) for q in queries if q)
        evidence.search_result_count = len(all_candidates)

    def _evidence_from_suggested_data(
        self,
        data: dict,
        sentence: SentenceRecord,
        marker: MarkerSpec,
        resolved: list[ResolvedSuggestion],
        all_candidates: dict[str, CitationCandidate],
        queries_used: list[str],
    ) -> EvidenceRecord:
        """Turn the agent's JSON for a suggested marker into an EvidenceRecord.

        The author's suggestions define the slots: one selected citation per
        suggestion (in marker order), followed by any extra (REF) searches.
        Identifier suggestions are pinned to the record they resolved to, so
        the agent cannot silently substitute a different paper.
        """
        evidence = EvidenceRecord(sentence_id=sentence.id)
        raw_selected = data.get("selected") or []
        entries = [e for e in raw_selected if isinstance(e, dict)] if isinstance(raw_selected, list) else []

        # Resolve every entry to a candidate up front
        resolved_entries: list[tuple[dict, Optional[CitationCandidate]]] = [
            (e, self._lookup_selection(e, all_candidates)) for e in entries
        ]
        used: set[int] = set()
        selected: list[CitationCandidate] = []
        raw_unresolved = data.get("unresolved") or []
        unresolved_reasons = {
            str(u.get("suggestion", "")): str(u.get("reason", ""))
            for u in (raw_unresolved if isinstance(raw_unresolved, list) else [])
            if isinstance(u, dict)
        }

        for idx, r in enumerate(resolved):
            s = r.suggestion
            entry_idx = self._match_entry(idx, r, resolved_entries, used)
            entry, cand = (resolved_entries[entry_idx] if entry_idx is not None else (None, None))

            pinned = self._pinned_candidate(r)
            if pinned is not None:
                # Identifier suggestion: always show the record it resolved to
                if cand is None or not self._same_paper(cand, pinned):
                    if cand is not None:
                        self._emit(
                            "warning",
                            f"    Agent answered with a different paper for {s.label}; "
                            f"keeping the author's suggestion",
                        )
                        entry = None
                    cand = pinned
            elif cand is not None and s.kind != SuggestionKind.AUTHOR_YEAR \
                    and not self._identifier_matches(cand, s):
                # The resolver found nothing for this identifier and the agent
                # answered with some other paper: never cite it as the author's
                # suggestion; offer it as an alternative instead.
                self._emit(
                    "warning",
                    f"    Agent answered {s.label} with a paper that has a different "
                    f"identifier; treating the suggestion as unresolved",
                )
                evidence.warnings.append(Warning(
                    code="suggested_alternative",
                    message=(
                        f"{s.label} could not be resolved; the AI proposes "
                        f"\"{cand.title[:70]}\" ({self._ident(cand)}) instead."
                    ),
                    severity="low",
                ))
                entry, cand = None, None
            elif cand is not None and s.kind == SuggestionKind.AUTHOR_YEAR \
                    and not self._author_year_matches(cand, s):
                self._emit(
                    "warning",
                    f"    Agent picked a paper that does not match {s.label}; ignoring it",
                )
                entry, cand = None, None

            if cand is None:
                reason = unresolved_reasons.get(s.raw) or unresolved_reasons.get(s.label) \
                    or r.error or "not found"
                evidence.warnings.append(Warning(
                    code="suggested_unresolved",
                    message=f"Could not resolve the suggested citation {s.label}: {reason}",
                    severity="high",
                ))
                continue

            if entry_idx is not None:
                used.add(entry_idx)

            # Two suggestions in one marker naming the same paper (e.g. its PMID
            # and its DOI) are cited once.
            dup = next((c for c in selected if self._same_paper(c, cand)), None)
            if dup is not None:
                evidence.warnings.append(Warning(
                    code="suggested_duplicate",
                    message=f"{s.label} refers to the same paper as an earlier suggestion in "
                            f"{marker.text}; it is cited once.",
                    severity="low",
                ))
                continue

            cand = cand.model_copy()
            score = self._entry_score(entry)
            why = str(entry.get("why", "") or "").strip() if entry else ""
            if score is None:
                cand.composite_score = 0.0
                cand.score_rationale = (
                    f"Author-suggested ({s.label}): resolved, but the AI did not score it"
                )
                evidence.warnings.append(Warning(
                    code="suggested_unscored",
                    message=f"{s.label} was resolved but not scored by the AI; review it manually",
                    severity="low",
                ))
            else:
                cand.composite_score = score
                cand.score_rationale = f"Author-suggested ({s.label}): {why or 'no explanation given'}"
                if score < WEAK_MATCH_THRESHOLD:
                    evidence.warnings.append(Warning(
                        code="suggested_weak_match",
                        message=(
                            f"The suggested citation {s.label} may not support this claim "
                            f"(AI score {score:.0f}/100): {why or 'no explanation given'}"
                        ),
                        severity="medium",
                    ))
            if r.ambiguous:
                evidence.warnings.append(Warning(
                    code="suggested_ambiguous",
                    message=(
                        f"{s.label} matched {max(r.total_matches, len(r.candidates))} papers; "
                        f"the AI chose \"{cand.title[:70]}\". Confirm it is the one you meant."
                    ),
                    severity="medium",
                ))
            if cand.is_retracted:
                evidence.warnings.append(Warning(
                    code="retracted",
                    message=f"{s.label} resolves to a RETRACTED article: {cand.title[:70]}",
                    severity="high",
                ))
            selected.append(cand)

        # Extra (REF) searches requested inside the same marker
        extra = self._extra_search_count(marker)
        if extra:
            leftover = [
                (i, e, c) for i, (e, c) in enumerate(resolved_entries)
                if i not in used and c is not None
            ]
            chosen_keys = {candidate_key(c) for c in selected}
            cap = len(selected) + extra
            for i, e, c in leftover:
                if len(selected) >= cap:
                    break
                if candidate_key(c) in chosen_keys:
                    continue
                c = c.model_copy()
                score = self._entry_score(e)
                c.composite_score = score if score is not None else 0.0
                c.score_rationale = str(e.get("why", "") or "AI-selected additional reference")
                selected.append(c)
                chosen_keys.add(candidate_key(c))
                used.add(i)

        evidence.selected = selected
        evidence.slot_sizes = [len(selected)]

        # Alternatives the agent proposed (offered, never substituted)
        raw_alternatives = data.get("alternatives") or []
        for alt in (raw_alternatives if isinstance(raw_alternatives, list) else []):
            if not isinstance(alt, dict):
                continue
            cand = self._lookup_selection(alt, all_candidates)
            if cand is None:
                continue
            key = candidate_key(cand)
            if key and key not in all_candidates:
                all_candidates[key] = cand
            ident = f"PMID {cand.pmid}" if cand.pmid else (f"doi:{cand.doi}" if cand.doi else "")
            evidence.warnings.append(Warning(
                code="suggested_alternative",
                message=(
                    f"The AI suggests an alternative citation: \"{cand.title[:70]}\" "
                    f"{ident}. {str(alt.get('why', '') or '').strip()}"
                ).strip(),
                severity="low",
            ))

        self._apply_common_fields(evidence, data, all_candidates, queries_used)

        if not selected:
            evidence.confidence_level = ConfidenceLevel.LOW
            evidence.retrieval_error = (
                "None of the author-suggested citations could be resolved"
                if resolved else "No citation could be confirmed for this marker"
            )
        elif evidence.confidence_level == ConfidenceLevel.HIGH and any(
            w.code in ("suggested_unresolved", "suggested_ambiguous", "suggested_unscored")
            for w in evidence.warnings
        ):
            # An unresolved, ambiguous, or unscored suggestion is never HIGH confidence
            evidence.confidence_level = ConfidenceLevel.MEDIUM
        return evidence

    # ── Suggested-citation helpers ──────────────────────────────────

    @staticmethod
    def _unresolved_count(evidence: EvidenceRecord) -> int:
        return sum(1 for w in evidence.warnings if w.code == "suggested_unresolved")

    @staticmethod
    def _entry_score(entry: Optional[dict]) -> Optional[float]:
        if not entry:
            return None
        raw = entry.get("score", None)
        if raw is None or raw == "":
            return None
        try:
            return max(0.0, min(100.0, float(raw)))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _pinned_candidate(r: ResolvedSuggestion) -> Optional[CitationCandidate]:
        """The record an identifier suggestion must map to (None for author-year)."""
        if r.suggestion.kind == SuggestionKind.AUTHOR_YEAR:
            return None
        return r.candidates[0] if r.candidates else None

    @staticmethod
    def _same_paper(a: CitationCandidate, b: CitationCandidate) -> bool:
        if a.pmid and b.pmid:
            return a.pmid == b.pmid
        if a.doi and b.doi:
            return a.doi.strip().lower() == b.doi.strip().lower()
        if a.pmcid and b.pmcid:
            return a.pmcid.strip().upper() == b.pmcid.strip().upper()
        return bool(a.title) and a.title.strip().lower() == b.title.strip().lower()

    @staticmethod
    def _ident(cand: CitationCandidate) -> str:
        if cand.pmid:
            return f"PMID {cand.pmid}"
        if cand.doi:
            return f"doi:{cand.doi}"
        if cand.pmcid:
            return cand.pmcid
        return "no identifier"

    @staticmethod
    def _identifier_matches(cand: CitationCandidate, s: SuggestedCitation) -> bool:
        """Does *cand* carry exactly the identifier the author wrote?"""
        if s.kind == SuggestionKind.PMID:
            return bool(cand.pmid) and cand.pmid.strip() == s.value
        if s.kind == SuggestionKind.PMCID:
            return bool(cand.pmcid) and cand.pmcid.strip().upper() == s.value.upper()
        if s.kind == SuggestionKind.DOI:
            norm = lambda d: re.sub(r"^https?://(?:dx\.)?doi\.org/", "", (d or "").strip(), flags=re.I).lower().rstrip(".")
            return bool(cand.doi) and norm(cand.doi) == norm(s.value)
        return False

    @staticmethod
    def _surname_matches(last_name: str, target: str) -> bool:
        """Compare surnames by their final token so particles and initials do not matter."""
        a = (last_name or "").strip().lower()
        b = (target or "").strip().lower()
        if not a or not b:
            return False
        return a == b or a.split()[-1] == b.split()[-1]

    @classmethod
    def _author_year_matches(cls, cand: CitationCandidate, s: SuggestedCitation) -> bool:
        """Sanity check for author-year picks: author(s) present and year within +-1."""
        if s.year and cand.year and abs(cand.year - s.year) > 1:
            return False
        if not cand.authors:
            return True  # no author data: cannot refute
        names = [a.last_name for a in cand.authors if a.last_name]
        if s.author and not any(cls._surname_matches(n, s.author) for n in names):
            return False
        if s.coauthor and not any(cls._surname_matches(n, s.coauthor) for n in names):
            return False
        return True

    def _match_entry(
        self,
        idx: int,
        r: ResolvedSuggestion,
        entries: list[tuple[dict, Optional[CitationCandidate]]],
        used: set[int],
    ) -> Optional[int]:
        """Find the agent 'selected' entry that answers suggestion *idx*."""
        s = r.suggestion

        def _norm(text: str) -> str:
            return re.sub(r"\s+", " ", str(text or "")).strip().lower()

        wanted = {_norm(s.raw), _norm(s.label), f"[{idx + 1}]"}

        def _tag_matches(entry: dict) -> bool:
            tag = _norm(entry.get("suggestion", ""))
            return bool(tag) and tag in wanted

        # 1) the exact "suggestion" text the agent echoed back (exact match only:
        #    "PMID: 3287932" must never claim the entry for "PMID: 32879322")
        for i, (entry, cand) in enumerate(entries):
            if i not in used and _tag_matches(entry):
                return i
        # 2) identity: the entry's record is one of the resolved candidates
        for i, (entry, cand) in enumerate(entries):
            if i in used or cand is None:
                continue
            if any(self._same_paper(cand, c) for c in r.candidates):
                return i
        # 3) author-year: an entry whose record matches author + year
        if s.kind == SuggestionKind.AUTHOR_YEAR:
            for i, (entry, cand) in enumerate(entries):
                if i in used or cand is None:
                    continue
                tag = str(entry.get("suggestion", "") or "").strip().upper()
                if tag == "REF":
                    continue
                if self._author_year_matches(cand, s):
                    return i
        return None

    def _fallback_from_resolved(
        self,
        sentence: SentenceRecord,
        marker: MarkerSpec,
        resolved: list[ResolvedSuggestion],
        all_candidates: dict[str, CitationCandidate],
        reason: str,
    ) -> EvidenceRecord:
        """Evidence built without the agent: unambiguous resolutions are selected as-is."""
        evidence = EvidenceRecord(sentence_id=sentence.id)
        for r in resolved:
            s = r.suggestion
            if len(r.candidates) == 1 and not r.ambiguous:
                cand = r.candidates[0].model_copy()
                cand.composite_score = 0.0
                cand.score_rationale = f"Author-suggested ({s.label}): {reason}"
                evidence.selected.append(cand)
                evidence.warnings.append(Warning(
                    code="suggested_unscored",
                    message=f"{s.label} was resolved but not scored: {reason}",
                    severity="medium",
                ))
            elif r.candidates:
                evidence.warnings.append(Warning(
                    code="suggested_unresolved",
                    message=(
                        f"{s.label} matched {max(r.total_matches, len(r.candidates))} papers "
                        f"and the AI could not choose ({reason}); pick one with Modify / Search"
                    ),
                    severity="high",
                ))
            else:
                evidence.warnings.append(Warning(
                    code="suggested_unresolved",
                    message=f"Could not resolve the suggested citation {s.label}: {r.error or reason}",
                    severity="high",
                ))
        evidence.slot_sizes = [len(evidence.selected)]
        evidence.candidates = list(all_candidates.values())
        evidence.search_result_count = len(all_candidates)
        evidence.confidence_level = ConfidenceLevel.UNRESOLVED
        evidence.confidence_rationale = reason
        if not evidence.selected:
            evidence.retrieval_error = reason
        return evidence
