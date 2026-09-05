"""Data models for pre-existing citations in a document."""

import re
from typing import Optional
from pydantic import BaseModel, Field

from .citation import CitationCandidate
from .embedded import TrackingReport


class ExistingBibEntry(BaseModel):
    """A single entry parsed from an existing References section."""
    original_number: int = Field(description="Original citation number in the document")
    raw_text: str = Field(default="", description="Full text of the bibliography entry")
    body: str = Field(default="", description="Entry text with the leading number stripped")
    # Parsed fields (best-effort extraction)
    title: str = Field(default="")
    authors_str: str = Field(default="")
    year: int = Field(default=0)
    journal: str = Field(default="")
    doi: str = Field(default="")
    pmid: str = Field(default="")
    # If matched to a CitationCandidate (e.g. via PubMed fetch, or read from a field)
    matched_candidate: Optional[CitationCandidate] = Field(default=None)
    # Tracked documents: identity and record read from the citation fields
    record_uuid: str = Field(default="")
    item: dict = Field(default_factory=dict, description="Raw CSL-JSON item from the field")
    uris: list[str] = Field(default_factory=list)
    is_uncited: bool = Field(default=False, description="Kept in the bibliography without a citation")
    entry_hash: str = Field(default="", description="Hash of the entry text at the last export")


class InTextCitation(BaseModel):
    """A single in-text citation occurrence at a specific position."""
    char_offset: int = Field(description="Character offset within the paragraph text")
    number: int = Field(description="The citation number (0 for an unresolved field)")
    is_superscript: bool = Field(default=False)
    # Tracked documents: which field and record this occurrence belongs to
    cid: str = Field(default="", description="Cluster (field) id; shared by the items of one field")
    record_uuid: str = Field(default="")
    cluster_index: int = Field(default=0, description="Position of the item within its field")
    source: str = Field(default="text", description="'field' (read from a field) or 'text' (regex)")
    user_edited: bool = Field(default=False, description="The field's visible text was edited by hand")
    unresolved: bool = Field(default=False, description="A [?] field still waiting for a citation")


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

    # How the document was read: tier, field counts, problems.
    # None only before analyze() has run.
    tracking: Optional[TrackingReport] = Field(default=None)

    # A citation field has a run under a tracked insertion, deletion or move
    pending_tracked_changes: bool = Field(default=False)

    # Body-paragraph indices (first, last) spanned by the AIREFS.BIBL field;
    # (-1, -1) when there is none (legacy documents, missing field)
    bibliography_span: tuple[int, int] = Field(default=(-1, -1))

    # False when analysis ran but found no References heading
    heading_para_idx_found: bool = Field(default=True)

    @property
    def has_existing_citations(self) -> bool:
        return len(self.bib_entries) > 0

    @property
    def body_end_para_idx(self) -> int:
        """First body paragraph that is not text to search for markers: the
        References heading, else the start of the bibliography field, else
        -1 (no bibliography: the whole document is body)."""
        if self.references_heading_para_idx >= 0:
            return self.references_heading_para_idx
        if self.bibliography_span[0] >= 0:
            return self.bibliography_span[0]
        return -1
