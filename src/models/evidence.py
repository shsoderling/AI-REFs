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


class Verdict(str, Enum):
    """The independent verifier's view of one selected paper."""
    SUPPORTS = "supports"
    PARTIAL = "partial"
    NOT_SUPPORTED = "not_supported"
    UNVERIFIED = "unverified"


class CitationVerdict(BaseModel):
    """Verdict for one selected paper, aligned by index with ``EvidenceRecord.selected``."""
    key: str = Field(default="", description="pmid, doi or title of the paper")
    verdict: Verdict = Field(default=Verdict.UNVERIFIED)
    quote: str = Field(default="", description="Verbatim supporting passage")
    quote_found: bool = Field(default=False, description="The quote occurs in the source text")
    source: str = Field(default="", description="abstract, full_text, or empty")
    reason: str = Field(default="")


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
    verdicts: list[CitationVerdict] = Field(default_factory=list,
                                            description="One per selected paper, same order")

    # Review
    review_decision: ReviewDecision = Field(default=ReviewDecision.PENDING)
    user_pmid_override: str = Field(default="")

    @property
    def is_resolved(self) -> bool:
        return self.review_decision in (ReviewDecision.ACCEPTED, ReviewDecision.MODIFIED)
