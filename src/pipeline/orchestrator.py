"""Pipeline orchestrator: chains all stages in sequence.

Can run headlessly or emit Qt signals for GUI progress updates.
"""

import json
import logging
from typing import Optional, Callable

from ..models.project import ProjectState, PipelineStage
from ..models.sentence import SentenceRecord, MarkerType
from ..models.evidence import EvidenceRecord, ConfidenceLevel
from ..services.docx_io import DocxHandler
from ..services.pubmed_client import PubMedClient
from ..services.biorxiv_client import BioRxivClient
from ..services.europepmc_client import EuropePMCClient
from ..services.ref_library import ReferenceLibrary
from ..storage.cache_db import CacheDB
from .document_parser import DocumentParser
from .marker_locator import MarkerLocator
from .llm_citation_agent import LLMCitationAgent
from .global_qa import GlobalQA
from .existing_citation_parser import ExistingCitationParser
from .renumbering import CitationKeyIndex

logger = logging.getLogger(__name__)


# ── Domain inference ─────────────────────────────────────────────────

_DOMAIN_INFERENCE_PROMPT = """\
You are a scientific domain classifier.  Given the text of a research document, \
identify the 1–3 most specific scientific domains or sub-fields that best \
describe its content.  Be precise — prefer sub-fields over broad categories \
(e.g. "synaptic neuroscience" over "biology", "proximity proteomics" over \
"chemistry").

Return ONLY a JSON array of 1–3 short domain strings, nothing else.
Example: ["synaptic neuroscience", "proximity proteomics", "protein language models"]
"""


def _infer_domains_with_llm(
    sentences: list[SentenceRecord],
    api_key: str,
    model: str,
) -> list[str]:
    """Use a lightweight LLM call to infer research domains from document text.

    Sends a condensed version of the document (up to ~2000 chars) and asks the
    model to return 1–3 specific scientific domains as a JSON array.
    """
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not available for domain inference")
        return []

    # Build a condensed document excerpt (first ~2000 chars of real text)
    all_text = " ".join(s.clean_text for s in sentences if s.clean_text.strip())
    excerpt = all_text[:2000]
    if len(all_text) > 2000:
        excerpt += " ..."

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=150,
            system=_DOMAIN_INFERENCE_PROMPT,
            messages=[{"role": "user", "content": excerpt}],
        )
        raw = response.content[0].text.strip()
        # Parse JSON array from response
        domains = json.loads(raw)
        if isinstance(domains, list):
            return [str(d).strip() for d in domains[:3] if d]
        logger.warning(f"Domain inference returned non-list: {raw}")
        return []
    except json.JSONDecodeError:
        # Try to salvage a comma-separated response
        raw = response.content[0].text.strip().strip("[]")
        parts = [p.strip().strip('"').strip("'") for p in raw.split(",")]
        return [p for p in parts[:3] if p]
    except Exception as exc:
        logger.warning(f"LLM domain inference failed: {exc}")
        return []

TOTAL_STAGES_FRESH = 4
TOTAL_STAGES_INSERT = 5


