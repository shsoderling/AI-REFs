"""Citation renumbering algorithm for insert mode.

One numbering engine fed by an ordered stream of citation events.
``build_events`` turns a pre-cited document's existing in-text numbers and
its newly-resolved (REF)/(REFS) markers into ``CitationEvent`` objects in
document order; ``compute_renumbering_from_events`` assigns sequential
numbers on first occurrence and seeds an entry for every parsed
bibliography entry that no event cited, so a partial parse never deletes
entries.  Tracked documents (Phase 1) only swap the event source.
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

from ..models.existing_refs import ExistingCitationMap, ExistingBibEntry
from ..models.citation import CitationCandidate

logger = logging.getLogger(__name__)


def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, and truncate for fuzzy title identity."""
    return re.sub(r'[^a-z0-9]+', ' ', title.lower()).strip()[:60]


class CitationKeyIndex:
    """Resolves any combination of (pmid, doi, title) to one canonical key.

    The same paper can surface with different identifier subsets (PMID from
    PubMed, DOI-only from bioRxiv, title-only from an old bibliography).
    Registering every identifier as an alias of one canonical key guarantees
    a single bibliography number per paper regardless of how it was found.
    """

    def __init__(self):
        self._alias_to_key: dict[str, str] = {}

    @staticmethod
    def _aliases(pmid: str, doi: str, title: str, record_uuid: str = "") -> list[str]:
        aliases = []
        if record_uuid and record_uuid.strip():
            aliases.append(f"uuid:{record_uuid.strip()}")
        if pmid and pmid.strip():
            aliases.append(f"pmid:{pmid.strip()}")
        if doi and doi.strip():
            aliases.append(f"doi:{doi.strip().lower()}")
        if title:
            norm = _normalize_title(title)
            if norm:
                aliases.append(f"title:{norm}")
        return aliases

    def get_or_assign(self, pmid: str = "", doi: str = "", title: str = "",
                      record_uuid: str = "") -> str:
        """Return the canonical key for this identifier set, registering aliases.

        A record uuid (tracked documents) is the strongest alias and comes
        first; PMID, DOI and normalised title follow, so a record found again
        through any of them keeps one number.
        """
        aliases = self._aliases(pmid, doi, title, record_uuid)
        if not aliases:
            return ""
        canonical = next(
            (self._alias_to_key[a] for a in aliases if a in self._alias_to_key),
            aliases[0],
        )
        for a in aliases:
            self._alias_to_key.setdefault(a, canonical)
        return canonical

    def key_for_candidate(self, cand: CitationCandidate) -> str:
        return self.get_or_assign(cand.pmid, cand.doi, cand.title, cand.record_uuid)

    def key_for_existing(self, entry: ExistingBibEntry) -> str:
        # Tracked entries carry a record uuid; enriched entries pmid/doi;
        # bare entries may only have raw text.
        key = self.get_or_assign(entry.pmid, entry.doi, entry.title, entry.record_uuid)
        return key or f"existing_{entry.original_number}"


@dataclass
class CitationAssignment:
    """A citation that has been assigned a final number."""
    final_number: int
    is_new: bool  # True if from a resolved (REF)/(REFS) marker
    original_number: int = 0  # For existing citations only
    candidate: Optional[CitationCandidate] = None  # For new citations only
    bib_key: str = ""


@dataclass
class RenumberingResult:
    """Complete output of the renumbering algorithm."""
    # old_number -> new_number for all existing citations
    renumber_map: dict[int, int] = field(default_factory=dict)
    # final_number -> CitationAssignment
    assignments: dict[int, CitationAssignment] = field(default_factory=dict)
    # Next available number after all assignments
    next_number: int = 1
    # Original numbers of parsed bibliography entries that no citation event
    # referenced and that were therefore appended after the cited ones
    # (in original-number order), so callers can report them.
    seeded_uncited: list[int] = field(default_factory=list)
    # The identity index used during numbering — callers must resolve
    # candidates through this same instance so aliases stay consistent.
    key_index: CitationKeyIndex = field(default_factory=CitationKeyIndex)

    def number_for_candidate(self, cand: CitationCandidate) -> Optional[int]:
        """Final number assigned to a candidate, or None if it has none."""
        bib_key = self.key_index.key_for_candidate(cand)
        for num, assn in self.assignments.items():
            if assn.bib_key == bib_key:
                return num
        return None


@dataclass
class NewMarkerInfo:
    """A new (REF)/(REFS) marker with its resolved citations."""
    para_index: int
    char_offset: int
    citations: list[CitationCandidate]


@dataclass
class CitationEvent:
    """One citation site in document order.

    ``kind`` is ``'existing'`` for a number already in the text (``number``
    holds it) or ``'new'`` for a resolved marker (``citations`` holds its
    candidates).
    """
    para_index: int
    char_offset: int
    kind: str                         # 'existing' | 'new'
    number: int = 0                   # existing: the in-text number
    citations: list[CitationCandidate] = field(default_factory=list)  # new: resolved candidates
    record_uuid: str = ""             # tracked docs (Phase 1)


# Sort key for events at the same offset: an existing number precedes a new
# marker, matching the order the paragraph walk always used.
_EVENT_ORDER = {'existing': 0, 'new': 1}


