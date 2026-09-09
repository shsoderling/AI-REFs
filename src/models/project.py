"""Data model for project state, settings, and configuration."""

from enum import Enum
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field

from .sentence import SentenceRecord
from .evidence import EvidenceRecord
from .existing_refs import ExistingCitationMap
from .markers import MarkerConfig
from .embedded import TrackingReport


class CitationStyle(str, Enum):
    """Citation style options mapped to CSL files in assets/csl/."""
    # Grant-specific
    NIH_GRANT = "nih_grant"                     # NLM with PMCID — DEFAULT
    NSF_GRANT = "nsf_grant"                     # NSF grant proposals
    # Biomedical / Life Sciences
    VANCOUVER = "vancouver"                     # Vancouver (ICMJE)
    AMA = "ama"                                 # AMA 11th edition
    NLM = "nlm"                                 # National Library of Medicine
    # General Science
    APA = "apa"                                 # APA 7th edition
    CSE_AUTHOR_DATE = "cse_author_date"         # CSE Author-Date
    CSE_CITATION_SEQ = "cse_citation_seq"       # CSE Citation-Sequence
    NATURE = "nature"                           # Nature
    SCIENCE = "science"                         # Science / AAAS
    PLOS_ONE = "plos_one"                       # PLOS ONE
    CELL = "cell"                               # Cell
    ELIFE = "elife"                             # eLife
    PNAS = "pnas"                               # PNAS
    # Chemistry / Physics / Engineering
    ACS = "acs"                                 # American Chemical Society
    IEEE = "ieee"                               # IEEE
    APS = "aps"                                 # American Physical Society
    # Other
    ELSEVIER_HARVARD = "elsevier_harvard"       # Elsevier Harvard
    CHICAGO_AUTHOR_DATE = "chicago_author_date" # Chicago Author-Date


# Map each CitationStyle enum to its CSL filename and citation-format type
CSL_FILENAMES: dict["CitationStyle", str] = {
    CitationStyle.NIH_GRANT: "national-library-of-medicine-grant-proposals.csl",
    CitationStyle.NSF_GRANT: "national-science-foundation-grant-proposals.csl",
    CitationStyle.VANCOUVER: "vancouver.csl",
    CitationStyle.AMA: "american-medical-association.csl",
    CitationStyle.NLM: "national-library-of-medicine.csl",
    CitationStyle.APA: "apa.csl",
    CitationStyle.CSE_AUTHOR_DATE: "council-of-science-editors-author-date.csl",
    CitationStyle.CSE_CITATION_SEQ: "council-of-science-editors.csl",
    CitationStyle.NATURE: "nature.csl",
    CitationStyle.SCIENCE: "science.csl",
    CitationStyle.PLOS_ONE: "plos-one.csl",
    CitationStyle.CELL: "cell.csl",
    CitationStyle.ELIFE: "elife.csl",
    CitationStyle.PNAS: "pnas.csl",
    CitationStyle.ACS: "american-chemical-society.csl",
    CitationStyle.IEEE: "ieee.csl",
    CitationStyle.APS: "american-physics-society.csl",
    CitationStyle.ELSEVIER_HARVARD: "elsevier-harvard.csl",
    CitationStyle.CHICAGO_AUTHOR_DATE: "chicago-author-date.csl",
}

# Whether each style uses numeric in-text citations or author-date
AUTHOR_DATE_STYLES: set["CitationStyle"] = {
    CitationStyle.APA,
    CitationStyle.CSE_AUTHOR_DATE,
    CitationStyle.ELSEVIER_HARVARD,
    CitationStyle.CHICAGO_AUTHOR_DATE,
    CitationStyle.ELIFE,
}

# Styles that render in-text citation numbers as superscript
SUPERSCRIPT_STYLES: set["CitationStyle"] = {
    CitationStyle.NIH_GRANT,
    CitationStyle.NLM,
    CitationStyle.AMA,
    CitationStyle.NATURE,
    CitationStyle.CELL,
    CitationStyle.ACS,
    CitationStyle.CSE_CITATION_SEQ,
}


