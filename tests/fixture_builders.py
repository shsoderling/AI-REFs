"""Assemble DOCX edge cases (fields, hyperlinks, tracked changes, sdt, text boxes)
for tests. python-docx has no API for any of these, so build raw OOXML."""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import nsmap, qn

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"

# python-docx's namespace table has no "mc" prefix; register it so that
# OxmlElement("mc:AlternateContent") resolves.
nsmap.setdefault("mc", MC_NS)

# Run-level tracked-change wrappers Word writes: insertion, deletion, and the
# two halves of a move (old position / new position) made with Track Changes on.
TRACKED_CHANGE_KINDS = ("ins", "del", "moveFrom", "moveTo")


def run_texts(paragraph) -> list[str]:
    return [r.text for r in paragraph.runs]


def make_run(text: str | None = None, *, instr: str | None = None,
             fldchar: str | None = None, superscript: bool = False,
             deleted: bool = False, fld_lock: bool = False):
    r = OxmlElement("w:r")
    if superscript:
        rpr = OxmlElement("w:rPr")
        va = OxmlElement("w:vertAlign")
        va.set(qn("w:val"), "superscript")
        rpr.append(va)
        r.append(rpr)
    if fldchar:
        fc = OxmlElement("w:fldChar")
        fc.set(qn("w:fldCharType"), fldchar)
        if fld_lock:
            fc.set(qn("w:fldLock"), "1")
        r.append(fc)
    if instr is not None:
        it = OxmlElement("w:delInstrText" if deleted else "w:instrText")
        it.set(qn("xml:space"), "preserve")
        it.text = instr
        r.append(it)
    if text is not None:
        t = OxmlElement("w:delText" if deleted else "w:t")
        t.set(qn("xml:space"), "preserve")
        t.text = text
        r.append(t)
    return r


