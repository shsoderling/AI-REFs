"""DOCX document I/O: reading, marker detection, replacement, and bibliography."""

import copy
import re
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Optional

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

from .docx_fields import FieldIndex, run_ancestry

logger = logging.getLogger(__name__)

MARKER_PATTERN = re.compile(r'\((REFS?)\)')

W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
MC_NS = 'http://schemas.openxmlformats.org/markup-compatibility/2006'


def _child_text(child) -> str:
    """Text one child of a ``w:r`` contributes to ``paragraph.text``, by
    python-docx's ``CT_R.text`` rules.

    '' for everything that renders without text -- page and column breaks,
    ``w:sym``, drawings, footnote references, ``w:lastRenderedPageBreak`` --
    and for field code (``w:instrText``) and deleted text (``w:delText``).
    """
    tag = child.tag
    if tag == f'{{{W_NS}}}t':
        return child.text or ''
    if tag in (f'{{{W_NS}}}tab', f'{{{W_NS}}}ptab'):
        return '\t'
    if tag == f'{{{W_NS}}}br':
        # Only text-wrapping line breaks are text; page and column
        # breaks are invisible to paragraph.text.
        if child.get(f'{{{W_NS}}}type', 'textWrapping') == 'textWrapping':
            return '\n'
        return ''
    if tag == f'{{{W_NS}}}cr':
        return '\n'
    if tag == f'{{{W_NS}}}noBreakHyphen':
        return '-'
    return ''


def run_text(r_elem) -> str:
    """Visible text of a ``w:r`` element, matching python-docx's ``CT_R.text``.

    Field code (``w:instrText``) and deleted text (``w:delText``) contribute
    nothing, exactly as in ``paragraph.text``. Works on any lxml ``w:r``,
    including nested ones that python-docx's ``Run`` wrapper never exposes.
    """
    return ''.join(_child_text(child) for child in r_elem)


def iter_text_runs(paragraph) -> list:
    """Runs that contribute to ``paragraph.text``, in order.

    python-docx computes ``paragraph.text`` from ``w:r | w:hyperlink`` children,
    so this is exactly ``./w:r | ./w:hyperlink/w:r``. Use it (and only it) for
    character-offset arithmetic; a test asserts equality with paragraph.text.
    """
    return paragraph._p.xpath('./w:r | ./w:hyperlink/w:r')


@dataclass
class RunInfo:
    elem: object            # the w:r element
    deleted: bool           # under w:del / w:moveFrom: gone in the final view
    inserted: bool          # under w:ins / w:moveTo: present in the final view


def iter_all_runs(paragraph):
    """Every ``w:r`` under the paragraph except ``mc:Fallback`` duplicates.

    Reaches runs nested in hyperlinks, tracked changes, content controls and
    text boxes. For field awareness only -- never for character offsets.

    ``deleted`` / ``inserted`` follow final-view Track Changes semantics and
    come from ``docx_fields.run_ancestry``, the same helper the field reader
    uses, so a moved-from run (which keeps ``w:t``, unlike ``w:delText``) is
    flagged deleted here exactly as its field is in ``FieldIndex``.
    """
    p_elem = paragraph._p
    for r in p_elem.iter(f'{{{W_NS}}}r'):
        a = run_ancestry(r, p_elem)
        if not a.skip:
            yield RunInfo(elem=r, deleted=a.deleted, inserted=a.inserted)


class FieldBoundaryError(RuntimeError):
    """Raised when an edit would split or rewrite runs that belong to a field."""


class MarkerSpan(NamedTuple):
    """Where a marker sits among a paragraph's text runs (``_locate_span``)."""
    runs: list          # iter_text_runs(paragraph)
    first: int          # index in runs of the first run the marker touches
    first_off: int      # marker start as an offset into runs[first]
    last: int           # index in runs of the last run the marker touches
    last_off: int       # marker end as an offset into runs[last]


