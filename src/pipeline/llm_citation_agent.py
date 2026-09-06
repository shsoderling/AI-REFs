"""LLM-powered citation agent using Claude tool-use.

One Claude agent loop per claim: it searches the user's library, PubMed,
Europe PMC and bioRxiv, reads abstracts (and open-access full text when
needed) and hands back its selection through the ``submit_citations``
tool.  Independent verification of the selection happens afterwards in
``pipeline.verification``.
"""

import logging
import re
from typing import Optional, Callable

import anthropic

from ..models.sentence import SentenceRecord, MarkerType
from ..models.citation import CitationCandidate
from ..models.evidence import (
    EvidenceRecord, ConfidenceLevel, VerificationStatus,
)
from ..services.claude_client import ClaudeCaller, make_client
from ..services.pubmed_client import PubMedClient
from ..services.biorxiv_client import BioRxivClient
from ..services.europepmc_client import EuropePMCClient
from ..services.ref_library import ReferenceLibrary
from ..services.search_tools import SUBMIT_CITATIONS, build_tool_list, find_tool_use
from ..services.tool_executor import ToolExecutor

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
call submit_citations. If not, do ONE more targeted search.
Step 4 (round 4 max): Fetch any new articles, then STOP and call submit_citations.

DO NOT keep searching endlessly. After 2-3 searches and 1-2 fetches, you should \
have enough information to make a selection. Select the best available match even \
if it is not perfect.

For a (REF) marker, select exactly 1 best reference. For a (REFS) marker, \
select up to {max_refs} references.

{preferences}

Never select a retracted article. If an abstract is ambiguous and the article \
reports full_text_available, call get_fulltext_passages with a few keywords \
before deciding. Cite only the sentence marked as the CLAIM; surrounding text \
is context to help you understand it.

If the tool `search_user_library` is available, call it in round 1 before
external searches and strongly prefer those results when they support the claim.

When you are ready to answer, call the `submit_citations` tool exactly once. \
Do not write the answer as text. If no supporting references exist, call \
submit_citations with an empty "selected" list and confidence "LOW".
"""


def build_preference_text(prefer_reviews: bool, recency_bias: bool) -> str:
    """Prompt sentences for the Input tab's review/recency preferences."""
    if prefer_reviews:
        reviews = ("Prefer an authoritative review when one directly covers the claim; "
                   "otherwise choose peer-reviewed primary research.")
    else:
        reviews = "Prefer peer-reviewed primary research over reviews."
    if recency_bias:
        recency = "Prefer recent publications when relevance is equal."
    else:
        recency = ("Do not weigh publication date; choose the most relevant, "
                   "well-established source.")
    return f"{reviews} {recency}"