class DocBuilder:
    def __init__(self):
        self.doc = Document()
        self._rev_id = 100

    # ── paragraphs and plain runs ──────────────────────────────────
    def paragraph(self, text: str = ""):
        p = self.doc.add_paragraph()
        if text:
            p.add_run(text)
        return p

    def add_text(self, paragraph, text: str, *, superscript: bool = False,
                 wrap: str | None = None):
        """Append a run; wrap in TRACKED_CHANGE_KINDS wraps it in that
        tracked-change element. Only 'del' switches the text to w:delText;
        moved text (moveFrom/moveTo) keeps w:t, as Word writes it."""
        r = make_run(text, superscript=superscript, deleted=(wrap == "del"))
        if wrap:
            w = self._tracked_change(wrap)
            w.append(r)
            paragraph._p.append(w)
        else:
            paragraph._p.append(r)
        return r

    def add_multi_wt_run(self, paragraph, texts: list[str]):
        r = OxmlElement("w:r")
        for tx in texts:
            t = OxmlElement("w:t")
            t.set(qn("xml:space"), "preserve")
            t.text = tx
            r.append(t)
        paragraph._p.append(r)
        return r

    # ── fields ─────────────────────────────────────────────────────
    def add_field(self, paragraph, *, code: str, result: str,
                  superscript: bool = False, split_code_at: int | None = None,
                  end_in_new_paragraph: bool = False,
                  extra_paragraph_texts: list[str] | None = None,
                  wrap: str | None = None, fld_lock: bool = False,
                  nested_code: str | None = None):
        """Append a complex field. Returns the list of runs appended.

        split_code_at: split the instrText into two runs at that index.
        end_in_new_paragraph: put the end fldChar in a following paragraph
            (after any extra_paragraph_texts paragraphs), like a bibliography.
        nested_code: add a nested begin/instr/end inside (EndNote-style).
        wrap: one of TRACKED_CHANGE_KINDS; wraps every run of the field.
        """
        runs = [make_run(fldchar="begin", fld_lock=fld_lock)]
        if split_code_at is None:
            runs.append(make_run(instr=code))
        else:
            runs.append(make_run(instr=code[:split_code_at]))
            runs.append(make_run(instr=code[split_code_at:]))
        if nested_code is not None:
            runs += [make_run(fldchar="begin"), make_run(instr=nested_code),
                     make_run(fldchar="end")]
        runs.append(make_run(fldchar="separate"))
        runs.append(make_run(result, superscript=superscript))
        target = paragraph._p
        if wrap:
            w = self._tracked_change(wrap)
            target.append(w)
            target = w
        if not end_in_new_paragraph:
            runs.append(make_run(fldchar="end"))
            for r in runs:
                target.append(r)
            return runs
        for r in runs:
            target.append(r)
        last = paragraph
        for tx in (extra_paragraph_texts or []):
            last = self.doc.add_paragraph()
            last._p.append(make_run(tx))
        end = make_run(fldchar="end")
        last._p.append(end)
        runs.append(end)
        return runs

    def add_simple_field(self, paragraph, *, instr: str, result: str):
        fs = OxmlElement("w:fldSimple")
        fs.set(qn("w:instr"), instr)
        fs.append(make_run(result))
        paragraph._p.append(fs)
        return fs

    # ── hyperlinks, sdt, text boxes ────────────────────────────────
    def add_hyperlink(self, paragraph, url: str, text: str):
        rid = self.doc.part.relate_to(
            url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
            is_external=True)
        h = OxmlElement("w:hyperlink")
        h.set(qn("r:id"), rid)
        h.append(make_run(text))
        paragraph._p.append(h)
        return h

    def add_inline_sdt(self, paragraph, tag: str, text: str):
        sdt = OxmlElement("w:sdt")
        pr = OxmlElement("w:sdtPr")
        t = OxmlElement("w:tag")
        t.set(qn("w:val"), tag)
        pr.append(t)
        sdt.append(pr)
        content = OxmlElement("w:sdtContent")
        content.append(make_run(text))
        sdt.append(content)
        paragraph._p.append(sdt)
        return sdt

    def add_text_box(self, paragraph, inner_paragraph_xml_builder):
        """Append mc:AlternateContent with identical Choice and Fallback text boxes.

        inner_paragraph_xml_builder(p_elem) populates a w:p inside each box.
        """
        ac = OxmlElement("mc:AlternateContent")
        for branch in ("mc:Choice", "mc:Fallback"):
            el = OxmlElement(branch)
            if branch == "mc:Choice":
                el.set("Requires", "wps")
            txbx = OxmlElement("w:txbxContent")
            inner_p = OxmlElement("w:p")
            inner_paragraph_xml_builder(inner_p)
            txbx.append(inner_p)
            el.append(txbx)
            ac.append(el)
        r = OxmlElement("w:r")
        r.append(ac)
        paragraph._p.append(r)
        return ac

    def add_table_cell_paragraph(self, text: str):
        table = self.doc.add_table(rows=1, cols=1)
        cell_p = table.cell(0, 0).paragraphs[0]
        cell_p._p.append(make_run(text))
        return cell_p

    def save(self, path) -> Path:
        path = Path(path)
        self.doc.save(str(path))
        return path

    # ── internals ──────────────────────────────────────────────────
    def _tracked_change(self, kind: str):
        """Build an empty tracked-change run wrapper (w:ins, w:del, w:moveFrom
        or w:moveTo) with a fresh revision id."""
        if kind not in TRACKED_CHANGE_KINDS:
            raise ValueError(f"unknown tracked-change kind {kind!r}; "
                             f"expected one of {TRACKED_CHANGE_KINDS}")
        w = OxmlElement(f"w:{kind}")
        self._rev_id += 1
        w.set(qn("w:id"), str(self._rev_id))
        w.set(qn("w:author"), "tester")
        w.set(qn("w:date"), "2026-01-01T00:00:00Z")
        return w
