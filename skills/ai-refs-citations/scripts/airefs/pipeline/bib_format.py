"""Bibliography entry formatting (pure, GUI-free)."""

from ..models.citation import CitationCandidate
from ..models.project import CitationStyle, AUTHOR_DATE_STYLES


def format_bib_entry(citation: CitationCandidate, number: int,
                     style: CitationStyle) -> str:
    """Format a single bibliography entry.

    Uses a generic NLM-like format that works well for most styles.
    The CSL file determines in-text citation formatting; this function
    handles the bibliography list.

    DOI and PMID are always emitted when known: they are what allows a
    re-uploaded document to be parsed back into reliable identities
    (insert mode dedupes new candidates against existing entries by
    PMID/DOI).
    """
    authors = ', '.join(a.display_name for a in citation.authors[:6])
    if len(citation.authors) > 6:
        authors += ' et al.'

    base = f"{authors}. {citation.title}"
    if not base.endswith('.'):
        base += '.'
    base += f" {citation.journal_abbrev or citation.journal}."
    if citation.year:
        base += f" {citation.year}"
    if citation.volume:
        base += f";{citation.volume}"
        if citation.issue:
            base += f"({citation.issue})"
    if citation.pages:
        base += f":{citation.pages}"
    base += "."
    if citation.doi:
        base += f" doi:{citation.doi}"
    if citation.pmid:
        base += f" PMID: {citation.pmid}"

    # Numeric styles get a number prefix
    if style not in AUTHOR_DATE_STYLES:
        return f"{number}. {base}"
    return base