def build_events(
    existing: ExistingCitationMap,
    new_markers: list[NewMarkerInfo],
) -> list[CitationEvent]:
    """Legacy event source: existing in-text numbers plus new markers.

    Existing numbers count only before the References heading (everywhere
    when no heading was found); a new marker counts wherever it is -- a
    marker after the heading is a real citation site.  Events are ordered
    by (paragraph, offset), existing before new on ties; citations that
    share an offset (one bracket group or superscript run) keep the order
    the parser reported them in.
    """
    events: list[CitationEvent] = []
    heading = existing.references_heading_para_idx
    for para_idx, cites in existing.in_text_citations.items():
        if heading >= 0 and para_idx >= heading:
            continue
        for c in cites:
            events.append(CitationEvent(
                para_idx, c.char_offset, 'existing', number=c.number,
                record_uuid=getattr(c, 'record_uuid', ''),
            ))
    for m in new_markers:
        events.append(CitationEvent(
            m.para_index, m.char_offset, 'new', citations=list(m.citations)))
    events.sort(key=lambda e: (e.para_index, e.char_offset, _EVENT_ORDER[e.kind]))
    return events


def compute_renumbering_from_events(
    existing: ExistingCitationMap,
    events: list[CitationEvent],
    seed_entries: bool = True,
) -> RenumberingResult:
    """Assign sequential numbers to an ordered stream of citation events.

    - Each paper gets one number on its first occurrence (keyed by bib_key
      through the result's ``CitationKeyIndex``), whether it arrives as an
      existing number or as a resolved candidate.
    - ``renumber_map`` records old -> new for every existing number seen.
    - With ``seed_entries``, every parsed bibliography entry that no event
      referenced is appended afterwards in original-number order (an
      uncited entry that is the same paper as a numbered one just maps to
      that number), and listed in ``seeded_uncited``.
    """
    result = RenumberingResult()
    key_index = result.key_index
    current_num = 1
    key_to_number: dict[str, int] = {}

    for event in events:
        if event.kind == 'existing':
            old_num = event.number
            bib_key = _existing_bib_key(existing, old_num, key_index)
            if bib_key not in key_to_number:
                key_to_number[bib_key] = current_num
                result.assignments[current_num] = CitationAssignment(
                    final_number=current_num,
                    is_new=False,
                    original_number=old_num,
                    bib_key=bib_key,
                )
                current_num += 1
            # Record the mapping (even for repeated occurrences)
            result.renumber_map[old_num] = key_to_number[bib_key]

        elif event.kind == 'new':
            for cand in event.citations:
                bib_key = key_index.key_for_candidate(cand)
                if bib_key not in key_to_number:
                    key_to_number[bib_key] = current_num
                    result.assignments[current_num] = CitationAssignment(
                        final_number=current_num,
                        is_new=True,
                        candidate=cand,
                        bib_key=bib_key,
                    )
                    current_num += 1

        else:
            raise ValueError(f"Unknown citation event kind: {event.kind!r}")

    if seed_entries:
        for old_num in sorted(existing.bib_entries):
            bib_key = _existing_bib_key(existing, old_num, key_index)
            if bib_key in key_to_number:
                # Already numbered: cited above, or the same paper as a
                # numbered entry (then it merges instead of being seeded).
                result.renumber_map.setdefault(old_num, key_to_number[bib_key])
                continue
            key_to_number[bib_key] = current_num
            result.assignments[current_num] = CitationAssignment(
                final_number=current_num,
                is_new=False,
                original_number=old_num,
                bib_key=bib_key,
            )
            result.renumber_map[old_num] = current_num
            result.seeded_uncited.append(old_num)
            current_num += 1

    result.next_number = current_num

    logger.info(
        f"Renumbering: {len(result.assignments)} total citations "
        f"({sum(1 for a in result.assignments.values() if a.is_new)} new, "
        f"{sum(1 for a in result.assignments.values() if not a.is_new)} existing)"
    )
    for old, new in sorted(result.renumber_map.items()):
        if old != new:
            logger.info(f"  Renumber: {old} -> {new}")
    if result.seeded_uncited:
        logger.info(
            f"  Seeded {len(result.seeded_uncited)} uncited bibliography "
            f"entries: {result.seeded_uncited}"
        )

    return result


def compute_renumbering(
    existing: ExistingCitationMap,
    new_markers: list[NewMarkerInfo],
    seed_entries: bool = True,
) -> RenumberingResult:
    """Compute the renumbering for a document with existing + new citations.

    ``build_events`` orders the existing in-text numbers and the new markers
    by document position; ``compute_renumbering_from_events`` numbers them
    and, unless ``seed_entries`` is off, keeps every parsed bibliography
    entry that nothing cited.
    """
    return compute_renumbering_from_events(
        existing, build_events(existing, new_markers), seed_entries)


def _existing_bib_key(existing: ExistingCitationMap, old_num: int,
                      key_index: CitationKeyIndex) -> str:
    """Get a stable key for an existing bibliography entry."""
    entry = existing.bib_entries.get(old_num)
    if entry:
        return key_index.key_for_existing(entry)
    return f"existing_{old_num}"
