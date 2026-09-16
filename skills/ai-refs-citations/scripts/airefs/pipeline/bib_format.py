"""Bibliography entry formatting (pure, GUI-free)."""

import re

from ..models.citation import CitationCandidate
from ..models.project import CitationStyle, AUTHOR_DATE_STYLES
from .csl_bibliography import render_entry


def format_bib_entry(citation: CitationCandidate, number: int, style: CitationStyle,
                     bibliography_format: str = "style") -> str:
    """Format a single bibliography entry.

    ``style`` (the default) uses the citation style's own CSL
    ``<bibliography>`` rules, so the reference list matches the journal or
    funder the user picked; ``style_with_ids`` appends the DOI and PMID when
    the style leaves them out, so a document that later loses its hidden
    fields can still be matched back to real records; ``nlm`` keeps the
    generic entry every style shared before, which is what documents from
    earlier versions carry.  A style that cannot be rendered falls back to
    ``nlm`` on its own.
    """
    if bibliography_format in ("style", "style_with_ids"):
        rendered = render_entry(citation, number, style)
        if rendered:
            if bibliography_format == "style_with_ids":
                rendered = _with_identifiers(rendered, citation)
            return rendered
    return format_nlm_entry(citation, number, style)


def _with_identifiers(entry: str, citation: CitationCandidate) -> str:
    """The entry plus whichever identifiers the style did not print.

    "Printed" means in a form the entry reader recognises: several styles give
    the DOI as a URL, which reads well but is not what a stripped document is
    parsed back with, so the plain ``doi:`` form is added alongside it.
    """
    readable_doi = rf"(?:doi:\s*|https?://(?:dx\.)?doi\.org/){re.escape(citation.doi)}"
    if citation.doi and not re.search(readable_doi, entry, re.IGNORECASE):
        entry = entry.rstrip() + f" doi:{citation.doi}"
    if citation.pmid and not re.search(rf"PMID:\s*{re.escape(citation.pmid)}", entry, re.IGNORECASE):
        entry = entry.rstrip() + f" PMID: {citation.pmid}"
    return entry


def format_nlm_entry(citation: CitationCandidate, number: int,
                     style: CitationStyle) -> str:
    """A generic NLM-like entry, whatever the style.

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
