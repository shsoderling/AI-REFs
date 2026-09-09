"""Data models for citation markers found in a document.

A *marker* is a parenthetical in the source text that tells the pipeline
where a citation belongs.  Three kinds exist:

* ``(REF)``  -- find one supporting reference
* ``(REFS)`` -- find several supporting references
* *suggested* -- the author already named the reference(s), e.g.
  ``(PMID: 32879322)``, ``(PMC11413553, PMC3159129)``,
  ``(Battison et al. 2024)`` or ``(doi: 10.1101/2024.01.03.574066)``.
  The pipeline resolves each suggestion against the user library and the
  literature databases, then scores how well it supports the claim so the
  author can confirm or replace it.

The grammar that recognises markers lives in :mod:`src.utils.markers`;
this module only holds the data shapes so that models never import parsing
code (which would create an import cycle through ``SentenceRecord``).
"""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class MarkerType(str, Enum):
    """Type of reference marker found in text."""
    REF = "REF"                 # Single reference to be searched
    REFS = "REFS"               # Multiple references to be searched
    MULTIPLE = "REFS"           # Legacy alias (kept for old imports)
    SUGGESTED = "SUGGESTED"     # Author-suggested citation(s) to verify


class SuggestionKind(str, Enum):
    """How an author-suggested citation was written."""
    PMID = "pmid"
    PMCID = "pmcid"
    DOI = "doi"
    AUTHOR_YEAR = "author_year"


class SuggestedCitation(BaseModel):
    """One author-suggested citation token inside a marker."""
    kind: SuggestionKind
    raw: str = Field(default="", description="Token exactly as written, e.g. 'PMID: 32879322'")
    value: str = Field(default="", description="Normalised identifier: '32879322', 'PMC11413553', '10.1101/...', or 'Battison 2024'")
    # Author-year details (only for kind == AUTHOR_YEAR)
    author: str = Field(default="", description="First author last name")
    coauthor: str = Field(default="", description="Second author last name for 'A and B YEAR' forms")
    et_al: bool = Field(default=False)
    year: int = Field(default=0)
    year_suffix: str = Field(default="", description="Disambiguating letter, e.g. 'a' in 2024a")

    @property
    def label(self) -> str:
        """Short human-readable label used in the UI and in warnings."""
        if self.kind == SuggestionKind.PMID:
            return f"PMID {self.value}"
        if self.kind == SuggestionKind.PMCID:
            return self.value
        if self.kind == SuggestionKind.DOI:
            return f"doi:{self.value}"
        # author-year
        who = self.author
        if self.coauthor:
            who = f"{self.author} and {self.coauthor}"
        elif self.et_al:
            who = f"{self.author} et al."
        return f"{who} {self.year}{self.year_suffix}".strip()


class MarkerSpec(BaseModel):
    """A single citation marker located inside a sentence."""
    text: str = Field(default="", description="Exact marker text including parentheses, e.g. '(PMID: 32879322)'")
    kind: MarkerType = Field(default=MarkerType.REF)
    start: int = Field(default=0, description="Start offset of the marker within the sentence raw_text")
    end: int = Field(default=0, description="End offset (exclusive) within the sentence raw_text")
    suggestions: list[SuggestedCitation] = Field(default_factory=list)
    extra_search: int = Field(
        default=0,
        description=(
            "Additional references the author asked the AI to find alongside the "
            "suggestions: number of (REF) tokens in the marker, or -1 when a REFS "
            "token is present (meaning 'up to max refs')."
        ),
    )

    @property
    def is_suggested(self) -> bool:
        return self.kind == MarkerType.SUGGESTED

    @property
    def short_label(self) -> str:
        """Compact label for list displays (at most ~30 characters)."""
        if self.kind in (MarkerType.REF, MarkerType.REFS):
            return f"({self.kind.value})"
        text = self.text
        if len(text) > 30:
            text = text[:27] + "...)"
        return text


class MarkerConfig(BaseModel):
    """Which marker kinds the grammar should recognise.

    ``(REF)``/``(REFS)`` are always recognised.
    """
    detect_ids: bool = Field(default=True, description="Recognise PMID / PMCID / DOI suggestions")
    detect_author_year: bool = Field(default=True, description="Recognise (Author et al. YEAR) suggestions")

    @classmethod
    def legacy(cls) -> "MarkerConfig":
        """Only (REF)/(REFS), the behaviour of app versions before suggested markers."""
        return cls(detect_ids=False, detect_author_year=False)

    @classmethod
    def all_on(cls) -> "MarkerConfig":
        return cls(detect_ids=True, detect_author_year=True)

    @classmethod
    def from_settings(cls, settings: Optional[object]) -> "MarkerConfig":
        """Build from a ProjectSettings-like object (duck-typed to avoid an import cycle)."""
        if settings is None:
            return cls.all_on()
        return cls(
            detect_ids=bool(getattr(settings, "detect_suggested_ids", True)),
            detect_author_year=bool(getattr(settings, "detect_author_year", True)),
        )
