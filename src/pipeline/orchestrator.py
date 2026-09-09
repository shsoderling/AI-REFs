"""Pipeline orchestrator: chains all stages in sequence.

Can run headlessly or emit Qt signals for GUI progress updates.
"""

import json
import logging
from typing import Optional, Callable

from ..models.project import ProjectState, PipelineStage
from ..models.sentence import SentenceRecord, MarkerType
from ..models.markers import MarkerSpec
from ..models.evidence import EvidenceRecord, ConfidenceLevel, Warning
from ..services.docx_io import DocxHandler
from ..services.pubmed_client import PubMedClient
from ..services.biorxiv_client import BioRxivClient
from ..services.europepmc_client import EuropePMCClient
from ..services.ref_library import ReferenceLibrary
from ..services.suggestion_resolver import SuggestionResolver
from ..services.tool_executor import candidate_key
from ..storage.cache_db import CacheDB
from .document_parser import DocumentParser
from .marker_locator import MarkerLocator
from .llm_citation_agent import LLMCitationAgent
from .global_qa import GlobalQA
from .existing_citation_parser import ExistingCitationParser

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
        marker_config = self.project.marker_config
        # Remember the grammar this run used so export scans the DOCX the same way
        self.project.run_marker_config = marker_config
        parser = DocumentParser(handler, stop_at_para=stop_at, marker_config=marker_config)
        sentences = parser.parse()
        self.project.sentences = sentences

        if self._cancelled:
            return self.project

        # ── Stage 2: Locate markers ───────────────────────────────────
        self.project.current_stage = PipelineStage.MARKER_LOCATION
        self._emit("info", f"Stage {stage_offset + 2}: Locating markers...")
        self._progress("Locate Markers", stage_offset + 1, total_stages)

        locator = MarkerLocator(marker_config)
        sentences = locator.locate(sentences)
        marked = locator.get_marked_sentences(sentences)

        n_search = sum(1 for s in marked for m in s.markers if m.kind != MarkerType.SUGGESTED)
        n_suggested = sum(1 for s in marked for m in s.markers if m.kind == MarkerType.SUGGESTED)
        self._emit(
            "info",
            f"Found {len(marked)} sentences with markers: {n_search} (REF)/(REFS) to search, "
            f"{n_suggested} author-suggested citation marker(s) to verify",
        )

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

        resolver = SuggestionResolver(
            pubmed=pubmed,
            europepmc=europepmc if settings.search_europepmc else None,
            biorxiv=biorxiv if settings.search_biorxiv else None,
            medrxiv=BioRxivClient(cache_db=cache, server="medrxiv") if settings.search_biorxiv else None,
            user_library=user_library,
            status_callback=lambda msg: self._emit("info", f"    {msg}"),
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

                # A lone (REF)/(REFS) marker is a plain search.  Anything else
                # (several markers, or an author-suggested citation) is handled
                # marker by marker so each slot gets its own citations.
                if len(sent.markers) <= 1 and not sent.has_suggested_marker:
                    evidence = agent.find_citations(
                        sent, self.project.inferred_domains
                    )
                    evidence.slot_sizes = [len(evidence.selected)]
                else:
                    evidence = self._find_citations_per_marker(
                        agent, resolver, sent, self.project.inferred_domains
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

    def _find_citations_per_marker(
        self,
        agent: LLMCitationAgent,
        resolver: SuggestionResolver,
        sentence: SentenceRecord,
        domain_context: list[str] = None,
    ) -> EvidenceRecord:
        """Handle a sentence marker by marker and merge the results.

        * ``(REF)`` / ``(REFS)`` markers are searched independently, each told
          which papers earlier markers already took so the sentence does not
          cite the same paper twice.
        * Author-suggested markers are resolved (library, PubMed, Europe PMC,
          bioRxiv) and then scored by the agent against the claim.

        The merged ``EvidenceRecord`` keeps one contiguous block of
        ``selected`` citations per marker; ``slot_sizes`` records the block
        lengths so review and export know which citations belong to which
        marker.
        """
        markers = list(sentence.markers)
        if not markers:
            # Legacy record without marker specs: fall back to a plain search
            evidence = agent.find_citations(sentence, domain_context)
            evidence.slot_sizes = [len(evidence.selected)]
            return evidence

        # Text segments between markers: segments[i] precedes marker i,
        # segments[-1] follows the last marker.
        raw = sentence.raw_text
        segments: list[str] = []
        prev = 0
        for m in markers:
            segments.append(raw[prev:m.start])
            prev = m.end
        segments.append(raw[prev:])

        multi = len(markers) > 1
        if multi:
            self._emit("info", f"  {len(markers)} markers: handling each independently")

        combined = EvidenceRecord(sentence_id=sentence.id)
        combined.slot_sizes = []
        all_candidates: list = []
        all_snippets: list[str] = []
        all_queries: list[str] = []
        all_warnings: list[Warning] = []
        rationales: list[str] = []
        worst_confidence = ConfidenceLevel.HIGH
        total_score = 0.0
        scored_slots = 0

        assigned_keys: set[str] = set()  # PMIDs / DOIs already used in this sentence

        confidence_order = {
            ConfidenceLevel.HIGH: 3,
            ConfidenceLevel.MEDIUM: 2,
            ConfidenceLevel.LOW: 1,
            ConfidenceLevel.UNRESOLVED: 0,
        }

        def _keys(cand) -> set[str]:
            keys = {candidate_key(cand)}
            if cand.pmid:
                keys.add(cand.pmid)
            if cand.doi:
                keys.add(cand.doi)
            return {k for k in keys if k}

        # Resolve every author-suggested marker up front so that (REF)/(REFS)
        # searches earlier in the sentence are told to avoid those papers.
        pre_resolved: dict[int, list] = {}
        for idx, marker in enumerate(markers):
            if marker.kind != MarkerType.SUGGESTED or not marker.suggestions:
                continue
            resolved = resolver.resolve_all(marker.suggestions)
            pre_resolved[idx] = resolved
            for r in resolved:
                if r.candidates and not r.ambiguous:
                    for cand in r.candidates:
                        assigned_keys |= _keys(cand)

        for idx, marker in enumerate(markers):
            self._check_pause()
            if self._cancelled:
                return combined

            before = segments[idx].strip()
            after = segments[idx + 1].strip() if idx + 1 < len(segments) else ""
            label = f"[{idx + 1}/{len(markers)}] {marker.text}" if multi else marker.text
            self._emit("info", f"    Marker {label}")

            chosen: list = []

            if marker.kind == MarkerType.SUGGESTED:
                resolved = pre_resolved.get(idx) or resolver.resolve_all(marker.suggestions)
                for r in resolved:
                    if r.candidates:
                        self._emit(
                            "info",
                            f"      {r.suggestion.label} -> {len(r.candidates)} record(s) via {r.source}",
                        )
                    else:
                        self._emit("warning", f"      {r.suggestion.label}: {r.error}")

                position_note = ""
                if multi and before:
                    position_note = (
                        f'The marker {marker.text} appears right after the words: "{before[-160:]}"'
                    )
                temp = SentenceRecord(
                    id=f"{sentence.id}_m{idx}",
                    paragraph_index=sentence.paragraph_index,
                    sentence_index=sentence.sentence_index,
                    raw_text=sentence.raw_text,
                    clean_text=sentence.clean_text,
                    section=sentence.section,
                    marker_type=MarkerType.SUGGESTED,
                    marker_count=1,
                    markers=[marker],
                    keywords=sentence.keywords,
                )
                ev = agent.evaluate_suggested(
                    temp, marker, resolved, domain_context, context_text=position_note,
                )
                chosen = list(ev.selected)
                if ev.confidence_level != ConfidenceLevel.UNRESOLVED:
                    total_score += ev.confidence_score
                    scored_slots += 1

            else:
                # (REF) -> one citation, (REFS) -> up to max_refs
                want_many = marker.kind == MarkerType.REFS
                if multi:
                    sub_claim = (
                        f'From the sentence: "{sentence.clean_text}"\n\n'
                        f'Find {"citations" if want_many else "a citation"} specifically for this part: "{before}"'
                    )
                    if after:
                        sub_claim += f' (followed by: "{after[:80]}")'
                else:
                    sub_claim = sentence.clean_text

                if assigned_keys:
                    exclusion_list = ", ".join(sorted(assigned_keys))
                    sub_claim += (
                        f"\n\nIMPORTANT: The following identifiers have already been "
                        f"assigned to other markers in this sentence. You MUST "
                        f"find a DIFFERENT paper -- do NOT select any of these: "
                        f"{exclusion_list}"
                    )

                temp = SentenceRecord(
                    id=f"{sentence.id}_m{idx}",
                    paragraph_index=sentence.paragraph_index,
                    sentence_index=sentence.sentence_index,
                    raw_text=sentence.raw_text,
                    clean_text=sub_claim,
                    section=sentence.section,
                    marker_type=marker.kind,
                    marker_count=1,
                    markers=[marker],
                    keywords=sentence.keywords,
                )
                ev = agent.find_citations(temp, domain_context)

                limit = agent.max_refs if want_many else 1
                for cand in ev.selected:
                    if len(chosen) >= limit:
                        break
                    if _keys(cand) & assigned_keys:
                        self._emit(
                            "warning",
                            f"      Agent returned a paper already used in this sentence "
                            f"({candidate_key(cand)}); looking for an alternative...",
                        )
                        continue
                    chosen.append(cand)
                if ev.selected and not chosen:
                    # Everything the agent picked is already used in this
                    # sentence: fall back to the next-best unique paper it
                    # looked at (same behaviour as the earlier multi-(REF) code).
                    for alt in ev.candidates:
                        if _keys(alt) & assigned_keys:
                            continue
                        chosen.append(alt)
                        self._emit("info", f"      Using alternative: {alt.title[:60]}")
                        break
                    if not chosen:
                        self._emit("warning", f"      No unique alternative found for marker {label}")
                total_score += ev.confidence_score
                scored_slots += 1

            for cand in chosen:
                assigned_keys |= _keys(cand)

            if not chosen:
                self._emit("warning", f"      No citation for marker {label}")

            combined.selected.extend(chosen)
            combined.slot_sizes.append(len(chosen))
            all_candidates.extend(ev.candidates)
            all_snippets.extend(ev.abstract_snippets)
            all_warnings.extend(ev.warnings)
            if ev.search_query:
                all_queries.append(ev.search_query)
            if ev.confidence_rationale:
                rationales.append(
                    f"{marker.text}: {ev.confidence_rationale}" if multi else ev.confidence_rationale
                )
            if ev.retrieval_error and not chosen:
                combined.retrieval_error = (
                    f"{combined.retrieval_error}; " if combined.retrieval_error else ""
                ) + (f"{marker.text}: {ev.retrieval_error}" if multi else ev.retrieval_error)
            if ev.verification_status.value != "not_checked" and not multi:
                combined.verification_status = ev.verification_status

            ev_conf_order = confidence_order.get(ev.confidence_level, 0)
            if ev_conf_order < confidence_order.get(worst_confidence, 3):
                worst_confidence = ev.confidence_level

        # Merge results (deduplicate candidates by key, keeping first occurrence)
        seen: set[str] = set()
        merged_candidates = []
        for cand in all_candidates:
            key = candidate_key(cand)
            if key and key in seen:
                continue
            seen.add(key)
            merged_candidates.append(cand)
        combined.candidates = merged_candidates
        combined.abstract_snippets = all_snippets
        combined.warnings = all_warnings
        combined.search_query = " | ".join(q for q in all_queries if q)
        combined.search_result_count = len(merged_candidates)
        combined.confidence_level = worst_confidence
        combined.confidence_score = total_score / scored_slots if scored_slots else 0.0
        combined.confidence_rationale = " || ".join(rationales) if multi else (
            rationales[0] if rationales else ""
        )
        if combined.selected and combined.retrieval_error and not multi:
            combined.retrieval_error = ""

        self._emit(
            "info",
            f"  Result: {len(combined.selected)} citation(s) across {len(markers)} marker(s), "
            f"confidence={combined.confidence_level.value}"
        )
        return combined