class PipelineOrchestrator:
    """Run the full reference-finding pipeline."""

    def __init__(self, project: ProjectState,
                 progress_callback: Optional[Callable[[str, int, int], None]] = None,
                 log_callback: Optional[Callable[[str, str], None]] = None):
        """
        Args:
            project: ProjectState to populate
            progress_callback: Called with (stage_name, current, total) for progress
            log_callback: Called with (level, message) for GUI log panel
        """
        self.project = project
        self._progress = progress_callback or (lambda *a: None)
        self._log = log_callback or (lambda *a: None)
        self._paused = False
        self._cancelled = False

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def cancel(self):
        self._cancelled = True

    def _check_pause(self):
        """Block until unpaused or cancelled."""
        while self._paused and not self._cancelled:
            import time
            time.sleep(0.1)

    def _emit(self, level: str, msg: str):
        """Emit a log message."""
        getattr(logger, level, logger.info)(msg)
        self._log(level, msg)

    def run(self) -> ProjectState:
        """Execute the full pipeline. Returns the updated ProjectState."""
        try:
            return self._run_stages()
        except Exception as e:
            self.project.current_stage = PipelineStage.ERROR
            self._emit("error", f"Pipeline error: {e}")
            raise

    def _run_stages(self) -> ProjectState:
        settings = self.project.settings
        is_insert = self.project.is_insert_mode
        total_stages = TOTAL_STAGES_INSERT if is_insert else TOTAL_STAGES_FRESH
        stage_offset = 0  # Incremented by 1 in insert mode after stage 0

        # ── Stage 0 (Insert Mode Only): Analyze existing citations ────
        if is_insert:
            self.project.current_stage = PipelineStage.EXISTING_CITATION_ANALYSIS
            self._emit("info", "Stage 0: Analyzing existing citations...")
            self._progress("Analyze Existing Citations", 0, total_stages)

            handler = DocxHandler(self.project.input_docx_path)
            citation_parser = ExistingCitationParser(handler)
            self.project.existing_citations = citation_parser.analyze()

            existing = self.project.existing_citations
            self._emit("info", f"  Found {len(existing.bib_entries)} existing references")
            self._emit("info", f"  References heading at paragraph {existing.references_heading_para_idx}")
            self._emit("info", f"  Max existing number: {existing.max_existing_number}")

            # Recover PMIDs/DOIs for entries that don't print one, so new
            # candidates can be deduplicated against them.
            needs_ids = sum(1 for e in existing.bib_entries.values()
                            if not e.pmid and not e.doi)
            if settings.enrich_existing_refs and settings.ncbi_email and needs_ids:
                self._emit("info",
                           f"  Matching {needs_ids} entries without PMID/DOI "
                           f"against PubMed...")
                from .existing_enrichment import ExistingRefEnricher
                enrich_cache = CacheDB()
                enrich_pubmed = PubMedClient(
                    email=settings.ncbi_email,
                    api_key=settings.ncbi_api_key or "",
                    cache_db=enrich_cache,
                )
                enricher = ExistingRefEnricher(
                    enrich_pubmed, cache=enrich_cache, log_callback=self._log,
                )
                enriched = enricher.enrich(
                    existing, should_cancel=lambda: self._cancelled)
                self._emit("info",
                           f"  Recovered identifiers for {enriched}/{needs_ids} entries")

            if self._cancelled:
                return self.project

            stage_offset = 1

        # ── Stage 1: Parse document ───────────────────────────────────
        self.project.current_stage = PipelineStage.PARSING
        self._emit("info", f"Stage {stage_offset + 1}: Parsing document...")
        self._progress("Parse Document", stage_offset, total_stages)

        handler = DocxHandler(self.project.input_docx_path)
        stop_at = -1
        if is_insert and self.project.existing_citations:
            stop_at = self.project.existing_citations.references_heading_para_idx
        parser = DocumentParser(handler, stop_at_para=stop_at)
        sentences = parser.parse()
        self.project.sentences = sentences

        if self._cancelled:
            return self.project

        # ── Stage 2: Locate markers ───────────────────────────────────
        self.project.current_stage = PipelineStage.MARKER_LOCATION
        self._emit("info", f"Stage {stage_offset + 2}: Locating markers...")
        self._progress("Locate Markers", stage_offset + 1, total_stages)

        locator = MarkerLocator()
        sentences = locator.locate(sentences)
        marked = locator.get_marked_sentences(sentences)

        self._emit("info", f"Found {len(marked)} sentences with markers")

        if not marked:
            self._emit("info", "No markers found in document. Nothing to process.")
            self.project.current_stage = PipelineStage.COMPLETE
            return self.project

        # Domain inference (used as context for the agent)
        if settings.domain_inference and settings.anthropic_api_key:
            self._emit("info", "Inferring document research domains...")
            self.project.inferred_domains = _infer_domains_with_llm(
                sentences, settings.anthropic_api_key, settings.claude_model,
            )
            self._emit("info", f"Inferred domains: {self.project.inferred_domains}")

        self._check_pause()
        if self._cancelled:
            return self.project

        # ── Stage 3: AI Citation Search ───────────────────────────────
        self.project.current_stage = PipelineStage.AI_CITATION_SEARCH
        self._emit("info", f"Stage {stage_offset + 3}: AI-powered citation search...")
        self._progress("AI Citation Search", stage_offset + 2, total_stages)

        # Discard evidence from any previous run — sentence IDs are sequential
        # (S001, S002, ...), so stale records would silently attach to the
        # wrong sentences after the document is edited or replaced.
        self.project.evidence_map = {}

        cache = CacheDB()
        pubmed = PubMedClient(
            email=settings.ncbi_email,
            api_key=settings.ncbi_api_key or "",
            cache_db=cache,
        )
        biorxiv = BioRxivClient(cache_db=cache, server="biorxiv")
        europepmc = EuropePMCClient(cache_db=cache)
        user_library = None
        if settings.reference_library_enabled:
            user_library = ReferenceLibrary(settings.reference_library_path or "")
            self._emit(
                "info",
                f"Using user library: {user_library.db_path}",
            )

        agent = LLMCitationAgent(
            anthropic_api_key=settings.anthropic_api_key,
            model=settings.claude_model,
            pubmed_client=pubmed,
            biorxiv_client=biorxiv,
            europepmc_client=europepmc,
            reference_library=user_library,
            search_biorxiv=settings.search_biorxiv,
            search_europepmc=settings.search_europepmc,
            prefer_user_library=settings.prefer_user_library,
            max_library_results=settings.max_library_results,
            max_refs=settings.max_refs_for_refs,
            orcid_id=settings.orcid_id,
            log_callback=self._log,
        )

        try:
            for i, sent in enumerate(marked):
                self._check_pause()
                if self._cancelled:
                    return self.project

                self._emit(
                    "info",
                    f"  [{i+1}/{len(marked)}] {sent.id}: {sent.clean_text[:60]}..."
                )

                # Sentences with multiple markers that include at least one
                # (REF): run a SEPARATE search for each marker.  All-(REFS)
                # sentences keep the single combined search.
                marker_types = sent.effective_marker_types()
                if len(marker_types) > 1 and MarkerType.REF in marker_types:
                    evidence = self._find_citations_per_ref(
                        agent, sent, self.project.inferred_domains
                    )
                else:
                    evidence = agent.find_citations(
                        sent, self.project.inferred_domains
                    )
                self.project.evidence_map[sent.id] = evidence

                if not evidence.selected:
                    self._emit("warning", f"  No citations found for {sent.id}")

            self._check_pause()
            if self._cancelled:
                return self.project

            # ── Stage 4: Global QA ────────────────────────────────────────
            self.project.current_stage = PipelineStage.GLOBAL_QA
            self._emit("info", f"Stage {stage_offset + 4}: Running global QA...")
            self._progress("Global QA", stage_offset + 3, total_stages)

            qa = GlobalQA()
            self.project.evidence_map = qa.run(sentences, self.project.evidence_map)
        finally:
            if user_library:
                try:
                    user_library.close()
                except Exception:
                    pass

        # Done
        self.project.current_stage = PipelineStage.COMPLETE
        self._progress("Complete", total_stages, total_stages)

        resolved = sum(1 for ev in self.project.evidence_map.values()
                      if ev.selected)
        self._emit("info",
                   f"Pipeline complete: {resolved}/{len(marked)} sentences have citations")

        return self.project

    def _find_citations_per_ref(
        self,
        agent: LLMCitationAgent,
        sentence: SentenceRecord,
        domain_context: list[str] = None,
    ):
        """Handle sentences with multiple (REF) markers by searching independently.

        Splits the sentence at each (REF) marker to figure out which sub-claim
        needs a citation, runs the agent once per marker, then merges results
        into a single EvidenceRecord with one citation per slot.

        Each subsequent marker search is told which PMIDs/DOIs were already
        assigned so the agent avoids returning duplicates.  If a duplicate
        still slips through, a post-search deduplication picks the next-best
        unique candidate from the result set.
        """
        import re
        from ..models.evidence import EvidenceRecord, ConfidenceLevel, VerificationStatus
        from ..models.sentence import SentenceRecord as SR

        # Split the raw text at each marker to identify the sub-claims
        parts = re.split(r'\(REFS?\)', sentence.raw_text)
        num_markers = sentence.marker_count
        marker_types = sentence.effective_marker_types()

        self._emit("info", f"  Multi-marker: splitting into {num_markers} independent searches")

        # Run agent for each sub-claim independently
        combined_evidence = EvidenceRecord(sentence_id=sentence.id)
        all_candidates = []
        all_snippets = []
        all_queries = []
        worst_confidence = ConfidenceLevel.HIGH
        total_score = 0.0

        # Track already-assigned papers to prevent duplicates.  The key index
        # treats PMID/DOI/title as aliases of one identity, so the same paper
        # found via different identifiers still counts as a duplicate.
        key_index = CitationKeyIndex()
        assigned_keys: set[str] = set()       # canonical identity keys
        assigned_identifiers: set[str] = set()  # human-readable PMIDs/DOIs for the prompt

        confidence_order = {
            ConfidenceLevel.HIGH: 3,
            ConfidenceLevel.MEDIUM: 2,
            ConfidenceLevel.LOW: 1,
            ConfidenceLevel.UNRESOLVED: 0,
        }

        for marker_idx in range(num_markers):
            self._check_pause()
            if self._cancelled:
                return combined_evidence

            # A (REFS) marker inside a multi-marker sentence is searched like
            # a (REF): one citation per marker keeps the marker→citation slot
            # mapping unambiguous for review and export.
            if marker_idx < len(marker_types) and marker_types[marker_idx] == MarkerType.REFS:
                self._emit(
                    "info",
                    f"    Marker [{marker_idx+1}] is (REFS) in a multi-marker "
                    f"sentence — selecting a single best citation for it"
                )

            # Build a descriptive sub-claim for this marker
            sub_text = parts[marker_idx].strip() if marker_idx < len(parts) else ""
            trailing = parts[marker_idx + 1].strip() if marker_idx + 1 < len(parts) else ""

            sub_claim = (
                f"From the sentence: \"{sentence.clean_text}\"\n\n"
                f"Find a citation specifically for this part: \"{sub_text}\""
            )
            if trailing:
                sub_claim += f" (followed by: \"{trailing[:80]}\")"

            # Tell the agent which citations are already assigned to prior markers
            if assigned_identifiers:
                exclusion_list = ", ".join(sorted(assigned_identifiers))
                sub_claim += (
                    f"\n\nIMPORTANT: The following identifiers have already been "
                    f"assigned to other (REF) markers in this sentence. You MUST "
                    f"find a DIFFERENT paper — do NOT select any of these: "
                    f"{exclusion_list}"
                )

            # Create a temporary single-marker sentence for the agent
            temp_sentence = SR(
                id=f"{sentence.id}_ref{marker_idx}",
                paragraph_index=sentence.paragraph_index,
                sentence_index=sentence.sentence_index,
                raw_text=sentence.raw_text,
                clean_text=sub_claim,
                section=sentence.section,
                marker_type=MarkerType.REF,
                marker_count=1,  # Treat as single (REF)
                keywords=sentence.keywords,
            )

            self._emit(
                "info",
                f"    Marker [{marker_idx+1}/{num_markers}]: "
                f"{parts[marker_idx].strip()[:50]}..."
            )

            ev = agent.find_citations(temp_sentence, domain_context)

            # Collect the best single citation, enforcing uniqueness
            selected_citation = None
            if ev.selected:
                candidate = ev.selected[0]
                cand_key = key_index.key_for_candidate(candidate)

                if cand_key not in assigned_keys:
                    selected_citation = candidate
                else:
                    # Agent returned a duplicate — pick the next-best unique candidate
                    self._emit(
                        "warning",
                        f"    Agent returned duplicate ({cand_key}) for marker "
                        f"[{marker_idx+1}], searching for alternative..."
                    )
                    for alt in ev.candidates:
                        alt_key = key_index.key_for_candidate(alt)
                        if alt_key not in assigned_keys:
                            selected_citation = alt
                            self._emit(
                                "info",
                                f"    Using alternative: {alt.title[:60]}"
                            )
                            break

                    if not selected_citation:
                        self._emit(
                            "warning",
                            f"    No unique alternative found for marker [{marker_idx+1}]"
                        )

            if selected_citation:
                combined_evidence.selected.append(selected_citation)
                assigned_keys.add(key_index.key_for_candidate(selected_citation))
                if selected_citation.pmid:
                    assigned_identifiers.add(selected_citation.pmid)
                if selected_citation.doi:
                    assigned_identifiers.add(selected_citation.doi)
            else:
                self._emit("warning", f"    No citation for marker [{marker_idx+1}]")

            all_candidates.extend(ev.candidates)
            all_snippets.extend(ev.abstract_snippets)
            if ev.search_query:
                all_queries.append(ev.search_query)

            # Track worst confidence across all sub-searches
            ev_conf_order = confidence_order.get(ev.confidence_level, 0)
            worst_conf_order = confidence_order.get(worst_confidence, 3)
            if ev_conf_order < worst_conf_order:
                worst_confidence = ev.confidence_level
            total_score += ev.confidence_score

        # Merge results
        combined_evidence.candidates = all_candidates
        combined_evidence.abstract_snippets = all_snippets
        combined_evidence.search_query = " | ".join(all_queries)
        combined_evidence.search_result_count = len(all_candidates)
        combined_evidence.confidence_level = worst_confidence
        combined_evidence.confidence_score = total_score / max(num_markers, 1)
        combined_evidence.confidence_rationale = (
            f"Combined from {num_markers} independent per-marker searches"
        )

        self._emit(
            "info",
            f"  Multi-(REF) result: {len(combined_evidence.selected)}/{num_markers} "
            f"citations found, confidence={combined_evidence.confidence_level.value}"
        )

        return combined_evidence
