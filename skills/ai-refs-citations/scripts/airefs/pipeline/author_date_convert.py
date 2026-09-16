"""Convert a numerically-cited document to author-date citations.

Used by insert mode when the user selects an author-date style (e.g. APA)
for a document whose existing citations are numeric ([1] or superscript).

Each existing bibliography entry gets an author-date label, preferably from
its PubMed-enriched ``matched_candidate`` (full author list), else parsed
heuristically from the entry text.  Entries that can't be labeled are
reported so the caller can fall back to numeric export.
"""

import logging
import re
from dataclasses import dataclass, field

from ..models.existing_refs import ExistingCitationMap
from .bib_format import format_bib_entry
from .citation_numbers import expand_bracket_numbers
from .existing_citation_parser import (
    BRACKET_CITE_PATTERN, CITATION_SHAPE_PATTERN, in_field_result,
)
from .existing_enrichment import parse_entry_fields
from .renumbering import RenumberingResult

logger = logging.getLogger(__name__)


@dataclass
class AuthorDateLabels:
    """Author-date label per original citation number."""
    labels: dict[int, str] = field(default_factory=dict)   # e.g. "Smith et al., 2020"
    missing: list[int] = field(default_factory=list)       # entries we couldn't label


def build_author_date_labels(existing: ExistingCitationMap) -> AuthorDateLabels:
    """Derive an author-date label for every existing bibliography entry."""
    result = AuthorDateLabels()
    for num, entry in existing.bib_entries.items():
        label = ""
        if entry.matched_candidate and entry.matched_candidate.authors:
            label = entry.matched_candidate.first_author_year
        else:
            fields = parse_entry_fields(entry.body or entry.raw_text)
            last = fields["first_author_last"]
            year = fields["year"] or entry.year
            if last and year:
                # Mimic CitationCandidate.first_author_year using the number
                # of names in the authors segment.
                n_authors = len([a for a in fields["authors"].split(",") if a.strip()])
                if n_authors > 2:
                    label = f"{last} et al., {year}"
                elif n_authors == 2:
                    second = fields["authors"].split(",")[1].strip()
                    second_last = re.match(r"([A-Z][\w'\-]+)", second)
                    if second_last:
                        label = f"{last} & {second_last.group(1)}, {year}"
                    else:
                        label = f"{last} et al., {year}"
                else:
                    label = f"{last}, {year}"

        if label:
            result.labels[num] = label
        else:
            result.missing.append(num)

    if result.missing:
        logger.warning(
            f"Author-date conversion: no author/year for entries {result.missing}")
    return result


def convert_in_text_to_author_date(handler, existing: ExistingCitationMap,
                                   labels: dict[int, str],
                                   prefix: str = "(", suffix: str = ")",
                                   delimiter: str = "; ") -> int:
    """Replace numeric in-text citations with author-date text.

    Handles bracket groups ([1], [2-4]) and superscript runs.  Superscript
    citation runs lose their superscript formatting (author-date citations
    are normal text).  Runs that belong to a Word field and bracket groups
    inside a field's cached result are left alone.  Returns the number of
    citation sites converted.
    """
    refs_start = existing.references_heading_para_idx
    converted = 0

    def _group_label(numbers: list[int]) -> str:
        parts = [labels.get(n, f"ref {n}") for n in numbers]
        # De-duplicate while preserving order (e.g. [1,1] or merged numbers)
        seen, ordered = set(), []
        for p in parts:
            if p not in seen:
                seen.add(p)
                ordered.append(p)
        return f"{prefix}{delimiter.join(ordered)}{suffix}"

    # Superscript runs first: they are simple run-text swaps. The scanner
    # skips in-field runs and expands ranges ("3-5" -> 3, 4, 5).
    for run_info in handler.find_superscript_citation_runs():
        if run_info['para_index'] >= refs_start:
            continue
        run = run_info['run']
        if not CITATION_SHAPE_PATTERN.match(run.text or ""):
            continue
        numbers = [n for n in run_info['numbers'] if n in existing.bib_entries]
        if not numbers:
            continue
        run.text = _group_label(numbers)
        run.font.superscript = None
        converted += 1

    # Bracket groups. The replacement text contains no [N] token, so a
    # sequential per-match replacement cannot cascade. Matches and result
    # spans are both taken from the paragraph before any replacement, so
    # their offsets agree.
    for para_idx, para in enumerate(handler.get_paragraphs()):
        if para_idx >= refs_start:
            break
        para_text = para.text
        if "[" not in para_text:
            continue
        result_spans = handler.fields.result_spans(para)
        for match in BRACKET_CITE_PATTERN.finditer(para_text):
            if in_field_result(match, result_spans):
                continue
            numbers = [n for n in expand_bracket_numbers(match.group(1))
                       if n in existing.bib_entries]
            if not numbers:
                continue
            handler.replace_marker_by_regex(
                para, match.group(0), _group_label(numbers), superscript=False)
            converted += 1

    logger.info(f"Converted {converted} citation sites to author-date")
    return converted


def build_author_date_bibliography(existing: ExistingCitationMap,
                                   renumber_result: RenumberingResult,
                                   style, bibliography_format: str = "style") -> list[str]:
    """Build an unnumbered, alphabetically sorted merged bibliography."""
    entries: list[tuple[str, str]] = []
    for assignment in renumber_result.assignments.values():
        if assignment.is_new and assignment.candidate:
            text = format_bib_entry(assignment.candidate, 0, style, bibliography_format)
        else:
            old_entry = existing.bib_entries.get(assignment.original_number)
            if not old_entry:
                continue
            text = old_entry.body or re.sub(
                r'^\[?\d+[.\s)\]]*\s*', '', old_entry.raw_text, count=1)
        sort_key = re.sub(r'[^a-z0-9 ]+', '', text.lower())[:80]
        entries.append((sort_key, text))

    return [text for _, text in sorted(entries)]
