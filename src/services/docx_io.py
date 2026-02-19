"""DOCX document I/O: reading, marker detection, replacement, and bibliography."""

import re
import logging
from pathlib import Path
from typing import Optional

from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

logger = logging.getLogger(__name__)

MARKER_PATTERN = re.compile(r'\((REFS?)\)')


class DocxHandler:
    """Read and manipulate DOCX documents for reference insertion."""

    def __init__(self, path: str):
        self.path = path
        self.doc = Document(path)

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
                                superscript: bool = False):
        """Replace the first occurrence of *marker_text* in *paragraph*.

        Walks the existing runs to locate the one(s) that contain the marker,
        splits into up to three new runs (before-text | citation | after-text),
        and preserves all other runs untouched.  When *superscript* is True the
        citation run gets superscript formatting; when False it keeps the
        surrounding run's formatting as-is.
        """
        import copy
        from lxml import etree

        full_text = paragraph.text
        if marker_text not in full_text:
            return  # Marker not found in this paragraph

        w_ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'

        target_start = full_text.find(marker_text)
        target_end = target_start + len(marker_text)

        runs = paragraph.runs
        if not runs:
            if superscript:
                self._collapse_and_replace_superscript(paragraph, marker_text, replacement)
            else:
                paragraph.text = full_text[:target_start] + replacement + full_text[target_end:]
            return

        # Build (run_index, run_char_start, run_char_end) for every run
        run_spans = []
        offset = 0
        for i, run in enumerate(runs):
            run_end = offset + len(run.text)
            run_spans.append((i, offset, run_end))
            offset = run_end

        # Find the first and last run touched by the marker
        first_run_idx = None
        last_run_idx = None
        for i, rs, re_ in run_spans:
            if rs < target_end and re_ > target_start:
                if first_run_idx is None:
                    first_run_idx = i
                last_run_idx = i

        if first_run_idx is None:
            if superscript:
                self._collapse_and_replace_superscript(paragraph, marker_text, replacement)
            else:
                # Fallback: do a safe text-only replacement on the first run containing it
                for run in runs:
                    if marker_text in run.text:
                        run.text = run.text.replace(marker_text, replacement, 1)
                        return
            return

        p_elem = paragraph._element

        # Text before the marker (portion of the first hit run before the marker starts)
        first_rs = run_spans[first_run_idx][1]
        text_before = runs[first_run_idx].text[:target_start - first_rs]

        # Text after the marker (portion of the last hit run after the marker ends)
        last_rs = run_spans[last_run_idx][1]
        text_after = runs[last_run_idx].text[target_end - last_rs:]

        # We use the first hit run as the formatting template
        template_elem = runs[first_run_idx]._element

        new_xml_runs = []

        # 1) "before" portion — same formatting as template run
        if text_before:
            r_before = copy.deepcopy(template_elem)
            for t_el in r_before.findall(f'{{{w_ns}}}t'):
                t_el.text = text_before
                t_el.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            new_xml_runs.append(r_before)

        # 2) Citation run — clone formatting
        r_cite = copy.deepcopy(template_elem)
        for t_el in r_cite.findall(f'{{{w_ns}}}t'):
            t_el.text = replacement
            t_el.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')

        if superscript:
            # Add superscript vertAlign
            rPr = r_cite.find(f'{{{w_ns}}}rPr')
            if rPr is None:
                rPr = etree.SubElement(r_cite, f'{{{w_ns}}}rPr')
                r_cite.insert(0, rPr)
            for va in rPr.findall(f'{{{w_ns}}}vertAlign'):
                rPr.remove(va)
            etree.SubElement(rPr, f'{{{w_ns}}}vertAlign',
                             {f'{{{w_ns}}}val': 'superscript'})

        new_xml_runs.append(r_cite)

        # 3) "after" portion — uses the LAST hit run's formatting (no superscript)
        if text_after:
            r_after = copy.deepcopy(runs[last_run_idx]._element)
            for t_el in r_after.findall(f'{{{w_ns}}}t'):
                t_el.text = text_after
                t_el.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            if superscript:
                rPr_after = r_after.find(f'{{{w_ns}}}rPr')
                if rPr_after is not None:
                    for va in rPr_after.findall(f'{{{w_ns}}}vertAlign'):
                        rPr_after.remove(va)
            new_xml_runs.append(r_after)

        # Insert the new runs right after the LAST hit run, then remove
        # all original runs that the marker touched.
        anchor = runs[last_run_idx]._element
        for new_r in reversed(new_xml_runs):
            anchor.addnext(new_r)

        for i in range(first_run_idx, last_run_idx + 1):
            p_elem.remove(runs[i]._element)

    def _collapse_and_replace_superscript(self, paragraph, marker_text: str,
                                           replacement: str):
        """Fallback when the marker straddles multiple runs.

        Collapses the paragraph into three fresh runs: before | cite^ | after.
        """
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

    def renumber_superscript_run(self, run, renumber_map: dict[int, int]):
        """Replace citation numbers in a superscript run using a renumber map.

        Handles multi-number runs like '1,2,3' by replacing each number
        individually with word-boundary awareness.
        """
        text = run.text
        # Replace numbers from largest to smallest to avoid substring collisions
        # (e.g. replacing '1' before '11' would corrupt '11')
        for old_num in sorted(renumber_map.keys(), reverse=True):
            new_num = renumber_map[old_num]
            if old_num != new_num:
                text = re.sub(rf'\b{old_num}\b', str(new_num), text)
        run.text = text

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

    def save(self, output_path: str):
        """Save the document to a new path."""
        self.doc.save(output_path)
        logger.info(f"Document saved to {output_path}")
