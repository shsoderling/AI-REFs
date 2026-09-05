"""Parse pre-existing citations from a document that already has references.

Detects and extracts:
- The "References" heading and bibliography entries
- In-text citation numbers (superscript and bracketed)
- Basic metadata from bibliography entries (DOI, PMID, year)

Every result carries a ``TrackingReport`` (tier, field counts, problems);
``analyze()`` never raises, a failure is reported as ``DocumentTier.FAILED``.
"""

import re
import logging
from ..models.embedded import DocumentTier, TrackingReport
from ..models.existing_refs import ExistingBibEntry, ExistingCitationMap, InTextCitation
from ..services.docx_io import DocxHandler
from .citation_numbers import expand_bracket_numbers

logger = logging.getLogger(__name__)

# Pattern for the References / Bibliography heading
REFS_HEADING_PATTERN = re.compile(
    r'^(References|Bibliography|Literature Cited|Works Cited)\s*$',
    re.IGNORECASE,
)

# Pattern for a numbered bibliography entry: "1. Author...", "1) Author...", "[1] Author..."
BIB_ENTRY_PATTERN = re.compile(r'^\[?(\d+)[.\s)\]]+\s*(.+)')

# Pattern for bracketed in-text citations: [1], [1,2,3], [1-3], [1; 3]
# Shared with the export renumbering pass \u2014 keep the two in sync by importing this.
BRACKET_CITE_PATTERN = re.compile(r'\[(\d+(?:\s*[,;\-\u2013]\s*\d+)*)\]')

# A run whose text is only digits/separators \u2014 the shape of a citation cluster.
# Anything else (Ca2+, m2, footnote text) must not be treated as a citation.
CITATION_SHAPE_PATTERN = re.compile(r'^[\d\s,;\-\u2013]+$')

# Pattern for (REF) / (REFS) markers — used to detect char offsets
MARKER_PATTERN = re.compile(r'\((REFS?)\)')


def bibliography_bounds(paragraphs, heading_idx: int) -> tuple[int, int]:
    """Body indices (first entry, last entry) of the bibliography under
    *heading_idx*, or (-1, -1) when no entry follows.

    The section is the run of blank or entry-shaped (``BIB_ENTRY_PATTERN``)
    paragraphs after the heading; it ends at the first paragraph that is
    neither, so a numbered appendix or acknowledgements after the list are
    never mistaken for entries. Trailing blank paragraphs are excluded.
    This is the one definition of where the bibliography ends: the parser
    reads entries within it and ``remove_references_section`` deletes it.
    """
    first = last = -1
    for i in range(heading_idx + 1, len(paragraphs)):
        text = paragraphs[i].text.strip()
        if not text:
            continue
        if BIB_ENTRY_PATTERN.match(text):
            if first < 0:
                first = i
            last = i
            continue
        break
    return first, last


def in_field_result(match, result_spans) -> bool:
    """True when a regex *match* over ``paragraph.text`` overlaps one of the
    paragraph's field result spans (``FieldIndex.result_spans``).

    Such text is a field's cached result, not plain citation text: the
    scanners must not report it and the writers must not rewrite it.
    Shared by the parser, ``apply_renumbering`` and the author-date converter
    so the three can never disagree on what counts as a field result.
    """
    return any(s < match.end() and match.start() < e for s, e in result_spans)


