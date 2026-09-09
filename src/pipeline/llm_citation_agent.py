"""LLM-powered citation agent using Claude tool-use.

One Claude agent loop per claim: it searches the user's library, PubMed,
Europe PMC and bioRxiv, reads abstracts (and open-access full text when
needed) and hands back its selection through the ``submit_citations``
tool.  Independent verification of the selection happens afterwards in
``pipeline.verification``.

Two entry points:

* :meth:`LLMCitationAgent.find_citations` -- search for references that
  support a claim (``(REF)`` / ``(REFS)`` markers).
* :meth:`LLMCitationAgent.evaluate_suggested` -- the author already named
  the reference(s) (``(PMID: ...)``, ``(Battison et al. 2024)``, ...); the
  agent confirms each one and scores how well it supports the claim,
  answering through the ``submit_suggested_evaluation`` tool.
"""

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
from ..services.claude_client import ClaudeCaller, make_client
from ..services.pubmed_client import PubMedClient
from ..services.biorxiv_client import BioRxivClient
from ..services.europepmc_client import EuropePMCClient
from ..services.ref_library import ReferenceLibrary
from ..services.search_tools import (
    SUBMIT_CITATIONS, SUBMIT_SUGGESTED_EVALUATION, build_tool_list, find_tool_use,
)
from ..services.suggestion_resolver import ResolvedSuggestion
from ..services.tool_executor import ToolExecutor, candidate_key, _article_summary
from .claim_context import ClaimContext, bare_context

logger = logging.getLogger(__name__)

MAX_AGENT_ROUNDS = 6

# Below this per-citation score an author-suggested citation gets a warning.
WEAK_MATCH_THRESHOLD = 50

# Warnings that keep a suggested-citation sentence from being HIGH confidence.
_NOT_HIGH_WARNINGS = ("suggested_unresolved", "suggested_ambiguous", "suggested_unscored")

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
- a provided record lacks an abstract and you need it to judge support (call \
get_fulltext_passages when the record reports full_text_available).

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
MEDIUM if all score >= 40, otherwise LOW. Cite only the sentence marked as the \
CLAIM; surrounding text is context to help you understand it.

{preferences}

When you are ready to answer, call the `submit_suggested_evaluation` tool \
exactly once. Do not write the answer as text.
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


