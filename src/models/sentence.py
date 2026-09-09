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
    markers: list[MarkerSpec] = Field(
        default_factory=list,
        description="Every marker in this sentence, in order of appearance",
    )
    keywords: list[str] = Field(default_factory=list, description="Extracted keywords")

    @model_validator(mode="after")
    def _backfill_legacy_markers(self):
        """Project files saved before suggested markers existed carry only
        marker_type/marker_count.  Rebuild the marker list from the raw text
        using the (REF)/(REFS)-only grammar so export and review keep working."""
        if self.marker_type is not None and not self.markers and self.raw_text:
            from ..utils.markers import find_markers
            from .markers import MarkerConfig
            legacy = find_markers(self.raw_text, MarkerConfig.legacy())
            if legacy:
                self.markers = legacy
                if self.marker_count <= 0:
                    self.marker_count = len(legacy)
        return self

    @property
    def has_markers(self) -> bool:
        return bool(self.markers) or self.marker_type is not None

    @property
    def has_suggested_marker(self) -> bool:
        return any(m.kind == MarkerType.SUGGESTED for m in self.markers)

    def marker_label(self) -> str:
        """Compact label for list displays."""
        if not self.markers:
            return f"({self.marker_type.value})" if self.marker_type else ""
        if len(self.markers) == 1:
            return self.markers[0].short_label
        return f"({len(self.markers)} markers)"
