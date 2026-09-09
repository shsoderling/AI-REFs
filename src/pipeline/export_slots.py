"""Decide what each citation marker becomes when the document is exported.

Export re-scans the DOCX for markers (so it always works on the real
paragraph runs) and matches them to the pipeline's sentence records by
paragraph index and order of appearance.  This module holds the Qt-free
part of that logic so it can be unit tested; ``renumber_plan`` uses it to
build the per-marker plan every export path consumes.

* :func:`collect_marker_slots` expands every marked sentence into one
  :class:`MarkerSlot` per marker, carrying the citations that belong to that
  marker and the action export should take.
* :func:`slot_for_docx_marker` picks the slot for a DOCX marker.

Actions
-------
``cite``        replace the marker text with formatted citation(s)
``unresolved``  a ``(REF)``/``(REFS)`` marker without an accepted citation:
                replaced with ``[?]`` (the historical behaviour)
``leave``       keep the original text: the user chose "Leave unchanged"
                (``ReviewDecision.SKIPPED``), or an author-suggested citation
                was never confirmed.  Suggested markers carry the author's own
                words, so they are never turned into ``[?]``.  A rejected
                (``REJECTED``) or pending ``(REF)``/``(REFS)`` marker is
                unresolved, as before.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from ..models.citation import CitationCandidate, is_valid_citation
from ..models.evidence import EvidenceRecord, ReviewDecision
from ..models.markers import MarkerSpec, MarkerType
from ..models.project import ProjectState
from ..models.sentence import SentenceRecord

ACTION_CITE = "cite"
ACTION_UNRESOLVED = "unresolved"
ACTION_LEAVE = "leave"


def is_placeholder(citation: Optional[CitationCandidate]) -> bool:
    """Review-tab placeholder rows are not real citations."""
    return citation is None or not is_valid_citation(citation)


@dataclass
class MarkerSlot:
    sentence: SentenceRecord
    marker_index: int
    marker: MarkerSpec
    evidence: Optional[EvidenceRecord]
    citations: list[CitationCandidate] = field(default_factory=list)
    action: str = ACTION_UNRESOLVED

    @property
    def is_suggested(self) -> bool:
        return self.marker.kind == MarkerType.SUGGESTED

    @property
    def is_skipped(self) -> bool:
        return self.evidence is not None and self.evidence.review_decision == ReviewDecision.SKIPPED


def marker_specs(sentence: SentenceRecord) -> list[MarkerSpec]:
    """Markers of a sentence, synthesising them for very old records if needed."""
    if sentence.markers:
        return list(sentence.markers)
    return [MarkerSpec(text=f"({kind.value})", kind=kind) for kind in sentence.effective_marker_types()]


def decide_action(marker: MarkerSpec, evidence: Optional[EvidenceRecord],
                  citations: list[CitationCandidate]) -> str:
    if evidence is not None:
        if evidence.review_decision == ReviewDecision.SKIPPED:
            return ACTION_LEAVE
        if evidence.review_decision in (ReviewDecision.ACCEPTED, ReviewDecision.MODIFIED) and citations:
            return ACTION_CITE
    return ACTION_LEAVE if marker.kind == MarkerType.SUGGESTED else ACTION_UNRESOLVED


def citations_for_marker(sentence: SentenceRecord, evidence: Optional[EvidenceRecord],
                         marker_index: int) -> list[CitationCandidate]:
    """The reviewed citations that belong to marker *marker_index*.

    A sentence handled marker by marker (``searched_per_marker``) keeps one
    block of ``evidence.selected`` per marker (``slot_sizes``); a sentence
    searched once (a lone marker, or an all-(REFS) sentence) cites its whole
    list at every marker.  Placeholder rows never count.
    """
    if evidence is None:
        return []
    if sentence.searched_per_marker:
        block = evidence.citations_for_slot(marker_index, sentence.slot_count)
    else:
        block = evidence.selected
    return [c for c in block if not is_placeholder(c)]


def collect_marker_slots(project: ProjectState) -> dict[int, list[MarkerSlot]]:
    """Expand marked sentences into per-marker slots, grouped by paragraph index.

    Within a paragraph the slots appear in document order (sentence order,
    then marker order), which is the order ``DocxHandler.find_markers``
    reports them in.
    """
    by_para: dict[int, list[MarkerSlot]] = defaultdict(list)
    for sent in project.sentences:
        specs = marker_specs(sent)
        if not specs:
            continue
        evidence = project.evidence_map.get(sent.id)
        for i, spec in enumerate(specs):
            citations = citations_for_marker(sent, evidence, i)
            by_para[sent.paragraph_index].append(MarkerSlot(
                sentence=sent,
                marker_index=i,
                marker=spec,
                evidence=evidence,
                citations=citations,
                action=decide_action(spec, evidence, citations),
            ))
    return dict(by_para)


def slot_for_docx_marker(
    slots_by_para: dict[int, list[MarkerSlot]],
    para_index: int,
    order_in_para: int,
    marker_text: Optional[str] = None,
) -> Optional[MarkerSlot]:
    """Slot for the *order_in_para*-th marker of a paragraph.

    When *marker_text* is given the slot's marker must carry the same text;
    a mismatch means the DOCX scan and the pipeline disagree (for example a
    project file from an older version) and the marker is treated as
    unmatched rather than replaced with someone else's citation.
    """
    slots = slots_by_para.get(para_index, [])
    if order_in_para >= len(slots):
        return None
    slot = slots[order_in_para]
    if marker_text is not None and slot.marker.text and slot.marker.text != marker_text:
        return None
    return slot


def action_for_unmatched(marker_kind: str) -> str:
    """A DOCX marker the pipeline never saw (e.g. inside a heading)."""
    return ACTION_LEAVE if marker_kind == MarkerType.SUGGESTED.value else ACTION_UNRESOLVED
