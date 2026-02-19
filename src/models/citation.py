"""Data models for citation candidates retrieved from PubMed / bioRxiv."""

from typing import Optional
from pydantic import BaseModel, Field


class Author(BaseModel):
    """A single author on a publication."""
    last_name: str = ""
    first_name: str = ""
    initials: str = ""

    @property
    def display(self) -> str:
        return f"{self.last_name} {self.initials}" if self.initials else self.last_name

    @property
    def display_name(self) -> str:
        """Alias used by export code."""
        return self.display


class CitationCandidate(BaseModel):
    """A candidate reference retrieved from PubMed or bioRxiv."""
    pmid: str = Field(default="", description="PubMed ID")
    doi: str = Field(default="")
    title: str = Field(default="")
    source: str = Field(default="literature", description="Origin: pubmed, europepmc, biorxiv, user_library, ...")
    authors: list[Author] = Field(default_factory=list)
    year: int = Field(default=0)
    journal: str = Field(default="")
    journal_abbrev: str = Field(default="")
    volume: str = Field(default="")
    issue: str = Field(default="")
    pages: str = Field(default="")
    abstract: str = Field(default="")
    mesh_terms: list[str] = Field(default_factory=list)
    publication_types: list[str] = Field(default_factory=list)
    is_retracted: bool = Field(default=False)
    is_review: bool = Field(default=False)

    # Retraction / erratum details
    retraction_notice: str = Field(default="")
    has_erratum: bool = Field(default=False)

    # Scoring (populated by Ranker)
    relevance_score: float = Field(default=0.0)
    recency_score: float = Field(default=0.0)
    journal_score: float = Field(default=0.0)
    composite_score: float = Field(default=0.0)
    score_rationale: str = Field(default="")
    matching_keywords: list[str] = Field(default_factory=list)

    @property
    def first_author_year(self) -> str:
        """E.g. 'Smith et al., 2023'."""
        if not self.authors:
            return str(self.year)
        first = self.authors[0].last_name
        if len(self.authors) > 2:
            return f"{first} et al., {self.year}"
        elif len(self.authors) == 2:
            return f"{first} & {self.authors[1].last_name}, {self.year}"
        return f"{first}, {self.year}"
