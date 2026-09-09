"""Pipeline orchestrator: chains all stages in sequence.

Can run headlessly or emit Qt signals for GUI progress updates.
"""

import json
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional, Callable

from ..models.project import ProjectState, PipelineStage
from ..models.embedded import DocumentTier
from ..models.markers import MarkerSpec
from ..models.sentence import SentenceRecord, MarkerType
from ..models.evidence import EvidenceRecord, ConfidenceLevel, Warning
from ..services.docx_io import DocxHandler
from ..services.pubmed_client import PubMedClient
from ..services.biorxiv_client import BioRxivClient
from ..services.europepmc_client import EuropePMCClient
from ..services.ref_library import ReferenceLibrary
from ..services.model_catalog import resolve_model_id
from ..services.suggestion_resolver import SuggestionResolver
from ..storage.cache_db import CacheDB
from .document_parser import DocumentParser
from .marker_locator import MarkerLocator
from .llm_citation_agent import LLMCitationAgent, cap_suggested_confidence
from .global_qa import GlobalQA
from .existing_citation_parser import ExistingCitationParser
from .renumbering import CitationKeyIndex
from .claim_context import ClaimContext, bare_context, build_claim_context, sub_claims_for
from .verification import CitationVerifier, deterministic_checks

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
        from ..services.claude_client import make_client
        client = make_client(api_key)
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

TOTAL_STAGES_FRESH = 5
TOTAL_STAGES_INSERT = 6