class DocxHandler:
    """Read and manipulate DOCX documents for reference insertion."""

    def __init__(self, path: str):
        self.path = path
        self.doc = Document(path)
        self._fields: Optional[FieldIndex] = None

    @property
    def fields(self) -> FieldIndex:
        """Field membership for the current document tree, built on demand.

        Keyed on run element identity, so every method that adds or removes
        a field run calls :meth:`invalidate_fields` afterwards. Building it
        walks the whole body, so an edit that provably leaves every field
        run in place (``_split_and_emit``) keeps the index instead.
        """
        if self._fields is None:
            self._fields = FieldIndex(self.doc)
        return self._fields

    def invalidate_fields(self):
        """Forget the cached :attr:`fields`; call after any structural edit."""
        self._fields = None

    def get_paragraphs(self) -> list:
        """Return all paragraphs in the document."""
        return self.doc.paragraphs

    def get_full_text(self) -> str:
        """Return full document text."""
        return "\n".join(p.text for p in self.doc.paragraphs)

    def find_markers(self) -> list[dict]:
        """Find all (REF) and (REFS) markers in the document.

        Returns list of dicts with keys:
            paragraph, para_index, location, marker_type,
            full_paragraph_text, marker_order_in_para.
        """
        markers = []
        for para_idx, para in enumerate(self.doc.paragraphs):
            text = para.text
            marker_in_para = 0
            for match in MARKER_PATTERN.finditer(text):
                markers.append({
                    'paragraph': para,
                    'para_index': para_idx,
                    'location': match.span(),
                    'marker_type': match.group(1),
                    'full_paragraph_text': text,
                    'marker_order_in_para': marker_in_para,
                })
                marker_in_para += 1
        logger.info(f"Found {len(markers)} markers in document")
        return markers

    def replace_marker_by_regex(self, paragraph, marker_text: str, replacement: str,
                                superscript: Optional[bool] = False):
        """Replace the first occurrence of *marker_text* in *paragraph*.

        Locates the marker over the text runs, then replaces the touched runs
        with up to three new ones (before | citation | after). The before- and
        after-runs keep their neighbours' ``w:rPr`` and every content child
        outside the marker (tabs, breaks, symbols, drawings... included, as
        the elements they are); the citation run is one ``w:t`` under the
        first touched run's ``w:rPr``. Every other run is untouched.

        *superscript* is tri-state. True: the citation run is superscript and
        the after-run loses any superscript (a fresh superscript citation).
        False: the citation run carries no vertical alignment (a fresh
        bracket citation). None: the citation run keeps the marker run's
        vertical alignment -- for rewriting an existing citation in place,
        where a superscript ``[3]`` must stay superscript.

        Returns the new citation ``w:r`` element, or None when the marker is
        not in the paragraph. Raises :class:`FieldBoundaryError` rather than
        rewriting a run that belongs to a field.
        """
        span = self._locate_span(paragraph, marker_text)
        if span is None:
            return None
        return self._split_and_emit(span, replacement, superscript)

    def _locate_span(self, paragraph, marker_text: str) -> Optional[MarkerSpan]:
        """Where the first *marker_text* sits among ``iter_text_runs(paragraph)``.

        Offsets are computed over the very runs that make up ``paragraph.text``,
        so a marker present in the text is always found. Raises
        :class:`FieldBoundaryError` if any touched run belongs to a field: a
        cached field result must be rewritten through the field API instead.
        """
        full_text = paragraph.text
        start = full_text.find(marker_text)
        if start < 0:
            return None
        end = start + len(marker_text)
        runs = iter_text_runs(paragraph)
        first = last = None
        offset = 0
        for i, r in enumerate(runs):
            run_start, run_end = offset, offset + len(run_text(r))
            if run_start < end and run_end > start:
                if first is None:
                    first = (i, start - run_start)
                last = (i, end - run_start)
            offset = run_end
        if first is None:               # only an empty marker_text overlaps no run
            return None
        span = MarkerSpan(runs, first[0], first[1], last[0], last[1])
        for r in runs[span.first:span.last + 1]:
            if self.fields.in_field(r):
                raise FieldBoundaryError(
                    f"marker {marker_text!r} overlaps a field; refusing to rewrite its runs")
        return span

    @staticmethod
    def _run_with_rpr(template_r, superscript: Optional[bool]):
        """New, otherwise empty ``w:r`` carrying a copy of *template_r*'s
        ``w:rPr`` -- the one child that formatting lives in.

        *superscript*: None keeps the template's vertical alignment, True makes
        the run superscript, False removes any vertical alignment.
        """
        new_r = OxmlElement('w:r')
        rpr = template_r.find(qn('w:rPr'))
        if rpr is not None:
            rpr = copy.deepcopy(rpr)
            new_r.append(rpr)
        if superscript is not None:
            if rpr is not None:
                for va in rpr.findall(qn('w:vertAlign')):
                    rpr.remove(va)
            if superscript:
                if rpr is None:
                    rpr = new_r.get_or_add_rPr()
                rpr.get_or_add_vertAlign().set(qn('w:val'), 'superscript')
        return new_r

    @staticmethod
    def _text_elem(text: str):
        """A ``w:t`` with *text*, whitespace preserved."""
        t = OxmlElement('w:t')
        t.set(qn('xml:space'), 'preserve')
        t.text = text
        return t

    @staticmethod
    def _slice_children(r_elem, lo: int, hi: Optional[int]) -> list:
        """Deep copies of the content children of *r_elem* (everything but
        ``w:rPr``) that lie within characters [lo, hi] of ``run_text(r_elem)``;
        *hi* None means the end of the run.

        A ``w:t`` straddling a bound is cut to the part inside; a tab, break
        or hyphen is one character and so wholly in or out. Children that
        contribute no text (page breaks, ``w:sym``, drawings, footnote
        references...) are kept when they sit anywhere in the range, bounds
        included: a page break right after a marker stays with the text after
        it, a symbol right before stays with the text before.
        """
        out, pos = [], 0
        for child in r_elem:
            if child.tag == qn('w:rPr'):
                continue
            start = pos
            end = pos = start + len(_child_text(child))
            if start == end:                                    # zero-width
                if lo <= start and (hi is None or start <= hi):
                    out.append(copy.deepcopy(child))
                continue
            cut_lo = max(start, lo)
            cut_hi = end if hi is None else min(end, hi)
            if cut_lo >= cut_hi:
                continue
            piece = copy.deepcopy(child)
            if (cut_lo, cut_hi) != (start, end):                # only a w:t can straddle
                piece.text = (child.text or '')[cut_lo - start:cut_hi - start]
                piece.set(qn('xml:space'), 'preserve')
            out.append(piece)
        return out

    def _split_and_emit(self, span: MarkerSpan, replacement: str,
                        superscript: Optional[bool]):
        """Replace the runs of *span* with before | citation | after runs.

        The touched runs are split at the child level. The before-run carries
        the first touched run's ``w:rPr`` and every content child of it up to
        the marker; the after-run carries the last touched run's ``w:rPr`` and
        every content child of it from the marker on. A ``w:t`` is cut at the
        marker; a ``w:tab``, ``w:br``, ``w:sym``, ``w:drawing``... survives as
        the element it is, in place. Only the marker text -- and any textless
        child strictly inside it -- is replaced. Whole runs between the first
        and the last touched run lie entirely inside the marker.

        The citation run holds exactly one ``w:t``. Its vertical alignment
        follows *superscript*: set when True, stripped when False, kept from
        the first touched run when None. A superscript citation must not
        bleed into the after-run, which then loses any superscript; otherwise
        the after-run keeps its own. Returns the citation run element.

        The cached :attr:`fields` index is deliberately kept: the span guard
        in ``_locate_span`` has already rejected any touched run that belongs
        to a field, so no field run is removed here, and the runs added carry
        no field characters or code. Every run the identity-keyed index knows
        stays in the tree, so it remains exact -- and rebuilding it per call
        made a 300-token renumber (two calls per token) 20x slower.
        """
        runs, first, first_off, last, last_off = span
        first_r, last_r = runs[first], runs[last]
        before = self._slice_children(first_r, 0, first_off)
        after = self._slice_children(last_r, last_off, None)
        new_runs = []
        if before:
            before_r = self._run_with_rpr(first_r, None)
            before_r.extend(before)
            new_runs.append(before_r)
        cite = self._run_with_rpr(first_r, superscript)
        cite.append(self._text_elem(replacement))
        new_runs.append(cite)
        if after:
            after_r = self._run_with_rpr(last_r, False if superscript else None)
            after_r.extend(after)
            new_runs.append(after_r)
        # Insert after the last touched run (inside its hyperlink, if any),
        # then drop the touched runs from wherever each of them lives.
        for r in reversed(new_runs):
            last_r.addnext(r)
        for r in runs[first:last + 1]:
            r.getparent().remove(r)
        # No invalidate_fields(): see the docstring -- no field run was
        # touched, so the index is still exact and a rebuild per marker
        # would dominate renumbering and export.
        return cite

    def _collapse_and_replace_superscript(self, paragraph, marker_text: str,
                                           replacement: str):
        """Legacy fallback: collapse the paragraph into three fresh runs,
        before | cite^ | after. Not reachable from ``replace_marker_by_regex``.

        Refuses paragraphs holding a field: rebuilding their runs would strip
        the field characters and code.
        """
        if paragraph._p.findall(f'.//{{{W_NS}}}fldChar'):
            raise FieldBoundaryError("paragraph contains fields; refusing to collapse runs")
        full_text = paragraph.text
        idx = full_text.find(marker_text)
        before = full_text[:idx]
        after = full_text[idx + len(marker_text):]

        # Capture formatting from the first run
        font_name = font_size = font_bold = font_italic = font_color = None
        if paragraph.runs:
            first_run = paragraph.runs[0]
            font_name = first_run.font.name
            font_size = first_run.font.size
            font_bold = first_run.font.bold
            font_italic = first_run.font.italic
            try:
                font_color = first_run.font.color.rgb if first_run.font.color and first_run.font.color.rgb else None
            except Exception:
                font_color = None

        # Remove all existing runs from the XML
        p_elem = paragraph._element
        w_ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
        for r_el in list(p_elem.findall(f'{{{w_ns}}}r')):
            p_elem.remove(r_el)

        # Helper to apply captured formatting
        def _apply_fmt(run, make_super=False):
            if font_name:
                run.font.name = font_name
            if font_size:
                run.font.size = font_size
            if font_bold is not None:
                run.font.bold = font_bold
            if font_italic is not None:
                run.font.italic = font_italic
            if font_color:
                run.font.color.rgb = font_color
            if make_super:
                run.font.superscript = True

        if before:
            r = paragraph.add_run(before)
            _apply_fmt(r)

        r_cite = paragraph.add_run(replacement)
        _apply_fmt(r_cite, make_super=True)

        if after:
            r = paragraph.add_run(after)
            _apply_fmt(r)
        self.invalidate_fields()

    # ── Insert-mode helpers ─────────────────────────────────────────

    def find_superscript_citation_runs(self) -> list[dict]:
        """Find all superscript runs that contain citation numbers.

        Returns list of dicts with keys:
            paragraph, para_index, run, run_index, numbers (list[int]),
            char_start (int), char_end (int).
        """
        results = []
        for para_idx, para in enumerate(self.doc.paragraphs):
            char_offset = 0
            for run_idx, run in enumerate(para.runs):
                if run.font.superscript:
                    nums = [int(n) for n in re.findall(r'\d+', run.text)]
                    if nums:
                        results.append({
                            'paragraph': para,
                            'para_index': para_idx,
                            'run': run,
                            'run_index': run_idx,
                            'numbers': nums,
                            'char_start': char_offset,
                            'char_end': char_offset + len(run.text),
                        })
                char_offset += len(run.text)
        return results

    def remove_references_section(self, start_para_idx: int):
        """Remove the existing References section (heading + all entries).

        Removes all paragraphs from start_para_idx to end of document.
        They will be replaced with a rebuilt bibliography.
        """
        paragraphs = self.doc.paragraphs
        body_elem = self.doc.element.body
        # Remove from end backwards to avoid index shifting
        for i in range(len(paragraphs) - 1, start_para_idx - 1, -1):
            p_elem = paragraphs[i]._element
            body_elem.remove(p_elem)
        logger.info(f"Removed {len(paragraphs) - start_para_idx} paragraphs "
                     f"(References section from para {start_para_idx})")
        self.invalidate_fields()

    def append_bibliography(self, entries: list[str]):
        """Append a bibliography section to the end of the document."""
        # Add a section break / heading
        bib_heading = self.doc.add_paragraph()
        run = bib_heading.add_run("References")
        run.bold = True
        run.font.size = Pt(14)
        bib_heading.alignment = WD_ALIGN_PARAGRAPH.LEFT

        # Add each entry
        for entry in entries:
            p = self.doc.add_paragraph()
            run = p.add_run(entry)
            run.font.size = Pt(10)
        self.invalidate_fields()

    def save(self, output_path: str):
        """Save the document to a new path."""
        self.doc.save(output_path)
        logger.info(f"Document saved to {output_path}")