def get_csl_path(style: "CitationStyle") -> Path:
    """Return the absolute path to the CSL file for a given style."""
    csl_dir = Path(__file__).parent.parent.parent / "assets" / "csl"
    filename = CSL_FILENAMES.get(style)
    if filename:
        return csl_dir / filename
    raise ValueError(f"No CSL file mapped for style: {style}")


class PipelineStage(str, Enum):
    """Stages of the processing pipeline."""
    NOT_STARTED = "not_started"
    PARSING = "parsing"
    MARKER_LOCATION = "marker_location"
    AI_CITATION_SEARCH = "ai_citation_search"
    VERIFICATION = "verification"
    GLOBAL_QA = "global_qa"
    EXISTING_CITATION_ANALYSIS = "existing_citation_analysis"
    COMPLETE = "complete"
    PAUSED = "paused"
    ERROR = "error"


class ProjectSettings(BaseModel):
    """User-configurable settings for the processing pipeline."""
    # Citation preferences
    citation_style: CitationStyle = Field(default=CitationStyle.NIH_GRANT)
    max_refs_for_refs: int = Field(default=3, ge=2, le=10, description="Max references for (REFS) markers")

    # Author-suggested citation markers
    detect_suggested_ids: bool = Field(
        default=True,
        description="Treat (PMID: ...), (PMC...), and (doi: ...) parentheticals as citations to verify",
    )
    detect_author_year: bool = Field(
        default=True,
        description="Treat (Author et al. YEAR) parentheticals as citations to verify",
    )

    # Search preferences
    recency_bias: bool = Field(default=True, description="Prefer more recent publications")
    prefer_reviews: bool = Field(default=False, description="Prefer review articles over primary research")
    domain_inference: bool = Field(default=True, description="Auto-detect research domain from document")
    parallel_searches: int = Field(
        default=3, ge=1, le=8,
        description="Sentences searched concurrently (clamped to 1 without an NCBI API key)",
    )
    verify_citations: bool = Field(
        default=True,
        description="Independently verify each selected paper against its claim and quote the evidence",
    )
    use_full_text: bool = Field(
        default=True,
        description="Read open-access full text from Europe PMC when an abstract is not enough",
    )

    # ORCID
    orcid_id: Optional[str] = Field(default=None, description="User's ORCID for self-cite detection")

    # PubMed
    ncbi_api_key: Optional[str] = Field(default=None, description="NCBI API key for higher rate limits")
    ncbi_email: str = Field(default="", description="Email for NCBI E-utilities (required)")

    # Anthropic / Claude
    anthropic_api_key: Optional[str] = Field(default=None, description="Anthropic API key for Claude-powered citation search")
    claude_model: str = Field(
        default="newest",
        description="Claude model id for the citation agent; 'newest' means the most recently "
                    "released model on the Input tab's list (see services.model_catalog)",
    )

    # bioRxiv
    search_biorxiv: bool = Field(default=True, description="Also search bioRxiv preprints")

    # Europe PMC
    search_europepmc: bool = Field(default=True, description="Also search Europe PMC")

    # Insert mode
    enrich_existing_refs: bool = Field(
        default=True,
        description="Look up existing bibliography entries lacking PMID/DOI "
                    "on PubMed so they can be deduplicated against new finds",
    )

    # Tracked documents
    embed_citation_fields: bool = Field(
        default=True,
        description="Write each citation as a hidden Word field carrying its record, so a "
                    "later session reopens the document exactly (EndNote-style tracking)",
    )
    keep_uncited_entries: bool = Field(
        default=False,
        description="Keep bibliography entries whose citations were all deleted",
    )

    # Export safety
    min_match_ratio: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description="Insert mode refuses to rebuild the bibliography when fewer than "
                    "this fraction of parsed entries were matched to in-text citations",
    )
    allow_export_with_tracked_changes: bool = Field(
        default=False,
        description="Export a document whose citations carry pending tracked changes",
    )

    # User reference library
    reference_library_enabled: bool = Field(
        default=True,
        description="Search user-managed reference library during citation selection",
    )
    reference_library_path: Optional[str] = Field(
        default=None,
        description="SQLite DB path for the AI REFs user reference library",
    )
    prefer_user_library: bool = Field(
        default=True,
        description="Bias citation selection toward user-library entries when relevant",
    )
    max_library_results: int = Field(
        default=10, ge=3, le=50,
        description="Maximum user-library matches to return per query",
    )


