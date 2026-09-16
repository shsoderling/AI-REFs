"""Build the insert-mode renumbering plan (pure, GUI-free).

Matches the document's citation markers ((REF)/(REFS) and author-suggested
citations such as ``(Smith et al. 2020)``) to their reviewed citations and
computes the merged numbering across existing + new citations.  Used by
every export path, the numbering preview, and tests.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from ..models.citation import CitationCandidate
from ..models.existing_refs import ExistingCitationMap
from ..models.project import ProjectState
from ..models.sentence import SentenceRecord
from .export_slots import (
    ACTION_CITE, ACTION_LEAVE, MarkerSlot, action_for_unmatched, collect_marker_slots,
    slot_for_docx_marker,
)
from .renumbering import (
    NewMarkerInfo, RecordCanonicaliser, RenumberingResult, compute_renumbering,
)

logger = logging.getLogger(__name__)


@dataclass
class RenumberPlan:
    """Everything needed to apply (or preview) an insert-mode renumbering."""
    # One record per paper for the whole document; the tracked path reuses it
    # so every citation of a paper resolves to the same object.
    canonicaliser: Optional["RecordCanonicaliser"] = None
    # DOCX marker dicts from DocxHandler.find_markers(), in document order
    markers: list[dict] = field(default_factory=list)
    # Parallel to markers: the reviewed citations resolved for each marker
    marker_resolved_map: list[list[CitationCandidate]] = field(default_factory=list)
    # Parallel to markers: the matched SentenceRecord (or None)
    marker_sentences: list[Optional[SentenceRecord]] = field(default_factory=list)
    # Parallel to markers: what export does with it (export_slots.ACTION_*)
    marker_actions: list[str] = field(default_factory=list)
    # Parallel to markers: the matched slot (None for a marker the pipeline never saw)
    marker_slots: list[Optional[MarkerSlot]] = field(default_factory=list)
    renumber_result: RenumberingResult = field(default_factory=RenumberingResult)

    @property
    def written_count(self) -> int:
        """Markers that become a citation or a [?] (everything not left as written)."""
        return sum(1 for a in self.marker_actions if a != ACTION_LEAVE)


def build_renumber_plan(handler, project: ProjectState,
                        seed_entries: bool = True) -> RenumberPlan:
    """Match DOCX markers to evidence and compute the merged renumbering.

    The marker→sentence matching is structural: markers are matched to
    sentence-marker slots in document order within each paragraph, and a
    slot must carry the same marker text as the DOCX marker. The DOCX is
    scanned with the marker configuration the pipeline ran with
    (``ProjectState.export_marker_config``). A project without an
    existing-citation map (fresh export) numbers the markers alone, in
    first-appearance order. Markers left as written (a skipped marker, or
    an author-suggested citation that was never confirmed) take no number.
    """
    plan = RenumberPlan()
    existing = project.existing_citations or ExistingCitationMap()

    plan.markers = handler.find_markers(project.export_marker_config)
    slots_by_para = collect_marker_slots(project)
    # One object per paper across the whole document, seeded from the records
    # it already carries, so a paper cited twice keeps one hidden record.
    canon = RecordCanonicaliser(existing)
    plan.canonicaliser = canon

    para_marker_counter: dict[int, int] = defaultdict(int)
    new_marker_infos: list[NewMarkerInfo] = []

    for marker_info in plan.markers:
        para_idx = marker_info['para_index']
        char_offset = marker_info['location'][0]
        marker_order = para_marker_counter[para_idx]
        para_marker_counter[para_idx] += 1

        slot = slot_for_docx_marker(slots_by_para, para_idx, marker_order, marker_info['text'])
        if slot is None:
            action = action_for_unmatched(marker_info['marker_type'])
            sentence = None
            resolved: list[CitationCandidate] = []
            logger.info(f"  Marker para={para_idx}[{marker_order}] {marker_info['text']!r}: "
                        f"no sentence record ({action})")
        else:
            action = slot.action
            sentence = slot.sentence
            resolved = canon.canonical_all(slot.citations) if action == ACTION_CITE else []

        plan.marker_sentences.append(sentence)
        plan.marker_resolved_map.append(resolved)
        plan.marker_actions.append(action)
        plan.marker_slots.append(slot)
        if action != ACTION_LEAVE:
            new_marker_infos.append(NewMarkerInfo(
                para_index=para_idx,
                char_offset=char_offset,
                citations=resolved,
            ))

    plan.renumber_result = compute_renumbering(existing, new_marker_infos, seed_entries)
    return plan