def cap_suggested_confidence(evidence: EvidenceRecord) -> None:
    """An unresolved, ambiguous, or unscored author suggestion is never HIGH.

    Applied when the evaluation is built and again after the independent
    verifier (which may raise the level on the papers it could check).
    """
    if evidence.confidence_level == ConfidenceLevel.HIGH and any(
            w.code in _NOT_HIGH_WARNINGS for w in evidence.warnings):
        evidence.confidence_level = ConfidenceLevel.MEDIUM


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

        self.tools = self._tool_list(SUBMIT_CITATIONS)
        self.suggested_tools = self._tool_list(SUBMIT_SUGGESTED_EVALUATION)

    def _tool_list(self, final_tool: dict) -> list[dict]:
        return build_tool_list(
            user_library=self.reference_library is not None,
            biorxiv=self.search_biorxiv,
            europepmc=self.search_europepmc,
            fulltext=self.use_full_text,
            final_tool=final_tool,
        )

    def _emit(self, level: str, msg: str):
        getattr(logger, level, logger.info)(msg)
        self._log(level, msg)

    def _make_executor(self, all_candidates: dict[str, CitationCandidate]) -> ToolExecutor:
        return ToolExecutor(
            pubmed=self.pubmed,
            biorxiv=self.biorxiv if self.search_biorxiv else None,
            europepmc=self.europepmc if self.search_europepmc else None,
            user_library=self.reference_library,
            all_candidates=all_candidates,
            status_callback=lambda msg: self._emit("info", f"    {msg}"),
        )

    # ── prompt assembly ──────────────────────────────────────────────

    def _extra_instructions(self, context: ClaimContext, domain_context: list[str] = None) -> str:
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
        if self.orcid_id and _SELF_REFERENCE.search(f"{context.claim} {context.paragraph}"):
            parts.append(
                f"IMPORTANT: This sentence references the document author's OWN work. "
                f"The author's ORCID is {self.orcid_id}. "
                f"Search PubMed using the author's ORCID: {self.orcid_id}[auid] "
                f"combined with relevant topic keywords. This will help find the "
                f"correct self-cited paper."
            )
        return "\n".join(parts)

    def _user_message(self, context: ClaimContext, num_refs: int,
                      domain_context: list[str] = None) -> str:
        return context.to_agent_message(num_refs, self._extra_instructions(context, domain_context))

    # ── agent loop ───────────────────────────────────────────────────

    def _run_loop(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        final_tool: dict,
        executor: ToolExecutor,
        all_candidates: dict[str, CitationCandidate],
        queries_used: list[str],
    ) -> Optional[dict]:
        """Run the tool-use loop until *final_tool* is called.

        Returns the final tool's input, or None when the model still had
        not called it after the deadline call (the only tool offered then is
        the final one). API errors propagate to the caller.
        """
        name = final_tool["name"]
        for round_num in range(MAX_AGENT_ROUNDS):
            self._emit("info", f"    Agent round {round_num + 1}...")
            response = self.caller.create(system=system, messages=messages, tools=tools)

            submit = find_tool_use(response, name)
            if submit is not None:
                return dict(submit.input or {})

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
                    f"Call {name} now with your selection. "
                    "Do not answer in text."
                )})
            else:
                self._emit("warning", f"    Unexpected stop_reason: {response.stop_reason}")
                break

        # Deadline: one more call where the only tool is the final one.
        self._emit("warning", f"    Hit max rounds ({MAX_AGENT_ROUNDS}), forcing final answer...")
        seen_summary = []
        for key, cand in list(all_candidates.items())[:20]:
            ident = f"PMID {cand.pmid}" if cand.pmid else (f"DOI {cand.doi}" if cand.doi else key)
            seen_summary.append(f"- {ident}: {cand.title} ({cand.year})")
        summary_text = "\n".join(seen_summary) if seen_summary else "(no articles fetched)"
        messages.append({"role": "user", "content": (
            f"DEADLINE REACHED. You must answer NOW by calling {name}.\n\n"
            f"Articles you have seen:\n{summary_text}\n\n"
            f"Select the best match(es) from the articles above. If none are "
            f"relevant, submit an empty \"selected\" list."
        )})
        response = self.caller.create(
            system=system, messages=messages,
            tools=build_tool_list(final_tool=final_tool)[-1:],
        )
        submit = find_tool_use(response, name)
        if submit is not None:
            return dict(submit.input or {})
        return None

    def find_citations(
        self,
        sentence: SentenceRecord,
        domain_context: list[str] = None,
        context: Optional[ClaimContext] = None,
    ) -> EvidenceRecord:
        """Run the agent loop for a single sentence. Returns an EvidenceRecord.

        ``context`` carries the section, neighbouring sentences and, for a
        multi-marker sentence, the sub-claim and exclusions; without it the
        bare sentence is used.
        """
        if context is None:
            context = bare_context(sentence)
        marker_type = sentence.marker_type or MarkerType.REF
        # Multi-(REF) sentences are handled by the orchestrator, which calls
        # find_citations once per marker with marker_count=1.
        num_refs = self.max_refs if marker_type == MarkerType.REFS else 1

        system = SYSTEM_PROMPT.format(max_refs=num_refs, max_rounds=MAX_AGENT_ROUNDS,
                                      preferences=self.preferences)
        messages = [{"role": "user", "content": self._user_message(context, num_refs, domain_context)}]

        # Track all candidates seen across rounds
        all_pmids_seen: dict[str, CitationCandidate] = {}
        queries_used: list[str] = []
        executor = self._make_executor(all_pmids_seen)
        evidence = EvidenceRecord(sentence_id=sentence.id)

        try:
            data = self._run_loop(system, messages, self.tools, SUBMIT_CITATIONS,
                                  executor, all_pmids_seen, queries_used)
            if data is not None:
                return self._finish(data, sentence, all_pmids_seen, queries_used)

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
        self._apply_common_fields(evidence, data, all_candidates, queries_used)
        return evidence

    def _apply_common_fields(
        self,
        evidence: EvidenceRecord,
        data: dict,
        all_candidates: dict[str, CitationCandidate],
        queries_used: list[str],
    ) -> None:
        """Confidence, verification, snippets, queries and candidate list."""
        # All candidates considered (PMIDs and DOIs)
        considered = data.get("all_pmids_considered", []) or []
        if not isinstance(considered, list):
            considered = []
        for p in considered:
            p = str(p)
            if p not in all_candidates and p.isdigit():
                for a in self.pubmed.fetch_articles([p]):
                    all_candidates[a.pmid] = a
        evidence.candidates = list(all_candidates.values())

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
        snippets = data.get("supporting_snippets", []) or []
        if not isinstance(snippets, list):
            snippets = [snippets]
        evidence.abstract_snippets = [str(s) for s in snippets if s]
        queries = data.get("search_queries_used", []) or []
        if not isinstance(queries, list):
            queries = [queries]
        evidence.search_query = " | ".join([str(q) for q in queries if q] or queries_used)
        evidence.search_result_count = len(all_candidates)

    # ── Author-suggested citations: verify and score ────────────────

    def evaluate_suggested(
        self,
        sentence: SentenceRecord,
        marker: MarkerSpec,
        resolved: list[ResolvedSuggestion],
        domain_context: list[str] = None,
        context: Optional[ClaimContext] = None,
    ) -> EvidenceRecord:
        """Confirm the author's suggested citation(s) for one marker and score them.

        Args:
            sentence: sentence whose ``clean_text`` is the claim to check.
            marker: the suggested marker (its ``suggestions`` drive the prompt).
            resolved: resolver output, one entry per suggestion, same order.
            domain_context: inferred document domains.
            context: section / neighbouring sentences and, for a sentence with
                several markers, the sub-claim this marker belongs to.
        """
        if context is None:
            context = bare_context(sentence)
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
            preferences=self.preferences,
        )

        # Pre-load the resolved records so the agent's identifiers resolve later.
        all_candidates: dict[str, CitationCandidate] = {}
        for r in resolved:
            for cand in r.candidates:
                key = candidate_key(cand)
                if key and key not in all_candidates:
                    all_candidates[key] = cand

        messages = [{"role": "user", "content": self._build_suggested_message(
            context, marker, resolved, domain_context)}]
        queries_used: list[str] = []
        executor = self._make_executor(all_candidates)

        try:
            data = self._run_loop(system, messages, self.suggested_tools,
                                  SUBMIT_SUGGESTED_EVALUATION, executor, all_candidates,
                                  queries_used)
            if data is None:
                self._emit("warning", "    Agent did not answer; using resolved records directly")
                return self._fallback_from_resolved(
                    sentence, marker, resolved, all_candidates,
                    "AI evaluation did not complete; citation resolved by identifier only",
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
        context: ClaimContext,
        marker: MarkerSpec,
        resolved: list[ResolvedSuggestion],
        domain_context: list[str],
    ) -> str:
        lines = ["CLAIM — the author cites the reference(s) below for this sentence:",
                 f'"{context.claim}"']
        if context.sub_claim:
            line = f'Specifically the part ending at the marker {marker.text}: "{context.sub_claim}"'
            if context.trailing:
                line += f' (followed by: "{context.trailing[:80]}")'
            lines.append(line)
        block = context.to_context_block()
        if block:
            lines += ["", block]
        extras = self._extra_instructions(context, domain_context)
        if extras:
            lines += ["", extras]
        lines += ["", f"The author wrote the marker {marker.text} here. Suggested references:", ""]

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
                    lines.append("    - " + _json(_article_summary(cand)))
            else:
                cand = r.candidates[0]
                lines.append(f"    Resolved via {r.source or 'lookup'}: "
                             + _json(_article_summary(cand, include_mesh=True)))
                if cand.is_retracted:
                    lines.append("    WARNING: this record is flagged as RETRACTED.")
            lines.append("")

        extra = self._extra_search_count(marker)
        if extra:
            lines.append(
                f"The author also asked for {extra} additional supporting reference(s); "
                f"search for them and include them with \"suggestion\": \"REF\"."
            )
        lines.append("Evaluate the suggested reference(s) against the claim and call "
                     "submit_suggested_evaluation.")
        return "\n".join(lines)

    # ── Response parsing (suggested markers) ────────────────────────

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
            for a in self.pubmed.fetch_articles([pmid]):
                all_candidates[a.pmid] = a
                return a
        for key in (pmid, doi, title):
            if key and key in all_candidates:
                return all_candidates[key]
        return None

    def _evidence_from_suggested_data(
        self,
        data: dict,
        sentence: SentenceRecord,
        marker: MarkerSpec,
        resolved: list[ResolvedSuggestion],
        all_candidates: dict[str, CitationCandidate],
        queries_used: list[str],
    ) -> EvidenceRecord:
        """Turn the submit_suggested_evaluation input into an EvidenceRecord.

        The author's suggestions define the slots: one selected citation per
        suggestion (in marker order), followed by any extra (REF) searches.
        Identifier suggestions are pinned to the record they resolved to, so
        the agent cannot silently substitute a different paper.
        """
        evidence = EvidenceRecord(sentence_id=sentence.id)
        data = data or {}
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
            evidence.warnings.append(Warning(
                code="suggested_alternative",
                message=(
                    f"The AI suggests an alternative citation: \"{cand.title[:70]}\" "
                    f"{self._ident(cand)}. {str(alt.get('why', '') or '').strip()}"
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
        cap_suggested_confidence(evidence)
        return evidence

    # ── Suggested-citation helpers ──────────────────────────────────

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
            def norm(d: str) -> str:
                d = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", (d or "").strip(), flags=re.I)
                return d.lower().rstrip(".")
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


def _json(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)