class ExistingCitationParser:
    """Analyze a DOCX for pre-existing citations and bibliography."""

    def __init__(self, handler: DocxHandler, keep_uncited: bool = False):
        self.handler = handler
        # Tracked documents: keep bibliography entries whose citations are all gone
        self.keep_uncited = keep_uncited

    def analyze(self) -> ExistingCitationMap:
        """Full analysis, reported through ``result.tracking``.

        Never raises: a failure becomes a ``DocumentTier.FAILED`` report
        whose ``problems`` carry the error, and nothing else in the map is
        trusted. Fields are counted first (ours, foreign, out of flow); a
        document with AIREFS.CITE fields goes to the tracked reader, any
        other to the plain-text (legacy) path.
        """
        result = ExistingCitationMap()
        report = TrackingReport()
        result.tracking = report
        try:
            idx = self.handler.fields
            report.field_count = idx.airefs_cite
            report.bibl_field_count = idx.airefs_bibl
            report.foreign_field_count = idx.foreign
            report.fields_in_tables = idx.out_of_flow    # tables and text boxes alike
            result.pending_tracked_changes = idx.pending_tracked_changes
            if report.foreign_field_count:
                report.problems.append(
                    f"{report.foreign_field_count} citation field(s) from another "
                    "reference manager were found. They are left untouched; "
                    "export is disabled.")
            if idx.airefs_cite:
                return self._analyze_tracked(result, idx)
            self._analyze_legacy(result)
            report.tier = DocumentTier.LEGACY
        except Exception as exc:                          # never raise: report instead
            logger.exception("Existing-citation analysis failed")
            result = ExistingCitationMap(tracking=TrackingReport(
                tier=DocumentTier.FAILED, problems=[f"{type(exc).__name__}: {exc}"]))
        return result

    def _analyze_tracked(self, result: ExistingCitationMap, idx) -> ExistingCitationMap:
        """Read a document that carries AIREFS.CITE fields: exact and offline."""
        from .field_citation_reader import build_tracked_map
        tracked = build_tracked_map(self.handler, keep_uncited=self.keep_uncited)
        # Carry over what analyze() already established about the whole document
        tracked.tracking.foreign_field_count = idx.foreign
        tracked.tracking.problems = result.tracking.problems + tracked.tracking.problems
        return tracked

    def _legacy_map(self) -> ExistingCitationMap:
        """The plain-text reading of the document, regardless of fields
        (fields are transparent to it). For tests and agreement checks."""
        result = ExistingCitationMap(tracking=TrackingReport())
        self._analyze_legacy(result)
        return result

    def _analyze_legacy(self, result: ExistingCitationMap) -> None:
        """Plain-text path: detect heading, parse bib, scan in-text numbers."""
        paragraphs = self.handler.get_paragraphs()

        # Step 1: Find the References heading
        refs_start_idx = self._find_references_heading(paragraphs)
        result.heading_para_idx_found = refs_start_idx >= 0
        if refs_start_idx < 0:
            return
        result.references_heading_para_idx = refs_start_idx

        # Step 2: Parse bibliography entries within the bounded section
        result.bibliography_span = bibliography_bounds(paragraphs, refs_start_idx)
        result.bib_entries, duplicates = self._parse_bibliography(
            paragraphs, refs_start_idx, result.bibliography_span[1])
        if result.bib_entries:
            result.max_existing_number = max(result.bib_entries.keys())
        for num in duplicates:
            result.tracking.problems.append(
                f"Duplicate bibliography number {num}: two entries carry the same "
                "number, so they cannot be told apart. Fix the numbering in Word first.")

        # Step 3: Scan body paragraphs for in-text citation numbers.
        # Only numbers that exist in the bibliography count as citations —
        # this excludes phantom matches (years, footnotes, chemical notation).
        result.in_text_citations = self._scan_in_text_citations(
            paragraphs[:refs_start_idx],
            valid_numbers=set(result.bib_entries.keys()),
        )

        # Step 4: Detect superscript vs bracket style
        result.detected_style_is_superscript = self._detect_superscript(
            paragraphs[:refs_start_idx]
        )

        logger.info(
            f"Existing citation analysis: {len(result.bib_entries)} bib entries, "
            f"refs heading at para {refs_start_idx}, "
            f"max number={result.max_existing_number}, "
            f"superscript={result.detected_style_is_superscript}"
        )

    def _find_references_heading(self, paragraphs) -> int:
        """Find the paragraph index of the References heading."""
        for i, para in enumerate(paragraphs):
            text = para.text.strip()
            if REFS_HEADING_PATTERN.match(text):
                return i
            if para.style.name.startswith('Heading') and 'reference' in text.lower():
                return i
        return -1

    def _parse_bibliography(
        self, paragraphs, start_idx: int, end_idx: int,
    ) -> tuple[dict[int, ExistingBibEntry], list[int]]:
        """Parse numbered entries between the heading and *end_idx* (inclusive).

        Returns the entries keyed by number and the numbers that occurred
        more than once (the first occurrence is kept).
        """
        entries: dict[int, ExistingBibEntry] = {}
        duplicates: list[int] = []
        for i in range(start_idx + 1, end_idx + 1):
            text = paragraphs[i].text.strip()
            if not text:
                continue
            match = BIB_ENTRY_PATTERN.match(text)
            if not match:
                continue
            num = int(match.group(1))
            if num in entries:
                if num not in duplicates:
                    duplicates.append(num)
                continue
            body = match.group(2).strip()
            entry = ExistingBibEntry(
                original_number=num,
                raw_text=text,
                body=body,
            )
            self._extract_bib_fields(entry, body)
            entries[num] = entry
        return entries, duplicates

    @staticmethod
    def _extract_bib_fields(entry: ExistingBibEntry, body: str):
        """Best-effort extraction of DOI, PMID, year from raw bib text."""
        # DOI
        doi_match = re.search(r'doi:\s*(10\.\S+)', body, re.IGNORECASE)
        if doi_match:
            entry.doi = doi_match.group(1).rstrip('.')
        # PMID
        pmid_match = re.search(r'PMID:\s*(\d+)', body)
        if pmid_match:
            entry.pmid = pmid_match.group(1)
        # Year
        year_match = re.search(r'(?:19|20)\d{2}', body)
        if year_match:
            entry.year = int(year_match.group())

    def _scan_in_text_citations(
        self, paragraphs, valid_numbers: set[int],
    ) -> dict[int, list[InTextCitation]]:
        """Scan body paragraphs for in-text citation numbers.

        Finds both superscript number runs and bracketed citations like [1,2,3].
        Skips any numbers that fall inside (REF)/(REFS) markers, superscript
        runs that are not citation-shaped (e.g. "2+" in Ca2+), numbers with
        no matching bibliography entry, and anything that belongs to a Word
        field (a run of the field, or a bracket group inside its cached
        result): field results are read through the field API, never as
        plain text.
        """
        cite_map: dict[int, list[InTextCitation]] = {}
        fields = self.handler.fields
        for idx, para in enumerate(paragraphs):
            citations: list[InTextCitation] = []

            # Determine character ranges occupied by (REF)/(REFS) markers
            marker_ranges = []
            for m in MARKER_PATTERN.finditer(para.text):
                marker_ranges.append((m.start(), m.end()))

            def _in_marker(offset: int) -> bool:
                return any(s <= offset < e for s, e in marker_ranges)

            result_spans = fields.result_spans(para)

            # Superscript runs. Every number of the run shares the run's
            # start offset; a range "3-5" expands to 3, 4, 5, and a piece
            # Word split off at a revision boundary ("3-") still reports
            # its 3 (lenient expansion) instead of vanishing.
            char_offset = 0
            for run in para.runs:
                if (run.font.superscript and not fields.in_field(run)
                        and CITATION_SHAPE_PATTERN.match(run.text or "")
                        and not _in_marker(char_offset)):
                    for number in expand_bracket_numbers(run.text, lenient=True):
                        if number in valid_numbers:
                            citations.append(InTextCitation(
                                char_offset=char_offset,
                                number=number,
                                is_superscript=True,
                            ))
                char_offset += len(run.text)

            # Bracketed citations
            for m in BRACKET_CITE_PATTERN.finditer(para.text):
                if _in_marker(m.start()) or in_field_result(m, result_spans):
                    continue
                for num in expand_bracket_numbers(m.group(1)):
                    if num in valid_numbers:
                        citations.append(InTextCitation(
                            char_offset=m.start(),
                            number=num,
                            is_superscript=False,
                        ))

            if citations:
                # Sort by position to enable sequential processing
                citations.sort(key=lambda c: (c.char_offset, c.number))
                cite_map[idx] = citations

        return cite_map

    def _detect_superscript(self, paragraphs) -> bool:
        """Detect whether the document uses superscript or bracketed citations."""
        superscript_count = 0
        bracket_count = 0
        for para in paragraphs:
            for run in para.runs:
                if (run.font.superscript and re.search(r'\d+', run.text)
                        and CITATION_SHAPE_PATTERN.match(run.text or "")):
                    superscript_count += 1
            bracket_count += len(BRACKET_CITE_PATTERN.findall(para.text))
        return superscript_count >= bracket_count
