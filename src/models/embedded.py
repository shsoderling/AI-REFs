"""Models describing embedded (field-based) citation tracking in a document."""

from enum import Enum
from pydantic import BaseModel, ConfigDict, Field


class DocumentTier(str, Enum):
    TRACKED = "tracked"              # AIREFS.CITE fields present: exact, offline reopen
    LEGACY = "legacy"                # plain-text citations: regex + PubMed path
    STRIPPED = "stripped"            # project knows this document but fields are gone
    FAILED = "failed"                # analysis raised; nothing trusted
    NEWER_VERSION = "newer_version"  # payload schema newer than this app: read-only


class ReconcileIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: str = ""                   # pasted | duplicate | hand_edited | ... (Task 17)
    cid: str = ""
    message: str = ""


class TrackingReport(BaseModel):
    """How a document was read: its tier, what fields it carries and what
    the reader could not trust. Attached to ``ExistingCitationMap.tracking``."""
    model_config = ConfigDict(extra="ignore")
    tier: DocumentTier = DocumentTier.LEGACY
    doc_id: str = ""
    schema_version: int = 0
    field_count: int = 0             # AIREFS.CITE fields
    bibl_field_count: int = 0
    foreign_field_count: int = 0     # ADDIN fields from other reference managers
    fields_in_tables: int = 0        # our fields in tables or text boxes (out of flow)
    record_count: int = 0
    problems: list[str] = Field(default_factory=list)
    reconcile: list[ReconcileIssue] = Field(default_factory=list)
