"""Build the insert-mode renumbering plan (pure, GUI-free).

Matches new (REF)/(REFS) markers in the document to their reviewed citations
and computes the merged numbering across existing + new citations.  Used by
the insert-mode export, the numbering preview, and tests.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from ..models.citation import CitationCandidate, is_valid_citation
from ..models.evidence import ReviewDecision
from ..models.project import ProjectState
from ..models.sentence import MarkerType, SentenceRecord
from .renumbering import NewMarkerInfo, RenumberingResult, compute_renumbering

logger = logging.getLogger(__name__)


@dataclass
class RenumberPlan:
    """Everything needed to apply (or preview) an insert-mode renumbering."""
    # DOCX marker dicts from DocxHandler.find_markers(), in document order
    markers: list[dict] = field(default_factory=list)
    # Parallel to markers: the reviewed citations resolved for each marker
    marker_resolved_map: list[list[CitationCandidate]] = field(default_factory=list)
    # Parallel to markers: the matched SentenceRecord (or None)
    marker_sentences: list[Optional[SentenceRecord]] = field(default_factory=list)
    renumber_result: RenumberingResult = field(default_factory=RenumberingResult)


def build_renumber_plan(handler, project: ProjectState) -> RenumberPlan:
    """Match DOCX markers to evidence and compute the merged renumbering.

    The marker→sentence matching is structural: markers are matched to
    sentence-marker slots in document order within each paragraph, mirroring
    the fresh-export logic.
    """
    plan = RenumberPlan()
    existing = project.existing_citations

    plan.markers = handler.find_markers()

    # paragraph_index → expanded list of (sentence, evidence, ref_index)
    para_to_markers: dict[int, list[tuple]] = defaultdict(list)
    for sent in project.sentences:
        if sent.marker_type is not None:
            ev = project.evidence_map.get(sent.id)
            count = max(sent.marker_count, 1)
            # Per-marker slots whenever the orchestrator ran independent
            # per-marker searches (SentenceRecord.searched_per_marker).
            if sent.searched_per_marker:
                for ref_idx in range(count):
                    para_to_markers[sent.paragraph_index].append((sent, ev, ref_idx))
            else:
                for _ in range(count):
                    para_to_markers[sent.paragraph_index].append((sent, ev, None))

    para_marker_counter: dict[int, int] = defaultdict(int)
    new_marker_infos: list[NewMarkerInfo] = []

    for marker_info in plan.markers:
        para_idx = marker_info['para_index']
        char_offset = marker_info['location'][0]

        expanded_list = para_to_markers.get(para_idx, [])
        marker_order = para_marker_counter[para_idx]
        para_marker_counter[para_idx] += 1

        resolved_citations: list[CitationCandidate] = []
        matching_sent = None
        if marker_order < len(expanded_list):
            matching_sent, ev, ref_index = expanded_list[marker_order]
            if ev and ev.review_decision in (ReviewDecision.ACCEPTED,
                                             ReviewDecision.MODIFIED):
                if ref_index is not None:
                    if ref_index < len(ev.selected):
                        resolved_citations = [ev.selected[ref_index]]
                else:
                    resolved_citations = list(ev.selected)
                # Placeholder slots must never enter the renumbering plan
                resolved_citations = [
                    c for c in resolved_citations if is_valid_citation(c)
                ]

        plan.marker_sentences.append(matching_sent)
        plan.marker_resolved_map.append(resolved_citations)
        new_marker_infos.append(NewMarkerInfo(
            para_index=para_idx,
            char_offset=char_offset,
            citations=resolved_citations,
        ))

    plan.renumber_result = compute_renumbering(existing, new_marker_infos)
    return plan