PROJECT_SCHEMA_VERSION = 2


class ProjectState(BaseModel):
    """Complete state of an AI REFs project, enabling save/load/resume."""
    schema_version: int = Field(default=PROJECT_SCHEMA_VERSION)

    # Project metadata
    project_name: str = Field(default="Untitled Project")
    project_path: Optional[str] = Field(default=None, description="Path to .airefsproj file")
    created_at: str = Field(default="")
    modified_at: str = Field(default="")

    # Input document
    input_docx_path: Optional[str] = Field(default=None)
    input_docx_hash: str = Field(default="", description="SHA256 hash of input file for change detection")

    # Output paths
    output_docx_path: Optional[str] = Field(default=None)
    output_xlsx_path: Optional[str] = Field(default=None)

    # Settings
    settings: ProjectSettings = Field(default_factory=ProjectSettings)

    # Pipeline state
    current_stage: PipelineStage = Field(default=PipelineStage.NOT_STARTED)
    stages_completed: list[PipelineStage] = Field(default_factory=list)

    # Data
    sentences: list[SentenceRecord] = Field(default_factory=list)
    evidence_map: dict[str, EvidenceRecord] = Field(default_factory=dict, description="sentence_id -> EvidenceRecord")

    # Domain inference results
    inferred_domains: list[str] = Field(default_factory=list)
    document_keywords: list[str] = Field(default_factory=list)

    # Insert mode: adding references to a pre-cited document
    is_insert_mode: bool = Field(default=False, description="True when adding refs to a pre-cited document")
    existing_citations: Optional[ExistingCitationMap] = Field(default=None, description="Parsed pre-existing citations (insert mode only)")

    # Tracked document mirror (written at export; the document is the source of truth)
    doc_id: str = Field(default="", description="Identity of the tracked document, carried in its bibliography field")
    doc_tracking: Optional[TrackingReport] = Field(default=None, description="How the loaded document was read")
    record_order: list[str] = Field(default_factory=list, description="Record uuids in bibliography order at the last export")
    uncited: list[str] = Field(default_factory=list, description="Record uuids kept without a citation at the last export")
    entry_hashes: list[str] = Field(default_factory=list, description="Hashes of the bibliography entries written at the last export")
    # Marker grammar the last pipeline run used.  Export must scan the DOCX
    # with exactly this configuration, whatever the settings say now, so the
    # markers it replaces are the markers the sentences were built from.
    run_marker_config: Optional["MarkerConfig"] = Field(
        default=None,
        description="Marker detection configuration used by the last pipeline run",
    )

    @property
    def marker_config(self) -> "MarkerConfig":
        """MarkerConfig for a NEW pipeline run, derived from the current settings."""
        return MarkerConfig.from_settings(self.settings)

    @property
    def export_marker_config(self) -> "MarkerConfig":
        """MarkerConfig for export: the one the sentences were parsed with.

        Project files written before suggested markers existed have sentences
        but no recorded configuration; they were parsed with the (REF)/(REFS)
        grammar, so that is what export uses for them.
        """
        if self.run_marker_config is not None:
            return self.run_marker_config
        if self.sentences:
            return MarkerConfig.legacy()
        return self.marker_config

    @property
    def total_markers(self) -> int:
        return sum(1 for s in self.sentences if s.marker_type is not None)

    @property
    def resolved_count(self) -> int:
        return sum(1 for eid, ev in self.evidence_map.items() if ev.is_resolved)

    @property
    def all_resolved(self) -> bool:
        marked = [s for s in self.sentences if s.marker_type is not None]
        if not marked:
            return True
        return all(
            self.evidence_map.get(s.id) and self.evidence_map[s.id].is_resolved
            for s in marked
        )
