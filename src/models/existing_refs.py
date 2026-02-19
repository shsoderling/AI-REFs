"""Data models for pre-existing citations in a document."""

import re
from typing import Optional
from pydantic import BaseModel, Field

from .citation import CitationCandidate


class ExistingBibEntry(BaseModel):
    """A single entry parsed from an existing References section."""
    original_number: int = Field(description="Original citation number in the document")
    raw_text: str = Field(default="", description="Full text of the bibliography entry")
    # Parsed fields (best-effort extraction)
    title: str = Field(default="")
    authors_str: str = Field(default="")
    year: int = Field(default=0)
    journal: str = Field(default="")
    doi: str = Field(default="")
    pmid: str = Field(default="")
    # If matched to a CitationCandidate (e.g. via PubMed fetch)
    matched_candidate: Optional[CitationCandidate] = Field(default=None)

    @property
    def bib_key(self) -> str:
        """Stable key for deduplication: PMID > DOI > positional fallback."""
        if self.pmid:
            return self.pmid
        if self.doi:
            return self.doi
        return f"existing_{self.original_number}"


class InTextCitation(BaseModel):
    """A single in-text citation occurrence at a specific position."""
    char_offset: int = Field(description="Character offset within the paragraph text")
    number: int = Field(description="The citation number")
    is_superscript: bool = Field(default=False)


class ExistingCitationMap(BaseModel):
    """Complete picture of pre-existing citations in a document."""
    # Parsed bibliography entries, keyed by original number
    bib_entries: dict[int, ExistingBibEntry] = Field(default_factory=dict)

    # Paragraph index of the "References" heading
    references_heading_para_idx: int = Field(default=-1)

    # In-text citation locations: paragraph_index -> list of InTextCitation
    in_text_citations: dict[int, list[InTextCitation]] = Field(default_factory=dict)

    # Highest existing citation number
    max_existing_number: int = Field(default=0)

    # Detected style properties
    detected_style_is_superscript: bool = Field(default=True)
    detected_style_is_author_date: bool = Field(default=False)

    @property
    def has_existing_citations(self) -> bool:
        return len(self.bib_entries) > 0
