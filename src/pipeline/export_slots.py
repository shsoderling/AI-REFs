"""Decide what each citation marker becomes when the document is exported.

Export re-scans the DOCX for markers (so it always works on the real
paragraph runs) and matches them to the pipeline's sentence records by
paragraph index and order of appearance.  This module holds the Qt-free
part of that logic so it can be unit tested:

* :func:`collect_marker_slots` expands every marked sentence into one
  :class:`MarkerSlot` per marker, carrying the citations that belong to that
  marker and the action export should take.
* :func:`slot_for_docx_marker` picks the slot for a DOCX marker.

Actions
-------
``cite``        replace the marker text with formatted citation(s)
``unresolved``  a ``(REF)``/``(REFS)`` marker without an accepted citation:
                replaced with ``[?]`` (the historical behaviour)
``leave``       keep the original text: the user skipped the marker, or an
                author-suggested citation was never confirmed.  Suggested
                markers carry the author's own words, so they are never turned
                into ``[?]``.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from ..models.citation import CitationCandidate
from ..models.evidence import EvidenceRecord, ReviewDecision
from ..models.markers import MarkerSpec, MarkerType
from ..models.project import ProjectState
from ..models.sentence import SentenceRecord

PLACEHOLDER_TITLE_PREFIX = "(No citation found"

ACTION_CITE = "cite"
ACTION_UNRESOLVED = "unresolved"
ACTION_LEAVE = "leave"


def is_placeholder(citation: Optional[CitationCandidate]) -> bool:
    """Review-tab placeholder rows are not real citations."""
    if citation is None:
        return True
    if not (citation.title or citation.pmid or citation.doi):
        return True
    return citation.title.startswith(PLACEHOLDER_TITLE_PREFIX)


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


@dataclass
class ExportStats:
    """Counts shown to the user after export."""
    cited: int = 0
    unresolved: int = 0        # (REF)/(REFS) replaced with [?]
    left_unverified: int = 0   # suggested markers kept as written (not confirmed)
    skipped: int = 0           # markers the user chose to leave unchanged
    unmatched: int = 0         # DOCX markers with no sentence record

    def summary(self) -> str:
        parts = [f"{self.cited} marker(s) replaced with citations"]
        if self.unresolved:
            parts.append(f"{self.unresolved} unresolved (REF)/(REFS) marker(s) written as [?]")
        if self.left_unverified:
            parts.append(
                f"{self.left_unverified} author-suggested citation(s) left unverified; "
                "their text is unchanged"
            )
        if self.skipped:
            parts.append(f"{self.skipped} marker(s) left unchanged as you requested")
        if self.unmatched:
            parts.append(f"{self.unmatched} marker(s) not processed by the pipeline")
        return "\n".join(parts)


def marker_specs(sentence: SentenceRecord) -> list[MarkerSpec]:
    """Markers of a sentence, synthesising them for very old records if needed."""
    if sentence.markers:
        return list(sentence.markers)
    if sentence.marker_type is None:
        return []
    count = max(sentence.marker_count, 1)
    kind = sentence.marker_type
    return [MarkerSpec(text=f"({kind.value})", kind=kind) for _ in range(count)]


def decide_action(marker: MarkerSpec, evidence: Optional[EvidenceRecord],
                  citations: list[CitationCandidate]) -> str:
    if evidence is not None:
        if evidence.review_decision == ReviewDecision.REJECTED:
            return ACTION_LEAVE
        if evidence.review_decision in (ReviewDecision.ACCEPTED, ReviewDecision.MODIFIED) and citations:
            return ACTION_CITE
    return ACTION_LEAVE if marker.kind == MarkerType.SUGGESTED else ACTION_UNRESOLVED


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
        n = len(specs)
        # Records from before slot bookkeeping existed: a (REFS)-led sentence
        # with several markers cited the whole list at every marker.
        legacy_refs_layout = (
            evidence is not None and not evidence.slot_sizes and n > 1
            and specs[0].kind == MarkerType.REFS
        )
        for i, spec in enumerate(specs):
            citations: list[CitationCandidate] = []
            if evidence is not None:
                block = evidence.selected if legacy_refs_layout else evidence.citations_for_slot(i, n)
                citations = [c for c in block if not is_placeholder(c)]
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


def record_action(stats: ExportStats, slot: Optional[MarkerSlot], action: str):
    if slot is None:
        stats.unmatched += 1
        return
    if action == ACTION_CITE:
        stats.cited += 1
    elif action == ACTION_UNRESOLVED:
        stats.unresolved += 1
    elif slot.evidence is not None and slot.evidence.review_decision == ReviewDecision.REJECTED:
        stats.skipped += 1
    elif slot.is_suggested:
        stats.left_unverified += 1
    else:
        stats.skipped += 1
