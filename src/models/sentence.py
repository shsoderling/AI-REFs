"""Data models for document sentences and markers."""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class MarkerType(str, Enum):
    """Type of reference marker found in text."""
    REF = "REF"         # Single reference
    REFS = "REFS"       # Multiple references
    MULTIPLE = "REFS"   # Alias used by ranker


class SentenceRecord(BaseModel):
    """A single sentence extracted from the source document."""
    id: str = Field(default="", description="Unique sentence identifier, e.g. S001")
    paragraph_index: int = Field(default=0)
    sentence_index: int = Field(default=0)
    raw_text: str = Field(default="", description="Original text including markers")
    clean_text: str = Field(default="", description="Text with markers stripped")
    section: Optional[str] = Field(default=None, description="Document section heading")
    marker_type: Optional[MarkerType] = Field(default=None)
    marker_count: int = Field(default=0, description="Number of markers found")
    marker_types: list[MarkerType] = Field(
        default_factory=list,
        description="Type of each marker in document order (handles mixed (REF)/(REFS))",
    )
    keywords: list[str] = Field(default_factory=list, description="Extracted keywords")

    def effective_marker_types(self) -> list[MarkerType]:
        """Per-marker types, falling back to marker_type for older projects."""
        if self.marker_types:
            return list(self.marker_types)
        if self.marker_type is None:
            return []
        return [self.marker_type] * max(self.marker_count, 1)