_SELF_REFERENCE = re.compile(
    r'\b(our|we|our lab|our group|our previous|our prior|our recent|'
    r'our earlier|our extensive|we previously|we have shown|'
    r'we developed|we reported)\b', re.IGNORECASE
)


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
        client=None,
        prefer_reviews: bool = False,
        recency_bias: bool = True,
        use_full_text: bool = True,
    ):
        if client is None:
            client = make_client(anthropic_api_key)
        self.client = client
        self.model = model
        self.caller = ClaudeCaller(client, model, log=log_callback)
        self.pubmed = pubmed_client
        self.biorxiv = biorxiv_client
        self.europepmc = europepmc_client
        self.reference_library = reference_library
        self.search_biorxiv = search_biorxiv and biorxiv_client is not None
        self.search_europepmc = search_europepmc and europepmc_client is not None
        self.use_full_text = use_full_text and self.search_europepmc
        self.prefer_user_library = prefer_user_library
        self.max_library_results = max(3, min(max_library_results, 50))
        self.max_refs = max_refs
        self.orcid_id = orcid_id
        self.preferences = build_preference_text(prefer_reviews, recency_bias)
        self._log = log_callback or (lambda *a: None)

        self.tools = build_tool_list(
            user_library=self.reference_library is not None,
            biorxiv=self.search_biorxiv,
            europepmc=self.search_europepmc,
            fulltext=self.use_full_text,
            final_tool=SUBMIT_CITATIONS,
        )

    def _emit(self, level: str, msg: str):
        getattr(logger, level, logger.info)(msg)
        self._log(level, msg)

    # ── prompt assembly ──────────────────────────────────────────────

    def _extra_instructions(self, sentence: SentenceRecord, domain_context: list[str] = None) -> str:
        parts = []
        if domain_context:
            parts.append(f"Document research domains: {', '.join(domain_context)}")

        if self.reference_library:
            if self.prefer_user_library:
                parts.append(
                    "A user reference library is available. You should search it first "
                    f"and prefer those papers when relevance is comparable. Use "
                    f"`max_results={self.max_library_results}` when calling search_user_library."
                )
            else:
                parts.append(
                    "A user reference library is available; search it first, then balance "
                    f"against external literature. Use `max_results={self.max_library_results}` "
                    "when calling search_user_library."
                )

        # Detect self-referencing language (our, we, our lab, etc.)
        if self.orcid_id and _SELF_REFERENCE.search(sentence.clean_text):
            parts.append(
                f"IMPORTANT: This sentence references the document author's OWN work. "
                f"The author's ORCID is {self.orcid_id}. "
                f"Search PubMed using the author's ORCID: {self.orcid_id}[auid] "
                f"combined with relevant topic keywords. This will help find the "
                f"correct self-cited paper."
            )
        return "\n".join(parts)

    def _user_message(self, sentence: SentenceRecord, num_refs: int,
                      domain_context: list[str] = None) -> str:
        extras = self._extra_instructions(sentence, domain_context)
        text = (
            f"Find {'1 reference' if num_refs == 1 else f'{num_refs} references'} "
            f"that support this claim:\n\n"
            f"\"{sentence.clean_text}\""
        )
        if extras:
            text += "\n\n" + extras
        return text

    # ── agent loop ───────────────────────────────────────────────────

    def find_citations(
        self,
        sentence: SentenceRecord,
        domain_context: list[str] = None,
    ) -> EvidenceRecord:
        """Run the agent loop for a single sentence. Returns an EvidenceRecord."""
        marker_type = sentence.marker_type or MarkerType.REF
        # Multi-(REF) sentences are handled by the orchestrator, which calls
        # find_citations once per marker with marker_count=1.
        num_refs = self.max_refs if marker_type == MarkerType.REFS else 1

        system = SYSTEM_PROMPT.format(max_refs=num_refs, max_rounds=MAX_AGENT_ROUNDS,
                                      preferences=self.preferences)
        messages = [{"role": "user", "content": self._user_message(sentence, num_refs, domain_context)}]

        # Track all candidates seen across rounds
        all_pmids_seen: dict[str, CitationCandidate] = {}
        queries_used: list[str] = []

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
                response = self.caller.create(system=system, messages=messages, tools=self.tools)

                submit = find_tool_use(response, SUBMIT_CITATIONS["name"])
                if submit is not None:
                    return self._finish(submit.input, sentence, all_pmids_seen, queries_used)

                if response.stop_reason == "tool_use":
                    tool_results = []
                    for block in response.content:
                        if block.type != "tool_use":
                            continue
                        result = executor.execute(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                        if block.name == "search_pubmed":
                            queries_used.append(block.input.get("query", ""))
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": tool_results})

                elif response.stop_reason == "end_turn":
                    # Prose instead of the tool: ask for the tool call.
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": (
                        "Call submit_citations now with your selection. "
                        "Do not answer in text."
                    )})
                else:
                    self._emit("warning", f"    Unexpected stop_reason: {response.stop_reason}")
                    break

            # Deadline: one more call where the only tool is submit_citations.
            self._emit("warning", f"    Hit max rounds ({MAX_AGENT_ROUNDS}), forcing final answer...")
            seen_summary = [
                f"- PMID {pmid}: {cand.title} ({cand.year})"
                for pmid, cand in list(all_pmids_seen.items())[:20]
            ]
            summary_text = "\n".join(seen_summary) if seen_summary else "(no articles fetched)"
            messages.append({"role": "user", "content": (
                f"DEADLINE REACHED. You must answer NOW by calling submit_citations.\n\n"
                f"Articles you have seen:\n{summary_text}\n\n"
                f"Select the best match(es) from the articles above. If none are "
                f"relevant, submit an empty \"selected\" list."
            )})
            response = self.caller.create(
                system=system, messages=messages,
                tools=build_tool_list(final_tool=SUBMIT_CITATIONS)[-1:],
            )
            submit = find_tool_use(response, SUBMIT_CITATIONS["name"])
            if submit is not None:
                evidence = self._finish(submit.input, sentence, all_pmids_seen, queries_used)
                self._emit("info", f"    Forced result: {len(evidence.selected)} ref(s)")
                return evidence

            # The model still did not submit: record what we have.
            evidence.candidates = list(all_pmids_seen.values())
            evidence.search_query = " | ".join(queries_used)
            evidence.confidence_level = ConfidenceLevel.LOW
            evidence.confidence_rationale = (
                f"Agent hit {MAX_AGENT_ROUNDS}-round limit without submitting a selection"
            )
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

    def _finish(self, data: dict, sentence: SentenceRecord,
                all_candidates: dict[str, CitationCandidate], queries_used: list[str]) -> EvidenceRecord:
        evidence = self._parse_submission(data, sentence, all_candidates, queries_used)
        self._emit(
            "info",
            f"    Result: {len(evidence.selected)} ref(s), "
            f"confidence={evidence.confidence_level.value}"
        )
        return evidence

    def _parse_submission(
        self,
        data: dict,
        sentence: SentenceRecord,
        all_candidates: dict[str, CitationCandidate],
        queries_used: list[str],
    ) -> EvidenceRecord:
        """Turn the submit_citations tool input into an EvidenceRecord."""
        evidence = EvidenceRecord(sentence_id=sentence.id)
        data = data or {}

        # Selected citations — support both PMID and DOI keys
        selected_keys = []
        for sel in data.get("selected", []) or []:
            if not isinstance(sel, dict):
                continue
            pmid = str(sel.get("pmid", "") or "").strip()
            doi = str(sel.get("doi", "") or "").strip()
            title = str(sel.get("title", "") or "").strip()
            if pmid:
                selected_keys.append(pmid)
            elif doi:
                selected_keys.append(doi)
            elif title:
                selected_keys.append(title)

        # Fetch any selected PMIDs we haven't seen yet (DOIs are already tracked)
        missing_pmids = [k for k in selected_keys if k not in all_candidates and k.isdigit()]
        if missing_pmids:
            for a in self.pubmed.fetch_articles(missing_pmids):
                all_candidates[a.pmid] = a

        evidence.selected = [all_candidates[k] for k in selected_keys if k in all_candidates]

        # All candidates considered (PMIDs and DOIs)
        for p in data.get("all_pmids_considered", []) or []:
            p = str(p)
            if p not in all_candidates and p.isdigit():
                for a in self.pubmed.fetch_articles([p]):
                    all_candidates[a.pmid] = a
        evidence.candidates = list(all_candidates.values())

        # Confidence
        conf_map = {
            "HIGH": ConfidenceLevel.HIGH,
            "MEDIUM": ConfidenceLevel.MEDIUM,
            "LOW": ConfidenceLevel.LOW,
        }
        evidence.confidence_level = conf_map.get(str(data.get("confidence", "LOW")).upper(),
                                                 ConfidenceLevel.LOW)
        try:
            evidence.confidence_score = float(data.get("confidence_score", 0))
        except (TypeError, ValueError):
            evidence.confidence_score = 0.0
        evidence.confidence_rationale = str(data.get("confidence_rationale", "") or "")

        # Verification (the agent's own view; the verifier may override it)
        ver_map = {
            "verified": VerificationStatus.VERIFIED,
            "partial": VerificationStatus.PARTIAL,
            "indirect": VerificationStatus.INDIRECT,
            "weak": VerificationStatus.WEAK,
        }
        evidence.verification_status = ver_map.get(
            str(data.get("verification_status", "not_checked")).lower(),
            VerificationStatus.NOT_CHECKED,
        )

        # Snippets and queries
        evidence.abstract_snippets = [str(s) for s in data.get("supporting_snippets", []) or []]
        evidence.search_query = " | ".join(
            [str(q) for q in data.get("search_queries_used", []) or []] or queries_used
        )
        evidence.search_result_count = len(all_candidates)
        return evidence
