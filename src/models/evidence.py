"""Data models for evidence records — linking sentences to citations."""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field

from .citation import CitationCandidate


class ConfidenceLevel(str, Enum):
    """Confidence in the citation assignment."""
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    UNRESOLVED = "Unresolved"


class ReviewDecision(str, Enum):
    """User decision on a citation assignment."""
    PENDING = "pending"
    ACCEPTED = "accepted"
    MODIFIED = "modified"
    REJECTED = "rejected"


class VerificationStatus(str, Enum):
    """Result of abstract-level verification."""
    NOT_CHECKED = "not_checked"
    VERIFIED = "verified"
    PARTIAL = "partial"
    INDIRECT = "indirect"
    WEAK = "weak"
    NOT_VERIFIED = "not_verified"
    PENDING = "pending"
    PLAUSIBLE = "plausible"
    MISMATCH = "mismatch"


class Warning(BaseModel):
    """A warning flag attached to an evidence record."""
    code: str = ""
    message: str = ""
    severity: str = Field(default="low", description="low, medium, or high")


class EvidenceRecord(BaseModel):
    """Links a sentence to its candidate and selected citations."""
    sentence_id: str = Field(default="")

    # Retrieval
    candidates: list[CitationCandidate] = Field(default_factory=list)
    search_query: str = Field(default="")
    search_result_count: int = Field(default=0)
    retrieval_error: str = Field(default="")

    # Ranking / selection
    selected: list[CitationCandidate] = Field(default_factory=list)
    confidence_level: ConfidenceLevel = Field(default=ConfidenceLevel.UNRESOLVED)
    confidence_score: float = Field(default=0.0)
    confidence_rationale: str = Field(default="")

    # Verification
    verification_status: VerificationStatus = Field(default=VerificationStatus.NOT_CHECKED)
    abstract_snippets: list[str] = Field(default_factory=list)
    warnings: list[Warning] = Field(default_factory=list)

    # Marker slots: how many entries of ``selected`` belong to each marker of
    # the sentence, in marker order.  Empty for records written by older app
    # versions; see ``slot_ranges`` for how those are interpreted.
    slot_sizes: list[int] = Field(default_factory=list)

    # Review
    review_decision: ReviewDecision = Field(default=ReviewDecision.PENDING)
    user_pmid_override: str = Field(default="")

    @property
    def is_resolved(self) -> bool:
        """True once the user has made a decision (accept, modify, or skip)."""
        return self.review_decision in (
            ReviewDecision.ACCEPTED, ReviewDecision.MODIFIED, ReviewDecision.REJECTED,
        )

    @property
    def is_skipped(self) -> bool:
        return self.review_decision == ReviewDecision.REJECTED

    # ── Slot helpers ────────────────────────────────────────────────────

    def slot_ranges(self, n_slots: int) -> list[tuple[int, int]]:
        """Return ``(start, end)`` index ranges into ``selected`` for each marker slot.

        When ``slot_sizes`` is consistent with ``selected`` it is used as-is.
        Otherwise the layout is derived the way older versions laid it out:
        a single marker owns every selected citation; several markers own one
        citation each in order (later markers are empty when fewer were found).
        """
        n_slots = max(int(n_slots), 1)
        total = len(self.selected)
        sizes = [max(int(s), 0) for s in self.slot_sizes]
        if len(sizes) != n_slots or sum(sizes) != total:
            if n_slots == 1:
                sizes = [total]
            else:
                sizes = [1 if i < total else 0 for i in range(n_slots)]
                # Any surplus citations belong to the last slot that has one
                surplus = total - sum(sizes)
                if surplus > 0:
                    sizes[min(total, n_slots) - 1] += surplus
        ranges = []
        offset = 0
        for size in sizes:
            ranges.append((offset, offset + size))
            offset += size
        return ranges

    def normalize_slots(self, n_slots: int) -> list[int]:
        """Make ``slot_sizes`` explicit and consistent; returns the sizes."""
        ranges = self.slot_ranges(n_slots)
        self.slot_sizes = [end - start for start, end in ranges]
        return self.slot_sizes

    def citations_for_slot(self, slot: int, n_slots: int) -> list[CitationCandidate]:
        ranges = self.slot_ranges(n_slots)
        if slot < 0 or slot >= len(ranges):
            return []
        start, end = ranges[slot]
        return self.selected[start:end]

    def slot_of_index(self, index: int, n_slots: int) -> int:
        """Which marker slot owns ``selected[index]`` (-1 when out of range)."""
        for slot, (start, end) in enumerate(self.slot_ranges(n_slots)):
            if start <= index < end:
                return slot
        return -1

    def remove_selected(self, index: int, n_slots: int) -> Optional[CitationCandidate]:
        """Pop ``selected[index]`` keeping ``slot_sizes`` consistent."""
        if index < 0 or index >= len(self.selected):
            return None
        self.normalize_slots(n_slots)
        slot = self.slot_of_index(index, n_slots)
        removed = self.selected.pop(index)
        if 0 <= slot < len(self.slot_sizes):
            self.slot_sizes[slot] = max(self.slot_sizes[slot] - 1, 0)
        return removed

    def insert_into_slot(self, slot: int, citation: CitationCandidate, n_slots: int) -> int:
        """Append *citation* to the given marker slot; returns its index in ``selected``."""
        self.normalize_slots(n_slots)
        slot = max(0, min(slot, len(self.slot_sizes) - 1))
        ranges = self.slot_ranges(n_slots)
        index = ranges[slot][1]
        self.selected.insert(index, citation)
        self.slot_sizes[slot] += 1
        return index