_CONFIDENCE_ORDER = {
    ConfidenceLevel.HIGH: 3,
    ConfidenceLevel.MEDIUM: 2,
    ConfidenceLevel.LOW: 1,
    ConfidenceLevel.UNRESOLVED: 0,
}


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
        # sentence id -> one ClaimContext per selected citation (built during
        # search, reused by verification)
        self._contexts: dict[str, list[ClaimContext]] = {}

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
            existing = self.project.existing_citations
            is_tracked = (existing is not None and existing.tracking is not None
                          and existing.tracking.tier == DocumentTier.TRACKED)
            if not is_tracked:
                citation_parser = ExistingCitationParser(
                    handler, keep_uncited=settings.keep_uncited_entries)
                self.project.existing_citations = citation_parser.analyze()
                existing = self.project.existing_citations
            else:
                self._emit("info", "  Tracked document: citations read from embedded fields")
            self._emit("info", f"  Found {len(existing.bib_entries)} existing references")
            self._emit("info", f"  References heading at paragraph {existing.references_heading_para_idx}")
            self._emit("info", f"  Max existing number: {existing.max_existing_number}")

            # Recover PMIDs/DOIs for entries that don't print one, so new
            # candidates can be deduplicated against them.
            needs_ids = sum(1 for e in existing.bib_entries.values()
                            if not e.pmid and not e.doi and not e.record_uuid)
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

        # The marker grammar of this run is recorded on the project: export
        # must re-scan the DOCX with exactly this configuration.
        marker_config = self.project.marker_config
        self.project.run_marker_config = marker_config

        handler = DocxHandler(self.project.input_docx_path)
        stop_at = -1
        if is_insert and self.project.existing_citations:
            stop_at = self.project.existing_citations.body_end_para_idx
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

        n_suggested = sum(1 for s in marked for m in s.markers if m.kind == MarkerType.SUGGESTED)
        self._emit("info", f"Found {len(marked)} sentences with markers"
                           + (f" ({n_suggested} author-suggested citation marker(s))"
                              if n_suggested else ""))

        if not marked:
            self._emit("info", "No markers found in document. Nothing to process.")
            self.project.current_stage = PipelineStage.COMPLETE
            return self.project

        # Domain inference (used as context for the agent)
        if settings.domain_inference and settings.anthropic_api_key:
            self._emit("info", "Inferring document research domains...")
            self.project.inferred_domains = _infer_domains_with_llm(
                sentences, settings.anthropic_api_key, resolve_model_id(settings.claude_model),
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
        self._contexts = {}

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

        # Looks up author-suggested citations (library, PubMed, Europe PMC,
        # bioRxiv/medRxiv) before the agent scores them.
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
            model=resolve_model_id(settings.claude_model),
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
            prefer_reviews=settings.prefer_reviews,
            recency_bias=settings.recency_bias,
        )

        try:
            workers = self._effective_parallelism()
            if workers > 1:
                self._emit("info", f"  Running {workers} sentence searches in parallel")

            def search_one(i: int, sent: SentenceRecord):
                return self._run_sentence(i, sent, marked, sentences, agent, resolver)

            for sent, evidence in self._run_pool(workers, marked, search_one):
                if evidence is not None:
                    self.project.evidence_map[sent.id] = evidence

            self._check_pause()
            if self._cancelled:
                return self.project

            # ── Stage 4: Verification ─────────────────────────────────────
            self.project.current_stage = PipelineStage.VERIFICATION
            self._emit("info", f"Stage {stage_offset + 4}: Verifying citations...")
            self._progress("Verify Citations", stage_offset + 3, total_stages)
            self._verify_selections(marked, agent, pubmed, biorxiv, europepmc)

            self._check_pause()
            if self._cancelled:
                return self.project

            # ── Stage 5: Global QA ────────────────────────────────────────
            self.project.current_stage = PipelineStage.GLOBAL_QA
            self._emit("info", f"Stage {stage_offset + 5}: Running global QA...")
            self._progress("Global QA", stage_offset + 4, total_stages)

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

    def _verify_selections(self, marked, agent: LLMCitationAgent, pubmed, biorxiv, europepmc):
        """Deterministic checks for every selection, then the LLM verifier
        when enabled. Each sentence's verdicts are aligned with its
        selected references."""
        settings = self.project.settings
        verifier = None
        if settings.verify_citations and settings.anthropic_api_key:
            verifier = CitationVerifier(
                agent.caller,
                europepmc if settings.search_europepmc else None,
                use_full_text=settings.use_full_text,
                log=self._log,
            )
        else:
            self._emit("info", "  Independent verification is off; running retraction/preprint checks only")

        def verify_one(i: int, sent: SentenceRecord) -> bool:
            self._check_pause()
            if self._cancelled:
                return False
            evidence = self.project.evidence_map.get(sent.id)
            if evidence is None or not evidence.selected:
                return False
            deterministic_checks(evidence, biorxiv=biorxiv, europepmc=europepmc,
                                 pubmed=pubmed, log=self._log)
            if verifier is None:
                return False
            contexts = self._contexts.get(sent.id) or [bare_context(sent)]
            verifier.verify_evidence(contexts, evidence)
            if sent.has_suggested_marker:
                # The verifier may raise the level for the papers it could
                # check; an unresolved or ambiguous suggestion still caps it.
                cap_suggested_confidence(evidence)
            return True

        checked = sum(1 for _, done in self._run_pool(self._effective_parallelism(), marked, verify_one)
                      if done)
        if verifier is not None:
            self._emit("info", f"  Verified selections for {checked} sentence(s)")

    # ── concurrency helpers ──────────────────────────────────────────

    def _effective_parallelism(self) -> int:
        """Worker threads for the per-sentence stages: 1 without an NCBI API
        key (3 requests/s would be exceeded), else the setting clamped to 1–8."""
        settings = self.project.settings
        if not settings.ncbi_api_key:
            return 1
        return max(1, min(int(settings.parallel_searches or 1), 8))

    def _run_pool(self, workers: int, items: list, func):
        """Apply ``func(index, item)`` to every item on ``workers`` threads and
        yield ``(item, result)`` in the original order.

        Cancel makes pending items return quickly (each checks the flag);
        an exception cancels the rest and is re-raised.
        """
        if workers <= 1:
            for i, item in enumerate(items):
                yield item, func(i, item)
            return
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="airefs-search") as pool:
            futures: list[Future] = [pool.submit(func, i, item) for i, item in enumerate(items)]
            try:
                for item, fut in zip(items, futures):
                    yield item, fut.result()
            except BaseException:
                self._cancelled = True
                pool.shutdown(wait=False, cancel_futures=True)
                raise

    def _run_sentence(self, i: int, sent: SentenceRecord, marked: list, sentences: list,
                      agent: LLMCitationAgent,
                      resolver: Optional[SuggestionResolver] = None) -> Optional[EvidenceRecord]:
        """Search one sentence (one call, or one per marker); None when cancelled."""
        self._check_pause()
        if self._cancelled:
            return None

        self._emit("info", f"  [{i+1}/{len(marked)}] {sent.id}: {sent.clean_text[:60]}...")

        # Sentences with several markers that include at least one (REF), and
        # any sentence with an author-suggested citation, are handled marker
        # by marker.  All-(REFS) sentences keep the single combined search.
        base_ctx = build_claim_context(sentences, sent)
        if sent.searched_per_marker:
            evidence = self._find_citations_per_marker(
                agent, sent, self.project.inferred_domains, base_ctx, resolver,
            )
        else:
            self._contexts[sent.id] = [base_ctx]
            evidence = agent.find_citations(
                sent, self.project.inferred_domains, context=base_ctx
            )
            evidence.slot_sizes = [len(evidence.selected)]
        if not evidence.selected:
            self._emit("warning", f"  No citations found for {sent.id}")
        return evidence

    def _find_citations_per_marker(
        self,
        agent: LLMCitationAgent,
        sentence: SentenceRecord,
        domain_context: list[str] = None,
        base_context: ClaimContext = None,
        resolver: Optional[SuggestionResolver] = None,
    ) -> EvidenceRecord:
        """Handle a sentence marker by marker and merge the results.

        * ``(REF)`` / ``(REFS)`` markers are searched independently, each told
          which papers earlier markers already took so the sentence does not
          cite the same paper twice.  If a duplicate still slips through, the
          next-best unique candidate from the result set is used.
        * Author-suggested markers are resolved (library, PubMed, Europe PMC,
          bioRxiv) and then scored by the agent against the claim.

        The merged ``EvidenceRecord`` keeps one contiguous block of
        ``selected`` citations per marker; ``slot_sizes`` records the block
        lengths so review and export know which citations belong to which
        marker.  One :class:`ClaimContext` per selected citation is kept for
        the verifier.
        """
        from ..models.sentence import SentenceRecord as SR

        if base_context is None:
            base_context = build_claim_context(self.project.sentences, sentence)
        markers = self._marker_specs(sentence)
        parts = sub_claims_for(sentence)
        num_markers = len(markers)
        multi = num_markers > 1
        self._contexts[sentence.id] = []   # one context per *selected* citation

        if multi:
            self._emit("info", f"  Multi-marker: splitting into {num_markers} independent searches")

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

        # Track already-assigned papers to prevent duplicates.  The key index
        # treats PMID/DOI/title as aliases of one identity, so the same paper
        # found via different identifiers still counts as a duplicate.
        key_index = CitationKeyIndex()
        assigned_keys: set[str] = set()         # canonical identity keys
        assigned_identifiers: set[str] = set()  # human-readable PMIDs/DOIs for the prompt

        def _assign(cand):
            assigned_keys.add(key_index.key_for_candidate(cand))
            if cand.pmid:
                assigned_identifiers.add(cand.pmid)
            if cand.doi:
                assigned_identifiers.add(cand.doi)

        # Resolve every author-suggested marker up front so that (REF)/(REFS)
        # searches earlier in the sentence are told to avoid those papers.
        pre_resolved: dict[int, list] = {}
        if resolver is not None:
            for idx, marker in enumerate(markers):
                if marker.kind != MarkerType.SUGGESTED or not marker.suggestions:
                    continue
                resolved = resolver.resolve_all(marker.suggestions)
                pre_resolved[idx] = resolved
                for r in resolved:
                    if r.candidates and not r.ambiguous:
                        for cand in r.candidates:
                            _assign(cand)

        for idx, marker in enumerate(markers):
            self._check_pause()
            if self._cancelled:
                return combined

            sub_text, trailing = parts[idx] if idx < len(parts) else ("", "")
            label = f"[{idx + 1}/{num_markers}] {marker.text}" if multi else marker.text
            self._emit("info", f"    Marker {label}: {sub_text[:50]}...")
            chosen: list = []

            if marker.kind == MarkerType.SUGGESTED:
                if resolver is not None:
                    resolved = pre_resolved.get(idx)
                    if resolved is None:
                        resolved = resolver.resolve_all(marker.suggestions)
                else:
                    resolved = []
                for r in resolved:
                    if r.candidates:
                        self._emit(
                            "info",
                            f"      {r.suggestion.label} -> {len(r.candidates)} record(s) via {r.source}",
                        )
                    else:
                        self._emit("warning", f"      {r.suggestion.label}: {r.error}")

                ctx = base_context.with_marker(sub_text if multi else "", trailing if multi else "", [])
                temp = SR(
                    id=f"{sentence.id}_m{idx}",
                    paragraph_index=sentence.paragraph_index,
                    sentence_index=sentence.sentence_index,
                    raw_text=sentence.raw_text,
                    clean_text=sentence.clean_text,
                    section=sentence.section,
                    marker_type=MarkerType.SUGGESTED,
                    marker_count=1,
                    marker_types=[MarkerType.SUGGESTED],
                    markers=[marker],
                    keywords=sentence.keywords,
                )
                ev = agent.evaluate_suggested(temp, marker, resolved, domain_context, context=ctx)
                chosen = list(ev.selected)
                if ev.confidence_level != ConfidenceLevel.UNRESOLVED:
                    total_score += ev.confidence_score
                    scored_slots += 1

            else:
                # (REF) -> one citation, (REFS) -> up to max_refs
                want_many = marker.kind == MarkerType.REFS
                ctx = base_context.with_marker(sub_text if multi else "", trailing if multi else "",
                                               sorted(assigned_identifiers))
                temp = SR(
                    id=f"{sentence.id}_m{idx}",
                    paragraph_index=sentence.paragraph_index,
                    sentence_index=sentence.sentence_index,
                    raw_text=sentence.raw_text,
                    clean_text=sentence.clean_text,
                    section=sentence.section,
                    marker_type=marker.kind,
                    marker_count=1,
                    marker_types=[marker.kind],
                    markers=[marker],
                    keywords=sentence.keywords,
                )
                ev = agent.find_citations(temp, domain_context, context=ctx)

                limit = agent.max_refs if want_many else 1
                for cand in ev.selected:
                    if len(chosen) >= limit:
                        break
                    if key_index.key_for_candidate(cand) in assigned_keys:
                        self._emit(
                            "warning",
                            f"      Agent returned a paper already used in this sentence "
                            f"({cand.pmid or cand.doi or cand.title[:40]}); looking for an alternative...",
                        )
                        continue
                    chosen.append(cand)
                    _assign(cand)
                if ev.selected and not chosen:
                    # Everything the agent picked is already used in this
                    # sentence: fall back to the next-best unique paper it saw.
                    for alt in ev.candidates:
                        if key_index.key_for_candidate(alt) in assigned_keys:
                            continue
                        chosen.append(alt)
                        _assign(alt)
                        self._emit("info", f"      Using alternative: {alt.title[:60]}")
                        break
                    if not chosen:
                        self._emit("warning", f"      No unique alternative found for marker {label}")
                total_score += ev.confidence_score
                scored_slots += 1

            for cand in chosen:
                _assign(cand)
                self._contexts[sentence.id].append(ctx)

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

            if _CONFIDENCE_ORDER.get(ev.confidence_level, 0) < _CONFIDENCE_ORDER.get(worst_confidence, 3):
                worst_confidence = ev.confidence_level

        # Merge results (deduplicate candidates by identity, keeping first occurrence)
        seen: set[str] = set()
        merged_candidates = []
        for cand in all_candidates:
            key = key_index.key_for_candidate(cand)
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
        cap_suggested_confidence(combined)

        self._emit(
            "info",
            f"  Result: {len(combined.selected)} citation(s) across {num_markers} marker(s), "
            f"confidence={combined.confidence_level.value}"
        )
        return combined

    # Name used before author-suggested markers existed.
    _find_citations_per_ref = _find_citations_per_marker

    @staticmethod
    def _marker_specs(sentence: SentenceRecord) -> list[MarkerSpec]:
        """The sentence's markers, synthesised from the marker types when a
        record (an older project, a hand-built test sentence) has no spans."""
        if sentence.markers:
            return list(sentence.markers)
        return [MarkerSpec(text=f"({t.value})", kind=t) for t in sentence.effective_marker_types()]
