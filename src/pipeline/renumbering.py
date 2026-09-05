"""Citation renumbering algorithm for insert mode.

Walks a pre-cited document in paragraph order to assign sequential citation
numbers that incorporate both existing numbered citations and newly-resolved
(REF)/(REFS) markers.
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
    def _aliases(pmid: str, doi: str, title: str) -> list[str]:
        aliases = []
        if pmid and pmid.strip():
            aliases.append(f"pmid:{pmid.strip()}")
        if doi and doi.strip():
            aliases.append(f"doi:{doi.strip().lower()}")
        if title:
            norm = _normalize_title(title)
            if norm:
                aliases.append(f"title:{norm}")
        return aliases

    def get_or_assign(self, pmid: str = "", doi: str = "", title: str = "") -> str:
        """Return the canonical key for this identifier set, registering aliases."""
        aliases = self._aliases(pmid, doi, title)
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
        return self.get_or_assign(cand.pmid, cand.doi, cand.title)

    def key_for_existing(self, entry: ExistingBibEntry) -> str:
        # Enriched entries carry pmid/doi; bare entries may only have raw text.
        key = self.get_or_assign(entry.pmid, entry.doi, entry.title)
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


def compute_renumbering(
    existing: ExistingCitationMap,
    new_markers: list[NewMarkerInfo],
) -> RenumberingResult:
    """Compute the renumbering for a document with existing + new citations.

    Algorithm:
    - Walk paragraphs 0 .. references_heading - 1 in order
    - For each paragraph, collect all citation events:
      (a) existing in-text numbers from ``existing.in_text_citations``
      (b) new marker positions from ``new_markers``
    - Sort by character offset within the paragraph
    - Assign sequential numbers on first-occurrence (keyed by bib_key)
    - Build renumber_map and assignments
    """
    result = RenumberingResult()
    key_index = result.key_index
    current_num = 1
    key_to_number: dict[str, int] = {}

    # Index new markers by paragraph
    new_by_para: dict[int, list[NewMarkerInfo]] = {}
    for marker in new_markers:
        new_by_para.setdefault(marker.para_index, []).append(marker)

    max_para = existing.references_heading_para_idx
    if max_para < 0:
        max_para = 0  # Shouldn't happen if we're in insert mode

    for para_idx in range(max_para):
        events: list[tuple[int, str, object]] = []

        # Existing in-text citations
        for cite in existing.in_text_citations.get(para_idx, []):
            events.append((cite.char_offset, "existing", cite.number))

        # New marker citations
        for marker in new_by_para.get(para_idx, []):
            events.append((marker.char_offset, "new", marker.citations))

        # Sort by character offset (stable sort keeps insertion order for ties)
        events.sort(key=lambda e: e[0])

        for _offset, event_type, data in events:
            if event_type == "existing":
                old_num: int = data
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

            elif event_type == "new":
                citations: list[CitationCandidate] = data
                for cand in citations:
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

    result.next_number = current_num

    logger.info(
        f"Renumbering: {len(result.assignments)} total citations "
        f"({sum(1 for a in result.assignments.values() if a.is_new)} new, "
        f"{sum(1 for a in result.assignments.values() if not a.is_new)} existing)"
    )
    for old, new in sorted(result.renumber_map.items()):
        if old != new:
            logger.info(f"  Renumber: {old} -> {new}")

    return result


def _existing_bib_key(existing: ExistingCitationMap, old_num: int,
                      key_index: CitationKeyIndex) -> str:
    """Get a stable key for an existing bibliography entry."""
    entry = existing.bib_entries.get(old_num)
    if entry:
        return key_index.key_for_existing(entry)
    return f"existing_{old_num}"
