"""Data models for document sentences and markers."""

from typing import Optional
from pydantic import BaseModel, Field, model_validator

# MarkerType lives in markers.py; re-exported here so existing imports keep working.
from .markers import MarkerType, MarkerSpec  # noqa: F401


class SentenceRecord(BaseModel):
    """A single sentence extracted from the source document."""
    id: str = Field(default="", description="Unique sentence identifier, e.g. S001")
    paragraph_index: int = Field(default=0)
    sentence_index: int = Field(default=0)
    raw_text: str = Field(default="", description="Original text including markers")
    clean_text: str = Field(default="", description="Text with markers stripped")
    section: Optional[str] = Field(default=None, description="Document section heading")
    marker_type: Optional[MarkerType] = Field(
        default=None,
        description="Kind of the first marker in the sentence (None when the sentence has no marker)",
    )
    marker_count: int = Field(default=0, description="Number of markers found")
    marker_types: list[MarkerType] = Field(
        default_factory=list,
        description="Type of each marker in document order (handles mixed (REF)/(REFS))",
    )
    markers: list[MarkerSpec] = Field(
        default_factory=list,
        description="Every marker in this sentence, in order of appearance",
    )
    keywords: list[str] = Field(default_factory=list, description="Extracted keywords")

    @model_validator(mode="after")
    def _backfill_legacy_markers(self):
        """Project files saved before suggested markers existed carry only
        marker_type / marker_count / marker_types.  Rebuild the marker list
        from the raw text with the (REF)/(REFS)-only grammar so export and
        review keep working, and keep ``marker_types`` in step with it."""
        if self.marker_type is not None and not self.markers and self.raw_text:
            from ..utils.markers import find_markers
            from .markers import MarkerConfig
            legacy = find_markers(self.raw_text, MarkerConfig.legacy())
            if legacy:
                self.markers = legacy
                if self.marker_count <= 0:
                    self.marker_count = len(legacy)
        if self.markers and not self.marker_types:
            self.marker_types = [m.kind for m in self.markers]
        return self

    def effective_marker_types(self) -> list[MarkerType]:
        """Per-marker types, falling back to marker_type for older projects."""
        if self.markers:
            return [m.kind for m in self.markers]
        if self.marker_types:
            return list(self.marker_types)
        if self.marker_type is None:
            return []
        return [self.marker_type] * max(self.marker_count, 1)

    @property
    def has_markers(self) -> bool:
        return bool(self.markers) or self.marker_type is not None

    @property
    def has_suggested_marker(self) -> bool:
        return any(t == MarkerType.SUGGESTED for t in self.effective_marker_types())

    @property
    def searched_per_marker(self) -> bool:
        """True when the orchestrator handled the sentence marker by marker.

        Mixed sentences with at least one (REF) get independent per-marker
        searches, and any sentence with an author-suggested citation is
        handled per marker (each suggestion is verified on its own);
        all-(REFS) sentences keep one combined search. The export and
        renumbering code must slice ``evidence.selected`` the same way, so
        this is the single home for the rule.
        """
        types = self.effective_marker_types()
        if MarkerType.SUGGESTED in types:
            return True
        return len(types) > 1 and MarkerType.REF in types

    @property
    def slot_count(self) -> int:
        """Number of citation slots: one per marker when the sentence was
        handled marker by marker, else a single slot shared by every marker."""
        types = self.effective_marker_types()
        if not types:
            return 1
        return len(types) if self.searched_per_marker else 1

    def marker_label(self) -> str:
        """Compact label for list displays."""
        if not self.markers:
            return f"({self.marker_type.value})" if self.marker_type else ""
        if len(self.markers) == 1:
            return self.markers[0].short_label
        return f"({len(self.markers)} markers)"
