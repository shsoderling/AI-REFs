# Persistent Citation Tracking (Phases 0–1) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or superpowers:subagent-driven-development in-session) to implement this plan task-by-task.

**Goal:** Make AI REFs track its own citations inside the DOCX (EndNote-style hidden fields) so a later session can add new `(REF)` markers, renumber and rebuild the bibliography exactly and offline, instead of re-deriving everything by regex and PubMed.

**Architecture:** Every citation becomes a Word complex field ` ADDIN AIREFS.CITE {csl-shaped json} ` whose cached result is the visible number; the bibliography is wrapped in ` ADDIN AIREFS.BIBL {json} `. Fields walked in document order are the single source of truth; the bibliography field and the `.airefsproj` are caches. Phase 0 hardens the DOCX writer (walkers, field-aware scanners, guards, five live bug fixes); Phase 1 adds the payload/CSL mapping, the field writer, the tracked reader, tracked renumbering and legacy adoption. Design: `docs/plans/2026-09-05-citation-tracking-design.md`.

**Tech Stack:** Python 3.11+, python-docx 1.2.0 (lxml `OxmlElement` for fields — python-docx has no field API), pydantic v2, pytest, PySide6 (GUI wiring only; keep logic headless).

**Conventions for every task**
- Run tests with `python -m pytest -q` from the repo root. The suite must stay green after every task (77 tests pass at the start).
- Never assign `paragraph.text` or `run.text` on anything that may contain a field; rewrite `w:t` text instead.
- Body paragraph indexing is `doc.paragraphs` order everywhere. Do not add table/text-box iteration to any index consumer.
- Commit after each task with a conventional message and the trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Test helpers for OOXML edge cases live in `tests/fixture_builders.py` (Task 1). Use them; do not hand-build XML in individual tests.

---

## Phase 0 — Hygiene (fixes five live bugs; ships alone)

### Task 1: Pin python-docx and add OOXML fixture builders

**Files:**
- Modify: `requirements.txt` (line `python-docx>=1.1.0` → `python-docx>=1.2.0,<2`)
- Create: `tests/fixture_builders.py`
- Test: `tests/test_fixture_builders.py`

**Step 1: Write the failing test**

```python
# tests/test_fixture_builders.py
from docx import Document

from tests.fixture_builders import (
    DocBuilder, W_NS, run_texts,
)


def test_builder_field_is_invisible_to_paragraph_text(tmp_path):
    b = DocBuilder()
    p = b.paragraph("Neurons fire")
    b.add_field(p, code=' ADDIN AIREFS.CITE {"x":1} ', result="1,2", superscript=True)
    b.add_text(p, ". Next")
    path = b.save(tmp_path / "f.docx")
    doc = Document(str(path))
    para = doc.paragraphs[0]
    assert para.text == "Neurons fire1,2. Next"
    assert run_texts(para) == ["Neurons fire", "", "", "", "1,2", "", ". Next"]
    assert [r.font.superscript for r in para.runs][4] is True


def test_builder_hyperlink_text_is_in_paragraph_text_but_not_runs(tmp_path):
    b = DocBuilder()
    p = b.paragraph("See ")
    b.add_hyperlink(p, "https://example.org", "the site")
    b.add_text(p, " here (REF).")
    path = b.save(tmp_path / "h.docx")
    para = Document(str(path)).paragraphs[0]
    assert para.text == "See the site here (REF)."
    assert run_texts(para) == ["See ", " here (REF)."]


def test_builder_split_instr_text_and_cross_paragraph_field(tmp_path):
    b = DocBuilder()
    p1 = b.paragraph("")
    b.add_field(p1, code=' ADDIN AIREFS.BIBL {"v":1} ', result="1. Entry one",
                split_code_at=12, end_in_new_paragraph=True, extra_paragraph_texts=["2. Entry two"])
    path = b.save(tmp_path / "s.docx")
    doc = Document(str(path))
    xml = doc.element.body.xml
    assert xml.count("<w:instrText") == 2
    assert doc.paragraphs[0].text == "1. Entry one"
    assert doc.paragraphs[1].text == "2. Entry two"


def test_builder_tracked_change_wrappers_and_multi_wt(tmp_path):
    b = DocBuilder()
    p = b.paragraph("A")
    b.add_text(p, "B", wrap="ins")
    b.add_text(p, "C", wrap="del")
    b.add_multi_wt_run(p, ["D", "E"])
    path = b.save(tmp_path / "t.docx")
    para = Document(str(path)).paragraphs[0]
    # python-docx ignores w:ins/w:del content in paragraph.text
    assert para.text == "ADE"
    body_xml = para._p.xml
    assert "<w:ins" in body_xml and "<w:del" in body_xml and "<w:delText>C</w:delText>" in body_xml
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fixture_builders.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.fixture_builders'`

**Step 3: Write the builders**

```python
# tests/fixture_builders.py
"""Assemble DOCX edge cases (fields, hyperlinks, tracked changes, sdt, text boxes)
for tests. python-docx has no API for any of these, so build raw OOXML."""

from __future__ import annotations

import copy
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"


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
        self._ins_id = 100

    # ── paragraphs and plain runs ──────────────────────────────────
    def paragraph(self, text: str = ""):
        p = self.doc.add_paragraph()
        if text:
            p.add_run(text)
        return p

    def add_text(self, paragraph, text: str, *, superscript: bool = False,
                 wrap: str | None = None):
        """Append a run; wrap='ins'|'del' wraps it in a tracked-change element."""
        r = make_run(text, superscript=superscript, deleted=(wrap == "del"))
        if wrap:
            w = OxmlElement(f"w:{wrap}")
            self._ins_id += 1
            w.set(qn("w:id"), str(self._ins_id))
            w.set(qn("w:author"), "tester")
            w.set(qn("w:date"), "2026-01-01T00:00:00Z")
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
            w = OxmlElement(f"w:{wrap}")
            self._ins_id += 1
            w.set(qn("w:id"), str(self._ins_id))
            w.set(qn("w:author"), "tester")
            w.set(qn("w:date"), "2026-01-01T00:00:00Z")
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
```

If `OxmlElement("mc:AlternateContent")` raises because the `mc` prefix is unknown to python-docx's nsmap, register it in the builder module: `from docx.oxml.ns import nsmap; nsmap.setdefault("mc", MC_NS)` before use. Same for `w:txbxContent` (already in the `w` namespace).

**Step 4: Run tests**

Run: `python -m pytest tests/test_fixture_builders.py -q`
Expected: 4 passed

**Step 5: Commit**

```bash
git add requirements.txt tests/fixture_builders.py tests/test_fixture_builders.py
git commit -m "test: add OOXML fixture builders; pin python-docx 1.2"
```

---

### Task 2: Text-run and all-run walkers in docx_io

**Files:**
- Modify: `src/services/docx_io.py` (add module-level helpers after `MARKER_PATTERN`)
- Test: `tests/test_docx_walkers.py`

**Step 1: Write the failing test**

```python
# tests/test_docx_walkers.py
import glob

import pytest
from docx import Document

from src.services.docx_io import iter_text_runs, iter_all_runs, run_text
from tests.fixture_builders import DocBuilder


def _edge_doc(tmp_path):
    b = DocBuilder()
    p = b.paragraph("Plain ")
    b.add_hyperlink(p, "https://x.org", "link")
    b.add_text(p, " then ")
    b.add_field(p, code=' ADDIN AIREFS.CITE {"v":1} ', result="3", superscript=True)
    b.add_text(p, " ins", wrap="ins")
    b.add_text(p, " del", wrap="del")
    b.add_inline_sdt(p, "TAG", " sdt")
    b.add_multi_wt_run(p, ["M1", "M2"])
    return b.save(tmp_path / "edge.docx")


@pytest.mark.parametrize("path", sorted(glob.glob("fixtures/*.docx")))
def test_text_runs_reproduce_paragraph_text_on_fixtures(path):
    for para in Document(path).paragraphs:
        assert "".join(run_text(r) for r in iter_text_runs(para)) == para.text


def test_text_runs_reproduce_paragraph_text_on_edge_doc(tmp_path):
    para = Document(str(_edge_doc(tmp_path))).paragraphs[0]
    texts = [run_text(r) for r in iter_text_runs(para)]
    assert "".join(texts) == para.text
    assert "link" in texts            # hyperlink run is included
    assert " sdt" not in texts        # sdt is invisible to paragraph.text, so also to us
    assert "M1M2" in texts            # multi-w:t run text is concatenated


def test_all_runs_covers_nested_and_marks_deleted(tmp_path):
    para = Document(str(_edge_doc(tmp_path))).paragraphs[0]
    infos = list(iter_all_runs(para))
    texts = [run_text(i.elem) for i in infos]
    assert " ins" in texts and " sdt" in texts and "link" in texts
    deleted = [i for i in infos if i.deleted]
    assert len(deleted) == 1 and deleted[0].elem.xpath("string(w:delText)") == " del"


def test_all_runs_skips_mc_fallback(tmp_path):
    b = DocBuilder()
    p = b.paragraph("Body")

    def fill(inner_p):
        from tests.fixture_builders import make_run
        inner_p.append(make_run("boxed"))

    b.add_text_box(p, fill)
    para = Document(str(b.save(tmp_path / "tb.docx"))).paragraphs[0]
    texts = [run_text(i.elem) for i in iter_all_runs(para)]
    assert texts.count("boxed") == 1
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_docx_walkers.py -q`
Expected: FAIL with `ImportError: cannot import name 'iter_text_runs'`

**Step 3: Implement**

Add to `src/services/docx_io.py` (module level, after `MARKER_PATTERN`):

```python
from dataclasses import dataclass

W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
MC_NS = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
_NSMAP = {'w': W_NS, 'mc': MC_NS}


def run_text(r_elem) -> str:
    """Visible text of a w:r, matching python-docx's CT_R.text (no instrText)."""
    parts = []
    for child in r_elem:
        tag = child.tag
        if tag == f'{{{W_NS}}}t':
            parts.append(child.text or '')
        elif tag == f'{{{W_NS}}}tab':
            parts.append('\t')
        elif tag in (f'{{{W_NS}}}br', f'{{{W_NS}}}cr'):
            parts.append('\n')
        elif tag == f'{{{W_NS}}}noBreakHyphen':
            parts.append('-')
        elif tag == f'{{{W_NS}}}ptab':
            parts.append('\t')
    return ''.join(parts)


def iter_text_runs(paragraph) -> list:
    """Runs that contribute to ``paragraph.text``, in order.

    python-docx computes ``paragraph.text`` from ``w:r | w:hyperlink`` children,
    so this is exactly ``./w:r | ./w:hyperlink/w:r``. Use it (and only it) for
    character-offset arithmetic. A test asserts equality with paragraph.text.
    """
    return paragraph._p.xpath('./w:r | ./w:hyperlink/w:r')


@dataclass
class RunInfo:
    elem: object            # the w:r element
    deleted: bool           # inside a w:del (tracked deletion)
    inserted: bool          # inside a w:ins


def iter_all_runs(paragraph):
    """Every w:r under the paragraph except mc:Fallback duplicates.

    For field awareness only — never for offsets.
    """
    for r in paragraph._p.iter(f'{{{W_NS}}}r'):
        anc = r.getparent()
        skip = False
        deleted = inserted = False
        while anc is not None and anc is not paragraph._p:
            if anc.tag == f'{{{MC_NS}}}Fallback':
                skip = True
                break
            if anc.tag == f'{{{W_NS}}}del':
                deleted = True
            elif anc.tag == f'{{{W_NS}}}ins':
                inserted = True
            anc = anc.getparent()
        if not skip:
            yield RunInfo(elem=r, deleted=deleted, inserted=inserted)
```

Note: `paragraph._p.xpath` is python-docx's namespaced xpath (prefixes `w`, `mc`, … preregistered), returning `CT_R` objects. `run_text` exists because `r.text` on a raw lxml element (from `.iter`) is not available.

**Step 4: Run tests**

Run: `python -m pytest tests/test_docx_walkers.py tests -q`
Expected: all pass

**Step 5: Commit**

```bash
git add src/services/docx_io.py tests/test_docx_walkers.py
git commit -m "feat(docx): add text-run and all-run walkers matching paragraph.text"
```

---

### Task 3: Complex-field reader (`docx_fields.py`)

**Files:**
- Create: `src/services/docx_fields.py`
- Test: `tests/test_docx_fields.py`

**Behaviour:**
- `iter_complex_fields(body_elem)` walks every `w:r` under the body in document order (body paragraphs and table cells; `mc:Fallback` skipped), keeps a stack of open fields, concatenates `w:instrText` / `w:delInstrText` across runs and paragraphs, records `separate` and result runs, and closes on `end`. Nested fields are recorded with `depth`. An unmatched `begin` at the end of the document yields a field with `complete=False`. `w:fldSimple` elements are yielded as fields with `code = @w:instr` and their runs as result runs.
- `classify_code(code)`: strip, then `"airefs_cite"` if it starts with `ADDIN AIREFS.CITE`, `"airefs_bibl"` for `ADDIN AIREFS.BIBL`, `"foreign"` for any other `ADDIN` whose code contains one of `EN.CITE`, `EN.REFLIST`, `CSL_CITATION`, `CSL_BIBLIOGRAPHY`, `ZOTERO_`, `Mendeley`, `CITATION`, `BIBLIOGRAPHY`; else `"other"` (page numbers, TOC, hyperlinks…).
- `FieldIndex(doc)`: builds the field list once and answers `in_field(run_elem)`, `role(run_elem)` (`'marker'`, `'code'`, `'result'`, or `None`), `fields_in_paragraph(p_elem)`, `result_spans(paragraph)` (char spans over `paragraph.text` occupied by result runs of top-level fields), plus counters `airefs_cite`, `airefs_bibl`, `foreign`, `pending_tracked_changes` (any field with a run under `w:del` or `w:ins`), `in_tables` (fields whose begin run is inside a `w:tbl`).

**Step 1: Write the failing test**

```python
# tests/test_docx_fields.py
from docx import Document

from src.services.docx_fields import (
    FieldIndex, classify_code, iter_complex_fields,
)
from tests.fixture_builders import DocBuilder, make_run


def _doc(tmp_path, build):
    b = DocBuilder()
    build(b)
    return Document(str(b.save(tmp_path / "d.docx")))


def test_single_field_code_and_result(tmp_path):
    def build(b):
        p = b.paragraph("x")
        b.add_field(p, code=' ADDIN AIREFS.CITE {"v":1} ', result="1", superscript=True)
    doc = _doc(tmp_path, build)
    fields = iter_complex_fields(doc.element.body)
    assert len(fields) == 1
    f = fields[0]
    assert f.code == 'ADDIN AIREFS.CITE {"v":1}'
    assert f.result_text == "1"
    assert f.complete and f.depth == 0 and f.kind == "airefs_cite"


def test_split_instr_text_is_concatenated(tmp_path):
    def build(b):
        p = b.paragraph("")
        b.add_field(p, code=' ADDIN AIREFS.CITE {"title":"a~b"} ', result="2", split_code_at=20)
    f = iter_complex_fields(_doc(tmp_path, build).element.body)[0]
    assert f.code == 'ADDIN AIREFS.CITE {"title":"a~b"}'


def test_field_spanning_paragraphs(tmp_path):
    def build(b):
        p = b.paragraph("")
        b.add_field(p, code=' ADDIN AIREFS.BIBL {"v":1} ', result="1. One",
                    end_in_new_paragraph=True, extra_paragraph_texts=["2. Two", "3. Three"])
    doc = _doc(tmp_path, build)
    f = iter_complex_fields(doc.element.body)[0]
    assert f.kind == "airefs_bibl" and f.complete
    assert len(f.paragraphs) == 3
    assert f.result_text == "1. One\n2. Two\n3. Three"


def test_nested_field_depth_and_foreign_classification(tmp_path):
    def build(b):
        p = b.paragraph("")
        b.add_field(p, code=" ADDIN EN.CITE <EndNote/> ", result="(Smith 2020)",
                    nested_code=" ADDIN EN.CITE.DATA ")
    fields = iter_complex_fields(_doc(tmp_path, build).element.body)
    assert [(f.kind, f.depth) for f in fields] == [("foreign", 0), ("foreign", 1)]


def test_fld_simple_and_unmatched_begin(tmp_path):
    def build(b):
        p = b.paragraph("")
        b.add_simple_field(p, instr=" PAGE ", result="7")
        p2 = b.paragraph("")
        p2._p.append(make_run(fldchar="begin"))
        p2._p.append(make_run(instr=" ADDIN AIREFS.CITE {} "))
    fields = iter_complex_fields(_doc(tmp_path, build).element.body)
    assert fields[0].kind == "other" and fields[0].result_text == "7"
    assert fields[1].complete is False


def test_tracked_change_wrappers_and_text_box_dedup(tmp_path):
    def build(b):
        p = b.paragraph("")
        b.add_field(p, code=' ADDIN AIREFS.CITE {"a":1} ', result="1", wrap="del")
        b.add_field(p, code=' ADDIN AIREFS.CITE {"a":2} ', result="2", wrap="ins")

        def fill(inner_p):
            for r in (make_run(fldchar="begin"), make_run(instr=' ADDIN AIREFS.CITE {"a":3} '),
                      make_run(fldchar="separate"), make_run("3"), make_run(fldchar="end")):
                inner_p.append(r)
        b.add_text_box(p, fill)
    doc = _doc(tmp_path, build)
    fields = iter_complex_fields(doc.element.body)
    assert len(fields) == 3
    assert fields[0].deleted is True and fields[1].deleted is False
    idx = FieldIndex(doc)
    assert idx.pending_tracked_changes is True
    assert idx.airefs_cite == 3


def test_field_index_roles_and_result_spans(tmp_path):
    def build(b):
        p = b.paragraph("Cells fire")
        b.add_field(p, code=' ADDIN AIREFS.CITE {"a":1} ', result="4,5", superscript=True)
        b.add_text(p, " and [2] more.")
    doc = _doc(tmp_path, build)
    para = doc.paragraphs[0]
    idx = FieldIndex(doc)
    roles = [idx.role(r) for r in para.runs]
    assert roles == [None, "marker", "code", "marker", "result", "marker", None]
    assert idx.in_field(para.runs[4]) and not idx.in_field(para.runs[0])
    assert idx.result_spans(para) == [(10, 13)]
    assert para.text[10:13] == "4,5"


def test_fields_in_tables_are_counted(tmp_path):
    def build(b):
        cell_p = b.add_table_cell_paragraph("Fig. 1 ")
        b.add_field(cell_p, code=' ADDIN AIREFS.CITE {"a":9} ', result="9")
    idx = FieldIndex(_doc(tmp_path, build))
    assert idx.airefs_cite == 1 and idx.in_tables == 1


def test_classify_code():
    assert classify_code(' ADDIN AIREFS.CITE {} ') == "airefs_cite"
    assert classify_code('ADDIN AIREFS.BIBL {}') == "airefs_bibl"
    assert classify_code(' ADDIN ZOTERO_ITEM CSL_CITATION {} ') == "foreign"
    assert classify_code(' ADDIN EN.CITE <x/> ') == "foreign"
    assert classify_code(' ADDIN Mendeley Bibliography CSL_BIBLIOGRAPHY ') == "foreign"
    assert classify_code(' PAGE ') == "other"
    assert classify_code(' HYPERLINK "x" ') == "other"
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_docx_fields.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.services.docx_fields'`

**Step 3: Implement**

```python
# src/services/docx_fields.py
"""Read Word complex fields (fldChar begin / instrText / separate / result / end).

python-docx has no field API. This module is the only place that understands
field structure; scanners ask a FieldIndex whether a run belongs to a field.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
MC_NS = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
_W = lambda tag: f'{{{W_NS}}}{tag}'  # noqa: E731

CITE_PREFIX = 'ADDIN AIREFS.CITE'
BIBL_PREFIX = 'ADDIN AIREFS.BIBL'
_FOREIGN_MARKERS = ('EN.CITE', 'EN.REFLIST', 'CSL_CITATION', 'CSL_BIBLIOGRAPHY',
                    'ZOTERO_', 'Mendeley', 'CITATION', 'BIBLIOGRAPHY')


def classify_code(code: str) -> str:
    c = (code or '').strip()
    if c.startswith(CITE_PREFIX):
        return 'airefs_cite'
    if c.startswith(BIBL_PREFIX):
        return 'airefs_bibl'
    if c.startswith('ADDIN') and any(m in c for m in _FOREIGN_MARKERS):
        return 'foreign'
    if c.startswith('CITATION') or c.startswith('BIBLIOGRAPHY'):
        return 'foreign'   # Word's native citation fields
    return 'other'


@dataclass
class ComplexField:
    begin: object                                   # w:r holding fldChar begin (or the w:fldSimple)
    code_runs: list = field(default_factory=list)
    separate: Optional[object] = None
    result_runs: list = field(default_factory=list)
    end: Optional[object] = None
    paragraphs: list = field(default_factory=list)  # w:p elements spanned, in order
    depth: int = 0
    deleted: bool = False
    inserted: bool = False
    complete: bool = False
    simple: bool = False
    code: str = ''
    kind: str = 'other'
    in_table: bool = False

    @property
    def result_text(self) -> str:
        """Visible result text; paragraphs joined with newlines."""
        from .docx_io import run_text
        chunks, current_p, buf = [], None, []
        for r in self.result_runs:
            p = _enclosing_paragraph(r)
            if current_p is not None and p is not current_p:
                chunks.append(''.join(buf))
                buf = []
            current_p = p
            buf.append(run_text(r))
        chunks.append(''.join(buf))
        return '\n'.join(chunks)

    @property
    def all_runs(self) -> list:
        runs = [self.begin] + list(self.code_runs)
        if self.separate is not None:
            runs.append(self.separate)
        runs += list(self.result_runs)
        if self.end is not None:
            runs.append(self.end)
        return runs


def _enclosing_paragraph(elem):
    p = elem
    while p is not None and p.tag != _W('p'):
        p = p.getparent()
    return p


def _ancestor_flags(r, stop):
    """(skip_fallback, deleted, inserted, in_table) for a run."""
    deleted = inserted = in_table = False
    anc = r.getparent()
    while anc is not None and anc is not stop:
        tag = anc.tag
        if tag == f'{{{MC_NS}}}Fallback':
            return True, deleted, inserted, in_table
        if tag == _W('del'):
            deleted = True
        elif tag == _W('ins'):
            inserted = True
        elif tag == _W('tbl'):
            in_table = True
        anc = anc.getparent()
    return False, deleted, inserted, in_table


def iter_complex_fields(body_elem) -> list[ComplexField]:
    """All fields under *body_elem* in document order (stack-based)."""
    fields: list[ComplexField] = []
    stack: list[ComplexField] = []
    for node in body_elem.iter(_W('r'), _W('fldSimple')):
        skip, deleted, inserted, in_table = _ancestor_flags(node, body_elem)
        if skip:
            continue
        if node.tag == _W('fldSimple'):
            f = ComplexField(begin=node, simple=True, complete=True,
                             code=(node.get(_W('instr')) or '').strip(),
                             depth=len(stack), deleted=deleted, inserted=inserted,
                             in_table=in_table)
            f.result_runs = list(node.iter(_W('r')))
            f.paragraphs = [_enclosing_paragraph(node)]
            f.kind = classify_code(f.code)
            fields.append(f)
            continue
        fc = node.find(_W('fldChar'))
        p = _enclosing_paragraph(node)
        if fc is not None:
            ftype = fc.get(_W('fldCharType'))
            if ftype == 'begin':
                f = ComplexField(begin=node, depth=len(stack), deleted=deleted,
                                 inserted=inserted, in_table=in_table, paragraphs=[p])
                stack.append(f)
                fields.append(f)
            elif ftype == 'separate' and stack:
                stack[-1].separate = node
                _note_paragraph(stack[-1], p)
            elif ftype == 'end' and stack:
                f = stack.pop()
                f.end = node
                f.complete = True
                _note_paragraph(f, p)
                f.code = f.code.strip()
                f.kind = classify_code(f.code)
            continue
        if not stack:
            continue
        f = stack[-1]
        _note_paragraph(f, p)
        instr = node.find(_W('instrText'))
        if instr is None:
            instr = node.find(_W('delInstrText'))
        if instr is not None and f.separate is None:
            f.code_runs.append(node)
            f.code += instr.text or ''
        elif f.separate is not None:
            f.result_runs.append(node)
    for f in stack:                       # unmatched begins
        f.code = f.code.strip()
        f.kind = classify_code(f.code)
    return fields


def _note_paragraph(f: ComplexField, p):
    if p is not None and (not f.paragraphs or f.paragraphs[-1] is not p):
        f.paragraphs.append(p)


class FieldIndex:
    """Per-document lookup: which runs belong to which field."""

    def __init__(self, doc):
        self.doc = doc
        self.fields = iter_complex_fields(doc.element.body)
        self._role: dict[int, str] = {}
        self._field_of: dict[int, ComplexField] = {}
        for f in self.fields:
            for r in f.all_runs:
                if r is None:
                    continue
                self._field_of.setdefault(id(r), f)
            self._role.setdefault(id(f.begin), 'marker')
            for r in f.code_runs:
                self._role.setdefault(id(r), 'code')
            if f.separate is not None:
                self._role.setdefault(id(f.separate), 'marker')
            for r in f.result_runs:
                self._role.setdefault(id(r), 'result')
            if f.end is not None:
                self._role.setdefault(id(f.end), 'marker')

    # ── counters ────────────────────────────────────────────────
    @property
    def airefs_cite(self) -> int:
        return sum(1 for f in self.fields if f.kind == 'airefs_cite')

    @property
    def airefs_bibl(self) -> int:
        return sum(1 for f in self.fields if f.kind == 'airefs_bibl')

    @property
    def foreign(self) -> int:
        return sum(1 for f in self.fields if f.kind == 'foreign')

    @property
    def in_tables(self) -> int:
        return sum(1 for f in self.fields if f.in_table and f.kind.startswith('airefs'))

    @property
    def pending_tracked_changes(self) -> bool:
        return any(f.deleted or f.inserted for f in self.fields)

    # ── membership ──────────────────────────────────────────────
    def _elem(self, run):
        return getattr(run, '_r', run)   # accept python-docx Run or raw element

    def in_field(self, run) -> bool:
        return id(self._elem(run)) in self._field_of

    def role(self, run) -> Optional[str]:
        return self._role.get(id(self._elem(run)))

    def field_of(self, run) -> Optional[ComplexField]:
        return self._field_of.get(id(self._elem(run)))

    def fields_in_paragraph(self, p_elem) -> list[ComplexField]:
        return [f for f in self.fields if any(p is p_elem for p in f.paragraphs)]

    def result_spans(self, paragraph) -> list[tuple[int, int]]:
        """Char spans in paragraph.text covered by top-level field results."""
        from .docx_io import iter_text_runs, run_text
        spans, offset = [], 0
        current = None
        for r in iter_text_runs(paragraph):
            n = len(run_text(r))
            f = self._field_of.get(id(r))
            if f is not None and f.depth == 0 and self._role.get(id(r)) == 'result':
                if current is not None and current[0] is f:
                    current[2] = offset + n
                else:
                    if current is not None:
                        spans.append((current[1], current[2]))
                    current = [f, offset, offset + n]
            offset += n
        if current is not None:
            spans.append((current[1], current[2]))
        return spans
```

Important: `python-docx` element identity — `paragraph.runs` returns wrapper `Run` objects whose `._r` is the same lxml element object as returned by `body.iter(...)`, so `id()`-based membership works within one `Document` instance. Rebuild the index after any structural edit (`DocxHandler` will own this; see Task 4).

**Step 4: Run tests**

Run: `python -m pytest tests/test_docx_fields.py -q`
Expected: 9 passed

**Step 5: Commit**

```bash
git add src/services/docx_fields.py tests/test_docx_fields.py
git commit -m "feat(docx): complex-field reader and FieldIndex"
```

---

### Task 4: Safe marker replacement (`_locate_span` / `_split_and_emit`)

**Files:**
- Modify: `src/services/docx_io.py:56-232` (`replace_marker_by_regex`, `_collapse_and_replace_superscript`) and `DocxHandler.__init__`
- Test: `tests/test_replace_marker.py`

**Behaviour:**
- `DocxHandler.fields` is a lazily built `FieldIndex`; `DocxHandler.invalidate_fields()` clears it. Every structural edit method calls `invalidate_fields()`.
- `replace_marker_by_regex(paragraph, marker_text, replacement, superscript=False)` keeps its signature (call sites: `main_window.py`, `renumber_apply.py`, `author_date_convert.py`) but is now: `_locate_span(paragraph, marker_text)` → `(first_run, first_offset_in_run, last_run, last_offset_in_run)` over `iter_text_runs`; raise `FieldBoundaryError` if any touched run is `fields.in_field`; then `_split_and_emit(...)` builds up to three new runs cloning **only** `w:rPr` (never other children) with exactly one `w:t`, inserts them after the last touched run and removes the touched runs. Returns the new citation run element. The `if not runs` and the run-scanning fallbacks are deleted — if `marker_text in paragraph.text`, `_locate_span` always finds it.
- `_collapse_and_replace_superscript` raises `FieldBoundaryError` if the paragraph contains any `w:fldChar`; otherwise unchanged. It is no longer reachable from `replace_marker_by_regex` (kept only for backward compatibility of tests; delete if no test uses it).

**Step 1: Write the failing test**

```python
# tests/test_replace_marker.py
import pytest
from docx import Document

from src.services.docx_io import DocxHandler, FieldBoundaryError
from tests.fixture_builders import DocBuilder, run_texts


def _handler(tmp_path, build):
    b = DocBuilder()
    build(b)
    return DocxHandler(str(b.save(tmp_path / "m.docx")))


def test_marker_after_hyperlink_is_placed_correctly(tmp_path):
    def build(b):
        p = b.paragraph("See ")
        b.add_hyperlink(p, "https://x.org", "the site")
        b.add_text(p, " for details (REF). More.")
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    h.replace_marker_by_regex(para, "(REF)", "7", superscript=True)
    assert para.text == "See the site for details 7. More."
    sup = [r for r in para.runs if r.font.superscript]
    assert [r.text for r in sup] == ["7"]


def test_marker_spanning_runs_keeps_neighbour_formatting(tmp_path):
    def build(b):
        p = b.paragraph("Claim ")
        b.add_text(p, "(RE")
        b.add_text(p, "F) rest")
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    h.replace_marker_by_regex(para, "(REF)", "[3]")
    assert para.text == "Claim [3] rest"


def test_multi_wt_run_is_not_duplicated(tmp_path):
    def build(b):
        p = b.paragraph("")
        b.add_multi_wt_run(p, ["Alpha ", "(REF)", " omega"])
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    h.replace_marker_by_regex(para, "(REF)", "[1]")
    assert para.text == "Alpha [1] omega"
    # one w:t per new run
    for r in para.runs:
        assert len(r._r.findall("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")) == 1


def test_refuses_to_touch_a_field_result(tmp_path):
    def build(b):
        p = b.paragraph("Cells ")
        b.add_field(p, code=' ADDIN AIREFS.CITE {"a":1} ', result="(REF)")
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    with pytest.raises(FieldBoundaryError):
        h.replace_marker_by_regex(para, "(REF)", "1")


def test_works_adjacent_to_a_field(tmp_path):
    def build(b):
        p = b.paragraph("Cells")
        b.add_field(p, code=' ADDIN AIREFS.CITE {"a":1} ', result="1", superscript=True)
        b.add_text(p, " and more (REF).")
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    h.replace_marker_by_regex(para, "(REF)", "2", superscript=True)
    assert para.text == "Cells1 and more 2."
    xml = para._p.xml
    assert xml.count("w:fldChar") == 3 * 2   # begin/separate/end, open+close tags untouched
    assert 'AIREFS.CITE' in xml


def test_never_clears_paragraph_content(tmp_path, monkeypatch):
    from docx.oxml.text.paragraph import CT_P
    called = []
    orig = CT_P.clear_content
    monkeypatch.setattr(CT_P, "clear_content", lambda self: called.append(1) or orig(self))

    def build(b):
        p = b.paragraph("")
        b.add_hyperlink(p, "https://x.org", "only a link (REF)")
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    h.replace_marker_by_regex(para, "(REF)", "[4]")
    assert called == []
    assert para.text == "only a link [4]"


def test_collapse_refuses_fielded_paragraph(tmp_path):
    def build(b):
        p = b.paragraph("A ")
        b.add_field(p, code=' ADDIN AIREFS.CITE {"a":1} ', result="1")
        b.add_text(p, " (REF)")
    h = _handler(tmp_path, build)
    para = h.get_paragraphs()[0]
    with pytest.raises(FieldBoundaryError):
        h._collapse_and_replace_superscript(para, "(REF)", "9")
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_replace_marker.py -q`
Expected: FAIL (`ImportError: cannot import name 'FieldBoundaryError'`)

**Step 3: Implement**

In `src/services/docx_io.py`:

```python
class FieldBoundaryError(RuntimeError):
    """Raised when an edit would split or rewrite runs that belong to a field."""


class DocxHandler:
    def __init__(self, path: str):
        self.path = path
        self.doc = Document(path)
        self._fields = None

    @property
    def fields(self):
        from .docx_fields import FieldIndex
        if self._fields is None:
            self._fields = FieldIndex(self.doc)
        return self._fields

    def invalidate_fields(self):
        self._fields = None
```

Replace the body of `replace_marker_by_regex` with:

```python
    def replace_marker_by_regex(self, paragraph, marker_text: str, replacement: str,
                                superscript: bool = False):
        span = self._locate_span(paragraph, marker_text)
        if span is None:
            return None
        return self._split_and_emit(paragraph, span, replacement, superscript)

    def _locate_span(self, paragraph, marker_text: str):
        """(runs, first_idx, first_off, last_idx, last_off) over iter_text_runs, or None."""
        full_text = paragraph.text
        start = full_text.find(marker_text)
        if start < 0:
            return None
        end = start + len(marker_text)
        runs = iter_text_runs(paragraph)
        offset = 0
        first = last = None
        for i, r in enumerate(runs):
            n = len(run_text(r))
            rs, re_ = offset, offset + n
            if rs < end and re_ > start:
                if first is None:
                    first = (i, start - rs)
                last = (i, end - rs)
            offset = re_
        if first is None:
            return None
        for i in range(first[0], last[0] + 1):
            if self.fields.in_field(runs[i]):
                raise FieldBoundaryError(
                    f"marker {marker_text!r} overlaps a citation field in paragraph")
        return runs, first[0], first[1], last[0], last[1]

    @staticmethod
    def _clone_rpr_only(template_r, text: str, superscript: bool | None):
        """New w:r with a copy of template's rPr (only) and one w:t."""
        import copy
        from lxml import etree
        new_r = etree.SubElement(etree.Element(f'{{{W_NS}}}dummy'), f'{{{W_NS}}}r')
        rpr = template_r.find(f'{{{W_NS}}}rPr')
        if rpr is not None:
            new_r.append(copy.deepcopy(rpr))
        if superscript is not None:
            rpr = new_r.find(f'{{{W_NS}}}rPr')
            if rpr is None:
                rpr = etree.SubElement(new_r, f'{{{W_NS}}}rPr')
                new_r.insert(0, rpr)
            for va in rpr.findall(f'{{{W_NS}}}vertAlign'):
                rpr.remove(va)
            if superscript:
                etree.SubElement(rpr, f'{{{W_NS}}}vertAlign', {f'{{{W_NS}}}val': 'superscript'})
        t = etree.SubElement(new_r, f'{{{W_NS}}}t')
        t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        t.text = text
        return new_r

    def _split_and_emit(self, paragraph, span, replacement: str, superscript: bool):
        runs, fi, fo, li, lo = span
        first_r, last_r = runs[fi], runs[li]
        text_before = run_text(first_r)[:fo]
        text_after = run_text(last_r)[lo:]
        new_runs = []
        if text_before:
            new_runs.append(self._clone_rpr_only(first_r, text_before, None))
        cite = self._clone_rpr_only(first_r, replacement, True if superscript else (False if superscript is False and first_r.find(f'{{{W_NS}}}rPr') is not None and first_r.find(f'{{{W_NS}}}rPr').find(f'{{{W_NS}}}vertAlign') is not None else None))
        new_runs.append(cite)
        if text_after:
            new_runs.append(self._clone_rpr_only(last_r, text_after, False if superscript else None))
        anchor = last_r
        for r in reversed(new_runs):
            anchor.addnext(r)
        parent = None
        for i in range(fi, li + 1):
            parent = runs[i].getparent()
            parent.remove(runs[i])
        self.invalidate_fields()
        return cite
```

Simplify the `cite` superscript expression to: `sup = True if superscript else None` and, when `superscript` is False and the template run has `vertAlign`, pass `False` so a citation inserted into a superscript template is not superscript. Keep the `text_after` rule from today: strip superscript from the after-run only when inserting a superscript citation.

`_collapse_and_replace_superscript`: add at the top

```python
        if paragraph._p.findall(f'.//{{{W_NS}}}fldChar'):
            raise FieldBoundaryError("paragraph contains fields; refusing to collapse runs")
```

Note the `getparent()` removal: a touched run inside `w:hyperlink` is removed from the hyperlink and the new runs are inserted after it *inside the hyperlink* when `last_r` is inside it (`addnext` keeps the parent). That preserves link text; acceptable.

**Step 4: Run tests**

Run: `python -m pytest tests/test_replace_marker.py tests -q`
Expected: all pass (existing `test_bracket_helpers.py` exercises `replace_marker_by_regex` through `apply_renumbering`).

**Step 5: Commit**

```bash
git add src/services/docx_io.py tests/test_replace_marker.py
git commit -m "fix(docx): locate markers over text runs, never clear paragraphs, refuse field spans"
```

---

### Task 5: Field-aware scanners and superscript range expansion

**Files:**
- Modify: `src/services/docx_io.py:236-260` (`find_superscript_citation_runs`)
- Modify: `src/pipeline/existing_citation_parser.py:128-202` (`_scan_in_text_citations`, `_expand_citation_range`)
- Modify: `src/pipeline/renumber_apply.py:85-153` (`apply_renumbering`)
- Modify: `src/pipeline/author_date_convert.py:70-127` (`convert_in_text_to_author_date`)
- Test: `tests/test_field_aware_scanners.py`

**Behaviour:**
- `find_superscript_citation_runs(skip_fields=True)` returns runs as before but (a) omits runs for which `self.fields.in_field(run)` when `skip_fields`, and (b) its `numbers` list is `expand_bracket_numbers(run.text)` so `3-5` → `[3,4,5]`.
- `ExistingCitationParser._scan_in_text_citations` uses `expand_bracket_numbers` for superscript runs, skips runs inside fields, and ignores bracket matches whose span overlaps `handler.fields.result_spans(para)`. `_expand_citation_range` is deleted and its two callers use `expand_bracket_numbers` (move `expand_bracket_numbers` and `format_bracket_numbers` into a new tiny module `src/pipeline/citation_numbers.py` to avoid a circular import between the parser and `renumber_apply`; `renumber_apply` re-exports them).
- `apply_renumbering` and `convert_in_text_to_author_date` skip in-field runs and skip bracket matches inside result spans. Superscript renumbering rewrites `3-5` as the mapped, re-collapsed list via `renumber_bracket_group`.

**Step 1: Write the failing test**

```python
# tests/test_field_aware_scanners.py
from docx import Document

from src.models.existing_refs import ExistingCitationMap
from src.pipeline.existing_citation_parser import ExistingCitationParser
from src.pipeline.renumber_apply import apply_renumbering
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder


def _cited_doc(tmp_path, *, with_field=True):
    b = DocBuilder()
    p = b.paragraph("First claim")
    b.add_text(p, "3-5", superscript=True)
    b.add_text(p, ". Second claim [1, 2]. ")
    if with_field:
        b.add_field(p, code=' ADDIN AIREFS.CITE {"a":1} ', result="6", superscript=True)
        b.add_text(p, " and ")
        b.add_field(p, code=' ADDIN AIREFS.CITE {"a":2} ', result="[7]")
    b.paragraph("References")
    for i in range(1, 8):
        b.paragraph(f"{i}. Author{i} A. Title {i}. J. 2020. doi:10.1/x{i}")
    return DocxHandler(str(b.save(tmp_path / "c.docx")))


def test_superscript_range_expands_and_fields_are_skipped(tmp_path):
    h = _cited_doc(tmp_path)
    runs = h.find_superscript_citation_runs()
    assert [r["numbers"] for r in runs] == [[3, 4, 5]]     # field result "6" skipped
    existing = ExistingCitationParser(h).analyze()
    nums = sorted(c.number for c in existing.in_text_citations[0])
    assert nums == [1, 2, 3, 4, 5]                          # 6 and [7] live in fields


def test_apply_renumbering_skips_fields_and_maps_ranges(tmp_path):
    h = _cited_doc(tmp_path)
    existing = ExistingCitationParser(h).analyze()
    apply_renumbering(h, existing, {3: 10, 4: 11, 5: 12, 1: 2, 2: 1, 7: 99})
    para = h.get_paragraphs()[0]
    assert para.text == "First claim10-12. Second claim [1, 2]. 6 and [7]"
    assert "AIREFS.CITE" in para._p.xml


def test_bracket_regex_ignores_field_results(tmp_path):
    h = _cited_doc(tmp_path)
    existing = ExistingCitationParser(h).analyze()
    assert all(c.number != 7 for c in existing.in_text_citations[0])
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_field_aware_scanners.py -q`
Expected: FAIL (`[3, 5]` instead of `[3, 4, 5]`, and `6`/`7` present)

**Step 3: Implement**

1. Create `src/pipeline/citation_numbers.py` containing `expand_bracket_numbers` and `format_bracket_numbers` (moved verbatim from `renumber_apply.py:15-64`). In `renumber_apply.py` replace the definitions with `from .citation_numbers import expand_bracket_numbers, format_bracket_numbers  # noqa: F401`.
2. `docx_io.find_superscript_citation_runs(self, skip_fields: bool = True)`: inside the loop, `if skip_fields and self.fields.in_field(run): char_offset += len(run.text); continue`; compute `nums = expand_bracket_numbers(run.text)` (import inside the method to avoid a circular import).
3. `existing_citation_parser._scan_in_text_citations(self, paragraphs, valid_numbers)`: keep the signature; for each paragraph get `spans = self.handler.fields.result_spans(para)` and `def _in_field_result(offset): return any(s <= offset < e for s, e in spans)`; for superscript runs skip `self.handler.fields.in_field(run)`; replace the inner `re.finditer(r'\d+', run.text)` loop with iterating `expand_bracket_numbers(run.text)` (all numbers share the run's start offset); for bracket matches also skip when `_in_field_result(m.start())`. Delete `_expand_citation_range`; use `expand_bracket_numbers` from `citation_numbers`.
4. `apply_renumbering`: superscript loop — `cite_runs = handler.find_superscript_citation_runs()` already skips fields; replace the `re.sub(r'\d+', ...)` with `new_text = renumber_bracket_group(text, renumber_map)` (handles ranges and dedup; keep the `CITATION_SHAPE_PATTERN` gate). Bracket loop — get `spans = handler.fields.result_spans(para)` and skip matches whose `match.start()` falls in a span. Note `format_bracket_numbers` emits `", "` separators; for superscript runs re-join with `","` (no space) to preserve today's superscript shape: after `renumber_bracket_group`, `new_text = new_text.replace(", ", ",")` when the run was superscript.
5. `convert_in_text_to_author_date`: same two skips (superscript runs come from `find_superscript_citation_runs()`, so already skipped; bracket matches check `result_spans`). Use `expand_bracket_numbers` for superscript number extraction.

**Step 4: Run tests**

Run: `python -m pytest -q`
Expected: all pass, including the updated `test_existing_citation_parser.py` (if it asserted `_expand_citation_range`, update that test to call `expand_bracket_numbers`).

**Step 5: Commit**

```bash
git add src/pipeline/citation_numbers.py src/pipeline/renumber_apply.py src/pipeline/existing_citation_parser.py src/pipeline/author_date_convert.py src/services/docx_io.py tests/test_field_aware_scanners.py tests/test_existing_citation_parser.py
git commit -m "fix: field-aware citation scanners; expand superscript ranges (3-5 keeps 4)"
```

---

### Task 6: Single numbering engine fed by events; fix post-heading markers and seed every entry

**Files:**
- Modify: `src/pipeline/renumbering.py:103-204`
- Modify: `src/pipeline/renumber_plan.py:98` (call `compute_renumbering` — unchanged API)
- Test: `tests/test_renumbering.py` (append)

**Behaviour:**
- New dataclass `CitationEvent(para_index, char_offset, kind, number=0, citations=())` with `kind in ('existing', 'new')`.
- `build_events(existing, new_markers) -> list[CitationEvent]`: existing in-text events for paragraphs `< references_heading_para_idx` (or all paragraphs when the heading index is `< 0`), new-marker events for **every** paragraph (a marker after the heading is a real citation site), sorted by `(para_index, char_offset)` with existing-before-new on ties as today.
- `compute_renumbering_from_events(existing, events, seed_entries=True) -> RenumberingResult`: today's loop over events. When `seed_entries`, after the walk every `existing.bib_entries` entry with no assignment gets one appended in original-number order with `is_new=False`; the result gains `seeded_uncited: list[int]` (original numbers) so callers can report them.
- `compute_renumbering(existing, new_markers, seed_entries=True)` = `compute_renumbering_from_events(existing, build_events(existing, new_markers), seed_entries)`.

**Step 1: Write the failing tests** (append to `tests/test_renumbering.py`)

```python
from src.pipeline.renumbering import build_events, compute_renumbering_from_events


class TestEventsEngine:
    def test_marker_after_heading_gets_a_number(self):
        existing = make_existing(num_entries=1, refs_heading=2, citations={0: [(0, 1)]})
        new = [NewMarkerInfo(para_index=5, char_offset=0, citations=[cand(pmid="9")])]
        result = compute_renumbering(existing, new)
        assert result.number_for_candidate(cand(pmid="9")) == 2

    def test_uncited_parsed_entries_are_seeded_and_reported(self):
        existing = make_existing(num_entries=4, refs_heading=3, citations={0: [(0, 2)]})
        result = compute_renumbering(existing, [])
        assert sorted(result.assignments) == [1, 2, 3, 4]
        assert result.assignments[1].original_number == 2       # cited first
        assert [result.assignments[n].original_number for n in (2, 3, 4)] == [1, 3, 4]
        assert result.seeded_uncited == [1, 3, 4]
        assert result.renumber_map == {2: 1}

    def test_seeding_can_be_disabled(self):
        existing = make_existing(num_entries=3, refs_heading=3, citations={0: [(0, 1)]})
        result = compute_renumbering(existing, [], seed_entries=False)
        assert sorted(result.assignments) == [1]

    def test_events_are_ordered_and_existing_wins_ties(self):
        existing = make_existing(num_entries=2, refs_heading=4, citations={1: [(5, 2)], 0: [(0, 1)]})
        new = [NewMarkerInfo(para_index=1, char_offset=5, citations=[cand(pmid="7")])]
        ev = build_events(existing, new)
        assert [(e.para_index, e.char_offset, e.kind) for e in ev] == [
            (0, 0, "existing"), (1, 5, "existing"), (1, 5, "new")]

    def test_from_events_matches_wrapper(self):
        existing = make_existing(num_entries=2, refs_heading=4, citations={0: [(0, 2), (3, 1)]})
        new = [NewMarkerInfo(para_index=0, char_offset=1, citations=[cand(doi="10.1/n")])]
        a = compute_renumbering(existing, new)
        b = compute_renumbering_from_events(existing, build_events(existing, new))
        assert a.renumber_map == b.renumber_map
        assert {n: x.bib_key for n, x in a.assignments.items()} == {n: x.bib_key for n, x in b.assignments.items()}
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_renumbering.py -q`
Expected: FAIL (`ImportError: build_events`)

**Step 3: Implement** in `src/pipeline/renumbering.py`:

```python
@dataclass
class CitationEvent:
    para_index: int
    char_offset: int
    kind: str                         # 'existing' | 'new'
    number: int = 0                   # existing: the in-text number
    citations: list = field(default_factory=list)   # new: resolved candidates
    record_uuid: str = ""             # tracked docs (Phase 1)


def build_events(existing: ExistingCitationMap, new_markers: list[NewMarkerInfo]) -> list[CitationEvent]:
    events: list[CitationEvent] = []
    heading = existing.references_heading_para_idx
    for para_idx, cites in existing.in_text_citations.items():
        if heading >= 0 and para_idx >= heading:
            continue
        for c in cites:
            events.append(CitationEvent(para_idx, c.char_offset, 'existing', number=c.number,
                                        record_uuid=getattr(c, 'record_uuid', '')))
    for m in new_markers:
        events.append(CitationEvent(m.para_index, m.char_offset, 'new', citations=list(m.citations)))
    order = {'existing': 0, 'new': 1}
    events.sort(key=lambda e: (e.para_index, e.char_offset, order[e.kind]))
    return events
```

`RenumberingResult` gains `seeded_uncited: list[int] = field(default_factory=list)`. `compute_renumbering_from_events` is today's loop body over `events` (same assignment logic), followed by:

```python
    if seed_entries:
        assigned_old = {a.original_number for a in result.assignments.values() if not a.is_new}
        for old_num in sorted(existing.bib_entries):
            if old_num in assigned_old:
                continue
            bib_key = _existing_bib_key(existing, old_num, key_index)
            if bib_key in key_to_number:      # same paper cited under another number
                result.renumber_map[old_num] = key_to_number[bib_key]
                continue
            key_to_number[bib_key] = current_num
            result.assignments[current_num] = CitationAssignment(
                final_number=current_num, is_new=False, original_number=old_num, bib_key=bib_key)
            result.renumber_map[old_num] = current_num
            result.seeded_uncited.append(old_num)
            current_num += 1
```

`compute_renumbering(existing, new_markers, seed_entries=True)` becomes the two-line wrapper. Keep the log lines.

**Step 4: Run tests**

Run: `python -m pytest -q`
Expected: all pass. If `tests/test_renumbering.py` or `test_round_trip.py` asserted an exact assignment count that seeding now changes, update those assertions (seeding is the intended new behaviour: parsed entries never vanish).

**Step 5: Commit**

```bash
git add src/pipeline/renumbering.py tests/test_renumbering.py
git commit -m "fix(renumbering): events engine; number markers after the heading; seed uncited entries"
```

---

### Task 7: Tracking report model; `analyze()` never raises; foreign-field detection

**Files:**
- Create: `src/models/embedded.py`
- Modify: `src/models/existing_refs.py` (add `tracking`, `pending_tracked_changes`, `bibliography_span`, `heading_para_idx_found`; delete the dead `bib_key` property after `grep -rn "\.bib_key" src` shows only `CitationAssignment.bib_key` uses)
- Modify: `src/pipeline/existing_citation_parser.py:43-78` (`analyze`)
- Test: `tests/test_tracking_report.py`

**Step 1: Write the failing test**

```python
# tests/test_tracking_report.py
from docx import Document

from src.models.embedded import DocumentTier, TrackingReport
from src.pipeline.existing_citation_parser import ExistingCitationParser
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder


def test_legacy_document_reports_legacy_tier(tmp_path):
    b = DocBuilder()
    p = b.paragraph("Claim")
    b.add_text(p, "1", superscript=True)
    b.paragraph("References")
    b.paragraph("1. A. T. J. 2020. doi:10.1/a")
    h = DocxHandler(str(b.save(tmp_path / "l.docx")))
    m = ExistingCitationParser(h).analyze()
    assert m.tracking.tier == DocumentTier.LEGACY
    assert m.tracking.field_count == 0 and m.tracking.foreign_field_count == 0
    assert m.has_existing_citations


def test_foreign_fields_are_counted_not_read(tmp_path):
    b = DocBuilder()
    p = b.paragraph("Claim")
    b.add_field(p, code=" ADDIN EN.CITE <EndNote/> ", result="1", superscript=True)
    b.paragraph("References")
    b.paragraph("1. A. T. J. 2020.")
    h = DocxHandler(str(b.save(tmp_path / "f.docx")))
    m = ExistingCitationParser(h).analyze()
    assert m.tracking.foreign_field_count == 1
    assert m.tracking.tier == DocumentTier.LEGACY
    assert any("another reference manager" in p for p in m.tracking.problems)


def test_analysis_failure_is_reported_not_raised(tmp_path, monkeypatch):
    b = DocBuilder()
    b.paragraph("Claim")
    h = DocxHandler(str(b.save(tmp_path / "x.docx")))
    parser = ExistingCitationParser(h)
    monkeypatch.setattr(parser, "_find_references_heading", lambda paras: 1 / 0)
    m = parser.analyze()
    assert m.tracking.tier == DocumentTier.FAILED
    assert m.tracking.problems and "ZeroDivisionError" in m.tracking.problems[0]
    assert not m.has_existing_citations


def test_pending_tracked_changes_flag(tmp_path):
    b = DocBuilder()
    p = b.paragraph("Claim")
    b.add_field(p, code=' ADDIN AIREFS.CITE {"a":1} ', result="1", wrap="del")
    h = DocxHandler(str(b.save(tmp_path / "t.docx")))
    m = ExistingCitationParser(h).analyze()
    assert m.pending_tracked_changes is True


def test_tracking_report_roundtrips_through_json():
    r = TrackingReport(tier=DocumentTier.LEGACY, problems=["p"])
    assert TrackingReport.model_validate_json(r.model_dump_json()) == r
```

**Step 2: Run to verify failure**

Run: `python -m pytest tests/test_tracking_report.py -q`
Expected: FAIL (`No module named 'src.models.embedded'`)

**Step 3: Implement**

```python
# src/models/embedded.py
"""Models describing embedded (field-based) citation tracking in a document."""

from enum import Enum
from pydantic import BaseModel, Field, ConfigDict


class DocumentTier(str, Enum):
    TRACKED = "tracked"          # AIREFS.CITE fields present: exact, offline reopen
    LEGACY = "legacy"            # plain-text citations: regex + PubMed path
    STRIPPED = "stripped"        # project knows this document but fields are gone
    FAILED = "failed"            # analysis raised; nothing trusted
    NEWER_VERSION = "newer_version"  # payload schema newer than this app: read-only


class ReconcileIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: str = ""               # pasted | duplicate | hand_edited | ... (Task 17)
    cid: str = ""
    message: str = ""


class TrackingReport(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tier: DocumentTier = DocumentTier.LEGACY
    doc_id: str = ""
    schema_version: int = 0
    field_count: int = 0          # AIREFS.CITE fields
    bibl_field_count: int = 0
    foreign_field_count: int = 0  # ADDIN fields from other reference managers
    fields_in_tables: int = 0
    record_count: int = 0
    problems: list[str] = Field(default_factory=list)
    reconcile: list[ReconcileIssue] = Field(default_factory=list)
```

`ExistingCitationMap` additions: `tracking: Optional[TrackingReport] = None`, `pending_tracked_changes: bool = False`, `bibliography_span: tuple[int, int] = (-1, -1)`, `heading_para_idx_found: bool = True`.

`ExistingCitationParser.analyze()`:

```python
    def analyze(self) -> ExistingCitationMap:
        result = ExistingCitationMap()
        report = TrackingReport()
        result.tracking = report
        try:
            idx = self.handler.fields
            report.field_count = idx.airefs_cite
            report.bibl_field_count = idx.airefs_bibl
            report.foreign_field_count = idx.foreign
            report.fields_in_tables = idx.in_tables
            result.pending_tracked_changes = idx.pending_tracked_changes
            if report.foreign_field_count:
                report.problems.append(
                    f"{report.foreign_field_count} citation field(s) from another reference "
                    "manager were found. They are left untouched; export is disabled.")
            if idx.airefs_cite:
                return self._analyze_tracked(result, idx)      # Task 16; until then: fall through
            self._analyze_legacy(result)
            report.tier = DocumentTier.LEGACY
        except Exception as exc:                                # never raise: report instead
            logger.exception("Existing-citation analysis failed")
            result = ExistingCitationMap(tracking=TrackingReport(
                tier=DocumentTier.FAILED, problems=[f"{type(exc).__name__}: {exc}"]))
        return result
```

`_analyze_legacy(result)` is today's steps 1–4 (heading, bibliography, in-text scan, superscript detection) moved verbatim; until Task 16 exists, `_analyze_tracked` simply calls `_analyze_legacy` and sets `tier = TRACKED` only when the reader exists — for now leave a `NotImplementedError`-free stub that delegates to legacy (tests in this task do not include AIREFS fields except the tracked-changes one, which has no References heading and therefore stays `LEGACY` with `has_existing_citations == False`).

**Step 4: Run tests**

Run: `python -m pytest -q`
Expected: all pass

**Step 5: Commit**

```bash
git add src/models/embedded.py src/models/existing_refs.py src/pipeline/existing_citation_parser.py tests/test_tracking_report.py
git commit -m "feat: tracking report; analysis never raises; count foreign fields"
```

---

### Task 8: Bounded References removal and the export hard guard

**Files:**
- Modify: `src/services/docx_io.py:262-275` (`remove_references_section`)
- Create: `src/pipeline/docx_export.py` (guard only in this task)
- Modify: `src/models/project.py` (`ProjectSettings.min_match_ratio: float = 0.5`)
- Test: `tests/test_export_guard.py`

**Behaviour:**
- `remove_references_section(start_para_idx, end_para_idx=None)`: when `end_para_idx` is None, compute the bound as the last paragraph after the heading that is either blank or matches `BIB_ENTRY_PATTERN`; stop at the first non-blank, non-entry paragraph (an appendix heading, acknowledgements…). Remove `[start, bound]` only. Return the number of removed paragraphs.
- `ExportBlocked(Exception)` carries `reasons: list[str]`.
- `check_export_guard(existing, mode, settings) -> list[str]` where `mode in ('fresh', 'legacy', 'tracked')`:
  1. `existing.tracking.tier == FAILED` → "Analysis of existing citations failed: …".
  2. `existing.tracking.foreign_field_count > 0` → "…another reference manager…".
  3. `mode == 'fresh'` and (`existing.references_heading_para_idx >= 0` or `existing.bib_entries`) → "This document already has a References section; a fresh export would append a second one."
  4. `mode == 'legacy'` and `existing.bib_entries`: `matched = len({c.number for cites in existing.in_text_citations.values() for c in cites})`; if `matched == 0` or `matched / len(existing.bib_entries) < settings.min_match_ratio` → "Only N of M reference entries were matched to in-text citations (ratio); refusing to rebuild the bibliography."
  5. `existing.pending_tracked_changes and not settings.allow_export_with_tracked_changes` → "Pending tracked changes…" (setting added here, default False).
- `main_window._do_export` calls it first and shows the reasons (Task 9 wires the GUI; here only the function).

**Step 1: Write the failing test**

```python
# tests/test_export_guard.py
import pytest

from src.models.embedded import DocumentTier, TrackingReport
from src.models.existing_refs import ExistingBibEntry, ExistingCitationMap, InTextCitation
from src.models.project import ProjectSettings
from src.pipeline.docx_export import check_export_guard
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder


def _map(n_entries, matched_numbers, heading=3, tier=DocumentTier.LEGACY, foreign=0, tracked=False):
    m = ExistingCitationMap(
        bib_entries={i: ExistingBibEntry(original_number=i, raw_text=f"{i}. x") for i in range(1, n_entries + 1)},
        references_heading_para_idx=heading,
        in_text_citations={0: [InTextCitation(char_offset=k, number=n) for k, n in enumerate(matched_numbers)]},
        tracking=TrackingReport(tier=tier, foreign_field_count=foreign),
        pending_tracked_changes=tracked,
    )
    return m


def test_fresh_export_blocked_when_bibliography_exists():
    reasons = check_export_guard(_map(2, [1]), "fresh", ProjectSettings())
    assert any("already has a References" in r for r in reasons)


def test_fresh_export_allowed_on_uncited_document():
    m = ExistingCitationMap(tracking=TrackingReport())
    assert check_export_guard(m, "fresh", ProjectSettings()) == []


@pytest.mark.parametrize("n,matched,blocked", [(50, [1, 2, 3], True), (10, [], True), (10, [1, 2, 3, 4, 5], False), (4, [1, 2, 3, 4], False)])
def test_ratio_guard(n, matched, blocked):
    reasons = check_export_guard(_map(n, matched), "legacy", ProjectSettings())
    assert bool(reasons) is blocked


def test_ratio_threshold_is_configurable():
    reasons = check_export_guard(_map(50, [1, 2, 3]), "legacy", ProjectSettings(min_match_ratio=0.05))
    assert reasons == []


def test_failed_analysis_and_foreign_fields_block():
    assert check_export_guard(_map(1, [1], tier=DocumentTier.FAILED), "legacy", ProjectSettings())
    assert check_export_guard(_map(1, [1], foreign=2), "legacy", ProjectSettings())


def test_tracked_changes_block_unless_allowed():
    assert check_export_guard(_map(1, [1], tracked=True), "legacy", ProjectSettings())
    assert check_export_guard(_map(1, [1], tracked=True), "legacy", ProjectSettings(allow_export_with_tracked_changes=True)) == []


def test_bounded_removal_keeps_appendix(tmp_path):
    b = DocBuilder()
    b.paragraph("Body")
    b.paragraph("References")
    b.paragraph("1. A. T. J. 2020.")
    b.paragraph("")
    b.paragraph("2. B. T. J. 2021.")
    b.paragraph("Appendix A")
    b.paragraph("Supplementary text.")
    h = DocxHandler(str(b.save(tmp_path / "a.docx")))
    removed = h.remove_references_section(1)
    assert removed == 4
    assert [p.text for p in h.get_paragraphs()] == ["Body", "Appendix A", "Supplementary text."]
```

**Step 2: Run to verify failure**

Run: `python -m pytest tests/test_export_guard.py -q`
Expected: FAIL (`No module named 'src.pipeline.docx_export'`)

**Step 3: Implement**

`docx_io.remove_references_section`:

```python
    def remove_references_section(self, start_para_idx: int, end_para_idx: Optional[int] = None) -> int:
        from ..pipeline.existing_citation_parser import BIB_ENTRY_PATTERN
        paragraphs = self.doc.paragraphs
        if end_para_idx is None:
            end_para_idx = start_para_idx
            for i in range(start_para_idx + 1, len(paragraphs)):
                text = paragraphs[i].text.strip()
                if not text or BIB_ENTRY_PATTERN.match(text):
                    end_para_idx = i
                    continue
                break
            # trailing blank paragraphs after the last entry are left alone
            while end_para_idx > start_para_idx and not paragraphs[end_para_idx].text.strip():
                end_para_idx -= 1
        body_elem = self.doc.element.body
        for i in range(end_para_idx, start_para_idx - 1, -1):
            body_elem.remove(paragraphs[i]._element)
        self.invalidate_fields()
        removed = end_para_idx - start_para_idx + 1
        logger.info(f"Removed {removed} paragraphs (References section from para {start_para_idx})")
        return removed
```

(The heading-to-EOF behaviour is gone on purpose. The blank between entries in the test is inside the range, so 4 paragraphs go: heading, entry, blank, entry.)

`src/pipeline/docx_export.py` (initial):

```python
"""Headless export: guard, then fresh / legacy / tracked writers (Tasks 15, 17, 20)."""

import logging
from ..models.embedded import DocumentTier
from ..models.existing_refs import ExistingCitationMap
from ..models.project import ProjectSettings

logger = logging.getLogger(__name__)


class ExportBlocked(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def check_export_guard(existing: ExistingCitationMap | None, mode: str,
                       settings: ProjectSettings) -> list[str]:
    reasons: list[str] = []
    if existing is None:
        return reasons
    tr = existing.tracking
    if tr is not None and tr.tier == DocumentTier.FAILED:
        reasons.append("Analysis of the document's existing citations failed: "
                       + "; ".join(tr.problems))
        return reasons
    if tr is not None and tr.foreign_field_count:
        reasons.append(f"{tr.foreign_field_count} citation field(s) from another reference manager "
                       "are present. AI REFs does not edit those documents.")
    if mode == "fresh" and (existing.references_heading_para_idx >= 0 or existing.bib_entries):
        reasons.append("This document already has a References section; a fresh export would "
                       "append a second one. Reload it so AI REFs can detect the existing citations.")
    if mode == "legacy" and existing.bib_entries:
        matched = len({c.number for cites in existing.in_text_citations.values() for c in cites})
        total = len(existing.bib_entries)
        if matched == 0 or matched / total < settings.min_match_ratio:
            reasons.append(f"Only {matched} of {total} reference entries could be matched to in-text "
                           f"citations ({matched / total:.0%}). Refusing to rebuild the bibliography; "
                           "check the document's citation format.")
    if existing.pending_tracked_changes and not settings.allow_export_with_tracked_changes:
        reasons.append("The document has pending tracked changes around citations. Accept or "
                       "reject them in Word first (or enable 'Export anyway' in settings).")
    return reasons
```

Add to `ProjectSettings`: `min_match_ratio: float = Field(default=0.5, ge=0.0, le=1.0)` and `allow_export_with_tracked_changes: bool = Field(default=False)`.

**Step 4: Run tests**

Run: `python -m pytest -q`
Expected: all pass

**Step 5: Commit**

```bash
git add src/services/docx_io.py src/pipeline/docx_export.py src/models/project.py tests/test_export_guard.py
git commit -m "fix: bounded References removal; ratio-based export guard"
```

---

### Task 9: Wire the guards into the GUI; atomic save; input-path and project-hash checks

**Files:**
- Modify: `src/services/docx_io.py:292-295` (`save`)
- Modify: `src/gui/main_window.py:140-192` (`_on_file_selected`), `:243-287` (`_export_document`), `:345-352` (`_do_export`), `:700-724` (`_open_project`)
- Modify: `src/gui/inputs_tab.py:658-668` (`set_insert_mode` → `set_document_mode`)
- Test: `tests/test_docx_save.py` (headless parts only)

**Behaviour:**
- `DocxHandler.save(output_path)` writes to `<output>.tmp` in the same directory, then `os.replace` onto the target.
- `_export_document`: after the path dialog, if `Path(output_path).resolve() == Path(input).resolve()`, copy the input to `<input>.bak` (`shutil.copy2`) and warn with a message box ("The original was backed up to …"). Then `reasons = check_export_guard(self._project.existing_citations, mode, settings)` where `mode` is `'legacy'` if insert mode else `'fresh'`; if reasons, show `QMessageBox.critical("Export blocked", "\n\n".join(reasons))` and return.
- `_on_file_selected`: always run `ExistingCitationParser(handler).analyze()`; `existing.tracking.tier == FAILED` → `is_insert_mode = False`, keep `existing_citations = existing` (so the guard sees the failure), banner mode `analysis-failed`; foreign fields → banner mode `foreign`; else insert mode iff `has_existing_citations` (as today). Replace `self.inputs_tab.set_insert_mode(...)` calls with `self.inputs_tab.set_document_mode(mode, report, n_existing)` where mode ∈ `{'fresh','legacy','foreign','analysis-failed'}` (Task 19 adds `tracked`, `stripped`, `newer-version`).
- `inputs_tab.set_document_mode(mode, report, n_existing)`: one label, text per mode:
  - fresh: hidden
  - legacy: today's insert-mode text
  - foreign: "This document contains citation fields from another reference manager (EndNote/Zotero/Mendeley). AI REFs will not modify it."
  - analysis-failed: "AI REFs could not analyse the existing citations: <first problem>. Export is disabled."
  Keep `set_insert_mode` as a thin wrapper calling `set_document_mode` for other callers.
- `_open_project`: after `load_project`, if `input_docx_path` exists on disk, hash it; if it differs from `project.input_docx_hash`, re-run the analysis (reuse `_on_file_selected`'s analysis via a new `_analyze_document(path)` helper) and set `statusBar` message "Document changed since the project was saved; re-analysed." Otherwise apply the banner from `project.existing_citations.tracking`.

**Step 1: Write the failing test**

```python
# tests/test_docx_save.py
import os

from docx import Document

from src.services.docx_io import DocxHandler


def test_save_is_atomic_and_leaves_no_tmp(tmp_path):
    src = tmp_path / "in.docx"
    Document().save(str(src))
    h = DocxHandler(str(src))
    h.doc.add_paragraph("added")
    out = tmp_path / "out.docx"
    h.save(str(out))
    assert Document(str(out)).paragraphs[-1].text == "added"
    assert not (tmp_path / "out.docx.tmp").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.docx", "out.docx"]


def test_save_over_input_path_works(tmp_path):
    src = tmp_path / "in.docx"
    Document().save(str(src))
    h = DocxHandler(str(src))
    h.doc.add_paragraph("again")
    h.save(str(src))
    assert Document(str(src)).paragraphs[-1].text == "again"
```

**Step 2: Run to verify failure**

Run: `python -m pytest tests/test_docx_save.py -q`
Expected: first test FAILS only if `.tmp` handling is missing (today's `save` writes directly; the test asserting no `.tmp` passes trivially, so assert the implementation by monkeypatching `os.replace` to record a call):

Add to the first test: `import src.services.docx_io as m; calls = []; monkeypatch.setattr(m.os, "replace", lambda a, b: calls.append((a, b)) or os.rename(a, b)); ...; assert calls and calls[0][0].endswith(".tmp")`. With that, expected: FAIL (`AttributeError: module has no attribute 'os'` or `calls == []`).

**Step 3: Implement**

```python
    def save(self, output_path: str):
        import os
        tmp = f"{output_path}.tmp"
        self.doc.save(tmp)
        os.replace(tmp, output_path)
        logger.info(f"Document saved to {output_path}")
```

(`import os` at module top so the monkeypatch target `docx_io.os` exists.) Then the GUI edits described above. `_analyze_document(path) -> ExistingCitationMap` extracts lines 159-188 of `_on_file_selected` into a helper that returns the map and sets `is_insert_mode` / `existing_citations` / banner.

**Step 4: Run tests and start the app**

Run: `python -m pytest -q` → all pass.
Run: `python -m src.app`, load `fixtures/complex_input.docx`, confirm the banner behaves; try exporting onto the input path and confirm the `.bak` warning; open a saved project after editing its DOCX and confirm the "re-analysed" status message.

**Step 5: Commit**

```bash
git add src/services/docx_io.py src/gui/main_window.py src/gui/inputs_tab.py tests/test_docx_save.py
git commit -m "feat(gui): export guard, atomic save, input-path backup, project hash check"
```

---

### Task 10: One split predicate; paragraph-index regression test

**Files:**
- Modify: `src/models/sentence.py` (add `searched_per_marker` property)
- Modify: `src/pipeline/orchestrator.py:289-290`, `src/gui/main_window.py:394`, `src/pipeline/renumber_plan.py:56`
- Test: `tests/test_paragraph_index.py`

**Step 1: Write the failing test**

```python
# tests/test_paragraph_index.py
import glob

import pytest
from docx import Document

from src.models.sentence import MarkerType, SentenceRecord
from src.pipeline.document_parser import DocumentParser
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder


def test_searched_per_marker_predicate():
    s = SentenceRecord(marker_type=MarkerType.REFS, marker_count=2,
                       marker_types=[MarkerType.REF, MarkerType.REFS])
    assert s.searched_per_marker is True
    s2 = SentenceRecord(marker_type=MarkerType.REFS, marker_count=2,
                        marker_types=[MarkerType.REFS, MarkerType.REFS])
    assert s2.searched_per_marker is False
    s3 = SentenceRecord(marker_type=MarkerType.REF, marker_count=1)
    assert s3.searched_per_marker is False


@pytest.mark.parametrize("path", sorted(glob.glob("fixtures/*.docx")))
def test_parser_and_marker_indices_share_doc_paragraphs_space(path):
    h = DocxHandler(path)
    sentences = DocumentParser(h).parse()
    markers = h.find_markers()
    paras = h.get_paragraphs()
    for s in sentences:
        assert s.raw_text in paras[s.paragraph_index].text
    for m in markers:
        assert paras[m["para_index"]] is m["paragraph"]


def test_indices_survive_hyperlinks_and_tracked_changes(tmp_path):
    b = DocBuilder()
    p = b.paragraph("Intro ")
    b.add_hyperlink(b.paragraph("Link "), "https://x.org", "site")
    p3 = b.paragraph("Claim one (REF). ")
    b.add_text(p3, "inserted", wrap="ins")
    b.paragraph("Claim two (REFS).")
    h = DocxHandler(str(b.save(tmp_path / "i.docx")))
    sentences = [s for s in DocumentParser(h).parse() if "(REF" in s.raw_text]
    markers = h.find_markers()
    assert [s.paragraph_index for s in sentences] == [2, 3]
    assert [m["para_index"] for m in markers] == [2, 3]
```

**Step 2: Run to verify failure**

Run: `python -m pytest tests/test_paragraph_index.py -q`
Expected: FAIL (`AttributeError: 'SentenceRecord' object has no attribute 'searched_per_marker'`)

**Step 3: Implement**

```python
    @property
    def searched_per_marker(self) -> bool:
        """True when the orchestrator ran one search per marker.

        Mixed sentences with at least one (REF) get independent per-marker
        searches; all-(REFS) sentences keep one combined search.  The export
        and renumbering code must slice evidence.selected the same way, so
        this is the single home for the rule.
        """
        types = self.effective_marker_types()
        return len(types) > 1 and MarkerType.REF in types
```

Replace the three inline predicates with `sent.searched_per_marker` (orchestrator: `if sent.searched_per_marker:`; main_window and renumber_plan: `if sent.searched_per_marker:` inside the slot expansion) and delete the "must stay in sync" comments.

**Step 4: Run tests**

Run: `python -m pytest -q` → all pass.

**Step 5: Commit**

```bash
git add src/models/sentence.py src/pipeline/orchestrator.py src/gui/main_window.py src/pipeline/renumber_plan.py tests/test_paragraph_index.py
git commit -m "refactor: single searched_per_marker predicate; paragraph-index regression tests"
```

**Phase 0 acceptance:** `python -m pytest -q` green; the five live bugs each have a test (Tasks 4, 5, 6, 8); `python -m src.app` runs an insert-mode export on `fixtures/complex_input.docx` without regressions. Tag: `git tag phase0-hygiene`.

---

## Phase 1 — Embedded citation fields

### Task 11: CSL mapping and record identity on `CitationCandidate`

**Files:**
- Modify: `src/models/citation.py` (add `record_uuid: str = ""`, `pmcid: str = ""`, `raw_entry: str = ""`)
- Create: `src/pipeline/csl_mapping.py`
- Test: `tests/test_csl_mapping.py`

**Behaviour:**
- `normalize_doi(doi)`: strip, remove `https://doi.org/` / `http://dx.doi.org/` / `doi:` prefixes, lowercase; `''` for empty.
- `ensure_record_uuid(c)`: set `c.record_uuid = str(uuid.uuid4())` if empty; return it.
- `to_csl_item(c) -> dict`: `{"id": record_uuid, "type": "article-journal", "title", "author": [{"family": last, "given": first or initials}], "container-title": journal, "container-title-short": journal_abbrev, "volume", "issue", "page": pages, "issued": {"date-parts": [[year]]} (omitted when year == 0), "DOI": normalize_doi(doi), "PMID", "PMCID", "custom": {"airefs": {"source", "authorCount": len(authors), "authorsTruncated": False, "isReview", "retracted": is_retracted, "retractionNotice", "hasErratum", "publicationTypes", "meshTerms": mesh_terms[:30], "rawEntry": raw_entry}}}`. Empty strings are omitted except `id`, `type`, `title`. Never `abstract`.
- `from_csl_item(item) -> CitationCandidate`: inverse; unknown keys ignored; `source` defaults to `"embedded"`; `Author.initials` is derived from `given` (first letters of each given-name token) when `given` looks like a full name, or copied when it is already initials.
- `build_uris(c) -> list[str]` in the fixed order `airefs:record/<uuid>`, `https://doi.org/<doi>`, `https://pubmed.ncbi.nlm.nih.gov/<pmid>/`, `https://www.ncbi.nlm.nih.gov/pmc/articles/<pmcid>/`, and `airefs:hash/<sha1(normalized title|year)>` only when there is no DOI and no PMID.
- `identity_from_uris(uris) -> dict(record_uuid, doi, pmid, pmcid)` parses them back.

**Step 1: Write the failing test**

```python
# tests/test_csl_mapping.py
import uuid

from src.models.citation import Author, CitationCandidate
from src.pipeline.csl_mapping import (
    build_uris, ensure_record_uuid, from_csl_item, identity_from_uris,
    normalize_doi, to_csl_item,
)


def full_candidate():
    return CitationCandidate(
        pmid="31978345", doi="10.1016/J.CELL.2020.01.001", pmcid="PMC7000000",
        title="Synaptic proteomes <in> vivo & beyond — ünïcode", source="pubmed",
        authors=[Author(last_name="Smith", first_name="Jane", initials="J"),
                 Author(last_name="Doe", first_name="", initials="AB")],
        year=2020, journal="Cell", journal_abbrev="Cell", volume="180", issue="2", pages="1-10",
        abstract="SHOULD NOT TRAVEL", mesh_terms=["Synapses", "Proteomics"],
        publication_types=["Journal Article", "Review"], is_retracted=True, is_review=True,
        retraction_notice="Retracted 2021", has_erratum=True, record_uuid=str(uuid.uuid4()),
    )


def test_round_trip_is_lossless_except_abstract_and_scores():
    c = full_candidate()
    c.composite_score = 0.9
    back = from_csl_item(to_csl_item(c))
    expect = c.model_copy(update={"abstract": "", "composite_score": 0.0})
    back.source = c.source
    assert back.model_dump() == expect.model_dump()


def test_item_shape_and_no_abstract():
    item = to_csl_item(full_candidate())
    assert item["type"] == "article-journal"
    assert item["DOI"] == "10.1016/j.cell.2020.01.001"
    assert item["issued"] == {"date-parts": [[2020]]}
    assert item["author"][0] == {"family": "Smith", "given": "Jane"}
    assert item["author"][1] == {"family": "Doe", "given": "AB"}
    assert "abstract" not in item
    assert item["custom"]["airefs"]["authorCount"] == 2
    assert item["custom"]["airefs"]["retracted"] is True


def test_unknown_keys_and_missing_fields_are_tolerated():
    c = from_csl_item({"id": "x", "type": "article-journal", "title": "T", "future": 1,
                       "custom": {"airefs": {"newKey": True}}})
    assert c.title == "T" and c.year == 0 and c.authors == [] and c.source == "embedded"


def test_uris_order_and_parse():
    c = full_candidate()
    uris = build_uris(c)
    assert uris[0] == f"airefs:record/{c.record_uuid}"
    assert uris[1] == "https://doi.org/10.1016/j.cell.2020.01.001"
    assert uris[2] == "https://pubmed.ncbi.nlm.nih.gov/31978345/"
    assert uris[3] == "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7000000/"
    assert identity_from_uris(uris) == {"record_uuid": c.record_uuid, "doi": "10.1016/j.cell.2020.01.001",
                                        "pmid": "31978345", "pmcid": "PMC7000000"}


def test_hash_uri_only_without_ids():
    c = CitationCandidate(title="Only a title", year=1999)
    ensure_record_uuid(c)
    uris = build_uris(c)
    assert len(uris) == 2 and uris[1].startswith("airefs:hash/")
    assert build_uris(CitationCandidate(title="x", doi="10.1/y", record_uuid="u"))[1].startswith("https://doi.org/")


def test_normalize_doi():
    assert normalize_doi("https://doi.org/10.1/ABC") == "10.1/abc"
    assert normalize_doi("doi:10.1/ABC") == "10.1/abc"
    assert normalize_doi("") == ""


def test_ensure_record_uuid_is_stable():
    c = CitationCandidate(title="t")
    u = ensure_record_uuid(c)
    assert ensure_record_uuid(c) == u and len(u) == 36
```

**Step 2: Run to verify failure**

Run: `python -m pytest tests/test_csl_mapping.py -q`
Expected: FAIL (`No module named 'src.pipeline.csl_mapping'`)

**Step 3: Implement** `src/pipeline/csl_mapping.py` per the behaviour list. Sketch of the tricky bits:

```python
def _author_to_csl(a):
    given = a.first_name or a.initials
    return {k: v for k, v in (("family", a.last_name), ("given", given)) if v}

def _author_from_csl(d):
    given = d.get("given", "") or ""
    tokens = given.replace(".", " ").split()
    looks_like_initials = bool(given) and all(len(t) <= 2 and t.isupper() for t in tokens)
    if looks_like_initials:
        return Author(last_name=d.get("family", ""), first_name="", initials="".join(tokens))
    initials = "".join(t[0].upper() for t in tokens)
    return Author(last_name=d.get("family", ""), first_name=given, initials=initials)
```

Note for the round-trip test: `Author(first_name="Jane", initials="J")` → given `"Jane"` → back to `first_name="Jane", initials="J"`; `Author(first_name="", initials="AB")` → given `"AB"` → looks like initials → `initials="AB"`. If `first_name="Jane Q"` the initials become `"JQ"`; acceptable.

**Step 4: Run tests** → `python -m pytest -q` all pass.

**Step 5: Commit**

```bash
git add src/models/citation.py src/pipeline/csl_mapping.py tests/test_csl_mapping.py
git commit -m "feat: CSL-JSON mapping, record uuids and identity URIs for candidates"
```

---

### Task 12: Field payloads (`citation_payload.py`)

**Files:**
- Create: `src/pipeline/citation_payload.py`
- Test: `tests/test_citation_payload.py`

**Behaviour:**
- Constants: `FIELD_SCHEMA_VERSION = 1`, `CITE_TOKEN = "AIREFS.CITE"`, `BIBL_TOKEN = "AIREFS.BIBL"`, `CSL_SCHEMA = "https://resource.citationstyles.org/schema/latest/input/json/csl-citation.json"`, `MAX_ITEM_BYTES = 16384`, `MAX_AUTHORS_WHEN_TRUNCATED = 30`, `RENDER_KINDS = ("numeric-superscript", "numeric-bracket", "numeric-paren", "author-date")`.
- `mint_citation_id() -> str`: 12 chars from `a-z2-7` via `secrets.choice`.
- `extract_json(code) -> str`: substring from the first `{` to the last `}`; `PayloadError` if absent.
- `build_cite_code(items: list[CitationCandidate], *, numbers: list[int], render: str, style: str, plain: str, citation_id: str | None = None, unresolved: bool = False) -> str` returns `f" ADDIN {CITE_TOKEN} {json} "` with the JSON (compact separators, `ensure_ascii=False`): `schema`, `citationID`, `properties: {formattedCitation, plainCitation, noteIndex: 0}`, `citationItems: [{id, uris, itemData}]` (uses `ensure_record_uuid`, `build_uris`, `to_csl_item`; applies the size safeguard per item: if `len(json.dumps(itemData))> MAX_ITEM_BYTES`, keep the first 30 authors and set `custom.airefs.authorsTruncated = True`), `airefs: {v, style, render, numbers, unresolved}`.
- `parse_cite_code(code) -> CitePayload` dataclass: `citation_id, items: list[CiteItem(record_uuid, uris, item: dict, identity: dict)], numbers, render, style, plain, unresolved, version, problems: list[str]`. Missing `citationID` → mint one and add a problem; `version > FIELD_SCHEMA_VERSION` → `NewerSchemaError(PayloadError)`; anything else lenient.
- `build_bibl_code(*, doc_id, style, render, heading_text, order: list[str], uncited: list[str], entry_hashes: list[str]) -> str` and `parse_bibl_code(code) -> BiblPayload(doc_id, style, render, heading_text, order, uncited, entry_hashes, version, exported_at, problems)`. `exported_at` is `datetime.now(timezone.utc).isoformat(timespec="seconds")`.
- `entry_hash(text) -> str`: sha1 of `text.strip()`.
- `is_cite_code(code)` / `is_bibl_code(code)` prefix tests (strip, then `startswith("ADDIN AIREFS.CITE")`).

**Step 1: Write the failing test**

```python
# tests/test_citation_payload.py
import json
import pytest

from src.models.citation import Author, CitationCandidate
from src.pipeline.citation_payload import (
    FIELD_SCHEMA_VERSION, NewerSchemaError, PayloadError, build_bibl_code, build_cite_code,
    entry_hash, extract_json, is_bibl_code, is_cite_code, mint_citation_id, parse_bibl_code,
    parse_cite_code,
)


def cand(i, **kw):
    return CitationCandidate(pmid=f"100{i}", doi=f"10.1/x{i}", title=f"Title <{i}> & co",
                             authors=[Author(last_name=f"A{i}", initials="B")], year=2000 + i, **kw)


def test_cite_code_shape_and_round_trip():
    code = build_cite_code([cand(1), cand(2)], numbers=[3, 5], render="numeric-superscript",
                           style="nih_grant", plain="3,5")
    assert code.startswith(" ADDIN AIREFS.CITE {") and code.endswith("} ")
    assert is_cite_code(code) and not is_bibl_code(code)
    p = parse_cite_code(code)
    assert len(p.citation_id) == 12 and set(p.citation_id) <= set("abcdefghijklmnopqrstuvwxyz234567")
    assert p.numbers == [3, 5] and p.render == "numeric-superscript" and p.style == "nih_grant"
    assert p.plain == "3,5" and p.unresolved is False and p.version == FIELD_SCHEMA_VERSION
    assert [it.identity["pmid"] for it in p.items] == ["1001", "1002"]
    assert p.items[0].item["title"] == "Title <1> & co"
    assert p.items[0].record_uuid and p.items[0].uris[0] == f"airefs:record/{p.items[0].record_uuid}"
    assert p.problems == []


def test_citation_id_is_preserved_and_mintable():
    code = build_cite_code([cand(1)], numbers=[1], render="numeric-bracket", style="ieee",
                           plain="[1]", citation_id="abcdefghijkl")
    assert parse_cite_code(code).citation_id == "abcdefghijkl"
    assert mint_citation_id() != mint_citation_id()


def test_unresolved_field_has_no_items():
    code = build_cite_code([], numbers=[], render="numeric-bracket", style="ieee", plain="[?]",
                           unresolved=True)
    p = parse_cite_code(code)
    assert p.unresolved and p.items == []


def test_extract_json_is_lenient_and_unknown_keys_ignored():
    code = ' ADDIN AIREFS.CITE   {"citationID":"x","future":{"a":[1]},"citationItems":[]}  junk '
    assert json.loads(extract_json(code))["future"] == {"a": [1]}
    p = parse_cite_code(code)
    assert p.citation_id == "x" and p.items == []


def test_missing_citation_id_is_minted_with_problem():
    p = parse_cite_code(' ADDIN AIREFS.CITE {"citationItems":[]} ')
    assert len(p.citation_id) == 12 and p.problems


def test_newer_schema_raises():
    with pytest.raises(NewerSchemaError):
        parse_cite_code(' ADDIN AIREFS.CITE {"citationID":"x","airefs":{"v":99}} ')


def test_garbage_raises_payload_error():
    with pytest.raises(PayloadError):
        parse_cite_code(" ADDIN AIREFS.CITE nothing here ")


def test_size_safeguard_truncates_authors():
    big = cand(1)
    big.authors = [Author(last_name=f"Consortium member {k}", first_name="Firstname " * 20) for k in range(400)]
    p = parse_cite_code(build_cite_code([big], numbers=[1], render="numeric-bracket", style="ieee", plain="[1]"))
    item = p.items[0].item
    assert len(item["author"]) == 30
    assert item["custom"]["airefs"]["authorsTruncated"] is True
    assert item["custom"]["airefs"]["authorCount"] == 400


def test_bibl_code_round_trip():
    code = build_bibl_code(doc_id="d1", style="nih_grant", render="numeric-superscript",
                           heading_text="References", order=["u1", "u2"], uncited=["u3"],
                           entry_hashes=[entry_hash("1. a"), entry_hash("2. b")])
    assert is_bibl_code(code)
    b = parse_bibl_code(code)
    assert (b.doc_id, b.style, b.render, b.heading_text) == ("d1", "nih_grant", "numeric-superscript", "References")
    assert b.order == ["u1", "u2"] and b.uncited == ["u3"] and len(b.entry_hashes) == 2
    assert b.version == FIELD_SCHEMA_VERSION and b.exported_at.endswith("+00:00")
```

**Step 2: Run to verify failure**

Run: `python -m pytest tests/test_citation_payload.py -q`
Expected: FAIL (`No module named 'src.pipeline.citation_payload'`)

**Step 3: Implement** per the behaviour list. Keep JSON compact: `json.dumps(obj, ensure_ascii=False, separators=(",", ":"))`. Parsing:

```python
def parse_cite_code(code: str) -> CitePayload:
    data = json.loads(extract_json(code))          # PayloadError on failure
    air = data.get("airefs") or {}
    version = int(air.get("v", 1))
    if version > FIELD_SCHEMA_VERSION:
        raise NewerSchemaError(f"field schema v{version} is newer than supported v{FIELD_SCHEMA_VERSION}")
    problems = []
    cid = data.get("citationID") or ""
    if not cid:
        cid = mint_citation_id(); problems.append("citationID missing; minted a new one")
    items = []
    for raw in data.get("citationItems") or []:
        item = raw.get("itemData") or {}
        uris = list(raw.get("uris") or [])
        ident = identity_from_uris(uris)
        rid = raw.get("id") or ident.get("record_uuid") or item.get("id") or ""
        if not rid:
            rid = str(uuid.uuid4()); problems.append("record id missing; minted")
        items.append(CiteItem(record_uuid=str(rid), uris=uris, item=item, identity=ident))
    props = data.get("properties") or {}
    return CitePayload(citation_id=cid, items=items, numbers=[int(n) for n in air.get("numbers") or []],
                       render=air.get("render", ""), style=air.get("style", ""),
                       plain=props.get("plainCitation", ""), unresolved=bool(air.get("unresolved", False)),
                       version=version, problems=problems)
```

**Step 4: Run tests** → `python -m pytest -q` all pass.

**Step 5: Commit**

```bash
git add src/pipeline/citation_payload.py tests/test_citation_payload.py
git commit -m "feat: AIREFS.CITE / AIREFS.BIBL payload build and parse"
```

---

### Task 13: Citation rendering from CSL (`citation_render.py`)

**Files:**
- Create: `src/pipeline/citation_render.py`
- Modify: `src/gui/main_window.py:310-343` (delete `_parse_csl_citation_layout`; import `parse_csl_layout`)
- Test: `tests/test_citation_render.py`

**Behaviour:**
- `@dataclass CitationLayout: prefix: str; suffix: str; delimiter: str; is_author_date: bool; is_superscript: bool; collapse: bool` and `render_kind` property → `"author-date"` / `"numeric-superscript"` / `"numeric-paren"` (prefix `(`) / `"numeric-bracket"` (default).
- `parse_csl_layout(style) -> CitationLayout`: today's `_parse_csl_citation_layout` logic plus `collapse = citation_el.get("collapse") == "citation-number"`, `is_superscript = style in SUPERSCRIPT_STYLES`, `is_author_date = style in AUTHOR_DATE_STYLES`. Defaults on any error: bracket layout with `collapse=False`. Author-date layouts with empty prefix/suffix get `("(", ")", "; ")` (today's fallback in `_do_insert_export`).
- `layout_for_existing_shape(layout, is_superscript_doc) -> CitationLayout`: the insert-mode override from `main_window.py:565-570` (superscript → `("", "", ",")`, else `("[", "]", ", ")`), numeric only.
- `render_numbers(numbers, layout) -> str`: numbers sorted ascending and deduplicated; joined with `layout.delimiter`; if `layout.collapse`, runs of 3+ consecutive numbers become `a-b` (reuse `format_bracket_numbers`, then replace `", "` by `layout.delimiter`); wrapped in prefix/suffix. Superscript layouts use `","` as delimiter with no spaces.
- `render_author_date(candidates, layout) -> str`: `prefix + delimiter.join(c.first_author_year for c in candidates) + suffix` deduplicated in order.
- `render_cluster(candidates, numbers, layout) -> str`: author-date → `render_author_date`, else `render_numbers`. Empty → `"[?]"`.

**Step 1: Write the failing test**

```python
# tests/test_citation_render.py
import pytest

from src.models.citation import Author, CitationCandidate
from src.models.project import CitationStyle
from src.pipeline.citation_render import (
    CitationLayout, layout_for_existing_shape, parse_csl_layout, render_cluster, render_numbers,
)


@pytest.mark.parametrize("style,kind,collapse", [
    (CitationStyle.NIH_GRANT, "numeric-superscript", True),
    (CitationStyle.NATURE, "numeric-superscript", True),
    (CitationStyle.VANCOUVER, "numeric-bracket", True),
    (CitationStyle.IEEE, "numeric-bracket", False),
    (CitationStyle.SCIENCE, "numeric-paren", True),
    (CitationStyle.APA, "author-date", False),
])
def test_layout_kind_and_collapse(style, kind, collapse):
    layout = parse_csl_layout(style)
    assert layout.render_kind == kind
    assert layout.collapse is collapse


def test_all_styles_parse():
    for style in CitationStyle:
        assert isinstance(parse_csl_layout(style), CitationLayout)


def test_render_numbers_collapses_only_when_csl_says_so():
    sup = CitationLayout("", "", ",", False, True, True)
    assert render_numbers([5, 3, 4, 3, 9], sup) == "3-5,9"
    ieee = CitationLayout("[", "]", ", ", False, False, False)
    assert render_numbers([3, 4, 5], ieee) == "[3, 4, 5]"
    assert render_numbers([1, 2], sup) == "1,2"            # two consecutive: no range


def test_render_cluster_author_date_and_unresolved():
    ad = parse_csl_layout(CitationStyle.APA)
    c1 = CitationCandidate(title="a", year=2020, authors=[Author(last_name="Smith"), Author(last_name="Doe"), Author(last_name="Roe")])
    c2 = CitationCandidate(title="b", year=2021, authors=[Author(last_name="Lee")])
    assert render_cluster([c1, c2], [], ad) == "(Smith et al., 2020; Lee, 2021)"
    assert render_cluster([], [], ad) == "[?]"


def test_existing_shape_override():
    base = parse_csl_layout(CitationStyle.SCIENCE)
    assert layout_for_existing_shape(base, True).render_kind == "numeric-superscript"
    assert layout_for_existing_shape(base, False).render_kind == "numeric-bracket"
    ad = parse_csl_layout(CitationStyle.APA)
    assert layout_for_existing_shape(ad, True) is ad
```

**Step 2: Run to verify failure**

Run: `python -m pytest tests/test_citation_render.py -q`
Expected: FAIL (`No module named 'src.pipeline.citation_render'`)

**Step 3: Implement** per the behaviour list (move the ElementTree code from `main_window.py` verbatim, add `collapse`). In `main_window.py` replace `csl_info = self._parse_csl_citation_layout(style)` blocks with `layout = parse_csl_layout(style)` and use `layout.prefix` etc.; keep behaviour identical for now (the export bodies move in Tasks 15/17/20).

**Step 4: Run tests** → `python -m pytest -q` all pass.

**Step 5: Commit**

```bash
git add src/pipeline/citation_render.py src/gui/main_window.py tests/test_citation_render.py
git commit -m "feat: CSL citation layout parsing with collapse; cluster rendering"
```

---

### Task 14: Writing fields: citation fields, bibliography field, styles, validation

**Files:**
- Modify: `src/services/docx_fields.py` (add `build_field_runs`, `make_result_run`, `rewrite_result`, `rewrite_code`)
- Modify: `src/services/docx_io.py` (add `insert_citation_field`, `ensure_bibliography_styles`, `write_bibliography_field`, `replace_bibliography_field`, `locate_bibliography_heading`, `validate_before_save`; `append_bibliography` stays for legacy)
- Test: `tests/test_field_writer.py`

**Behaviour:**
- `make_result_run(text, template_r=None, *, superscript=False, no_proof=True)`: `w:r` with `w:rPr` cloned from `template_r` (rPr only) then `w:noProof` added, `w:vertAlign superscript` set or removed per `superscript`, and one `w:t`.
- `build_field_runs(code, result_run, *, fld_lock=True) -> list`: `[begin(fldLock="1"), instr(code), separate, result_run, end]`.
- `rewrite_result(field, text, *, superscript=None)`: keep the first result run, delete the others (Word re-splits results), set its `w:t` to `text` (single `w:t`), adjust `vertAlign` when `superscript` is not None. Multi-paragraph fields (bibliography) are never rewritten with this; see `replace_bibliography_field`.
- `rewrite_code(field, code)`: set the first code run's `instrText` to `code`, delete the other code runs.
- `DocxHandler.insert_citation_field(paragraph, marker_text, code, result_text, superscript)`: `_locate_span` + emit `before-run, *field runs, after-run` (the result run is built with `make_result_run(result_text, first_r, superscript=superscript)`), removes the touched runs, `invalidate_fields()`, returns the new result run.
- `ensure_bibliography_styles()`: create paragraph styles `AIREFS Bibliography Heading` (based on Normal, bold, 14pt) and `AIREFS Bibliography` (based on Normal, 10pt) if absent (`doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)`).
- `write_bibliography_field(entries, bibl_code, heading_text="References")`: append heading paragraph (style heading) then one paragraph per entry (style bibliography). Field runs: paragraph 0 gets `begin, instr, separate` before its text run; the last paragraph gets `end` after its text run (single entry: all in one). Entries text runs get `w:sz 20` (10pt) like today.
- `replace_bibliography_field(field, entries, bibl_code)`: insert the new entry paragraphs (with field runs) before `field.paragraphs[0]`, then remove `field.paragraphs`; heading untouched; content after the field untouched. Returns the new field's first paragraph index.
- `locate_bibliography_heading(field, heading_text) -> int`: index in `doc.paragraphs` of the heading per the design rule (immediately preceding paragraph if it holds no field, is < 80 chars and not `BIB_ENTRY_PATTERN`-shaped; else nearest preceding paragraph with the heading style; else nearest preceding paragraph whose stripped text equals `heading_text`; else `-1`).
- `validate_before_save(expected_cite_fields=None) -> list[str]`: rebuild the index; problems if any `airefs` field is incomplete; any `w:t` text contains `ADDIN AIREFS`; any `airefs` payload fails to parse; `expected_cite_fields` given and the count differs.

**Step 1: Write the failing test**

```python
# tests/test_field_writer.py
import pytest
from docx import Document

from src.pipeline.citation_payload import build_bibl_code, parse_cite_code
from src.services.docx_fields import rewrite_code, rewrite_result
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder

CODE = ' ADDIN AIREFS.CITE {"citationID":"abcdefghijkl","citationItems":[],"airefs":{"v":1,"numbers":[3]}} '


def _handler(tmp_path, build):
    b = DocBuilder()
    build(b)
    return DocxHandler(str(b.save(tmp_path / "w.docx")))


def test_insert_citation_field_replaces_marker(tmp_path):
    h = _handler(tmp_path, lambda b: b.paragraph("Neurons fire (REF). Then"))
    para = h.get_paragraphs()[0]
    h.insert_citation_field(para, "(REF)", CODE, "3", superscript=True)
    assert para.text == "Neurons fire 3. Then"
    f = h.fields.fields[0]
    assert f.kind == "airefs_cite" and f.complete and f.result_text == "3"
    assert parse_cite_code(f.code).numbers == [3]
    xml = para._p.xml
    assert 'w:fldLock="1"' in xml and "<w:noProof/>" in xml and 'w:val="superscript"' in xml


def test_insert_field_after_hyperlink_and_round_trip_to_disk(tmp_path):
    def build(b):
        p = b.paragraph("See ")
        b.add_hyperlink(p, "https://x.org", "site")
        b.add_text(p, " and (REFS).")
    h = _handler(tmp_path, build)
    h.insert_citation_field(h.get_paragraphs()[0], "(REFS)", CODE, "[3]", superscript=False)
    out = tmp_path / "out.docx"
    h.save(str(out))
    h2 = DocxHandler(str(out))
    assert h2.get_paragraphs()[0].text == "See site and [3]."
    assert h2.fields.airefs_cite == 1


def test_bibliography_field_write_and_replace(tmp_path):
    h = _handler(tmp_path, lambda b: (b.paragraph("Body"), b.paragraph("Appendix")))
    code = build_bibl_code(doc_id="d", style="nih_grant", render="numeric-superscript",
                           heading_text="References", order=["u1", "u2"], uncited=[], entry_hashes=["a", "b"])
    h.write_bibliography_field(["1. One", "2. Two"], code)
    paras = h.get_paragraphs()
    assert [p.text for p in paras] == ["Body", "Appendix", "References", "1. One", "2. Two"]
    assert paras[2].style.name == "AIREFS Bibliography Heading"
    assert paras[3].style.name == "AIREFS Bibliography"
    f = [f for f in h.fields.fields if f.kind == "airefs_bibl"][0]
    assert f.result_text == "1. One\n2. Two" and len(f.paragraphs) == 2
    assert h.locate_bibliography_heading(f, "References") == 2

    h.replace_bibliography_field(f, ["1. Uno", "2. Dos", "3. Tres"], code)
    paras = h.get_paragraphs()
    assert [p.text for p in paras] == ["Body", "Appendix", "References", "1. Uno", "2. Dos", "3. Tres"]
    f2 = [f for f in h.fields.fields if f.kind == "airefs_bibl"][0]
    assert f2.complete and len(f2.paragraphs) == 3


def test_single_entry_bibliography_and_heading_rule(tmp_path):
    h = _handler(tmp_path, lambda b: b.paragraph("Body"))
    code = build_bibl_code(doc_id="d", style="x", render="numeric-bracket", heading_text="Literature Cited",
                           order=["u1"], uncited=[], entry_hashes=["a"])
    h.write_bibliography_field(["1. Only"], code, heading_text="Literature Cited")
    f = [f for f in h.fields.fields if f.kind == "airefs_bibl"][0]
    assert f.complete and f.result_text == "1. Only"
    # user renamed the heading: positional rule still finds it
    h.get_paragraphs()[1].runs[0].text = "REFERENCES CITED"
    assert h.locate_bibliography_heading(f, "Literature Cited") == 1
    # user inserted an entry-shaped paragraph directly above the field: fall back to text match
    h.get_paragraphs()[1].runs[0].text = "0. Not a heading"
    assert h.locate_bibliography_heading(f, "Literature Cited") == -1


def test_rewrite_result_collapses_split_runs_and_rewrite_code(tmp_path):
    def build(b):
        p = b.paragraph("x")
        runs = b.add_field(p, code=CODE, result="1", superscript=True)
        from tests.fixture_builders import make_run
        runs[-1].addprevious(make_run("2", superscript=True))   # Word split the result into "1","2"
    h = _handler(tmp_path, build)
    f = h.fields.fields[0]
    assert len(f.result_runs) == 2
    rewrite_result(f, "7,8", superscript=True)
    rewrite_code(f, CODE.replace("[3]", "[7,8]"))
    h.invalidate_fields()
    f = h.fields.fields[0]
    assert f.result_text == "7,8" and len(f.result_runs) == 1
    assert parse_cite_code(f.code).numbers == [7, 8]


def test_validate_before_save_catches_leaks_and_counts(tmp_path):
    h = _handler(tmp_path, lambda b: b.paragraph("Body (REF)"))
    h.insert_citation_field(h.get_paragraphs()[0], "(REF)", CODE, "1", superscript=False)
    assert h.validate_before_save(expected_cite_fields=1) == []
    assert h.validate_before_save(expected_cite_fields=2)
    h.get_paragraphs()[0].add_run(" ADDIN AIREFS.CITE leaked")
    assert any("leak" in p.lower() or "ADDIN" in p for p in h.validate_before_save())
```

**Step 2: Run to verify failure**

Run: `python -m pytest tests/test_field_writer.py -q`
Expected: FAIL (`AttributeError: 'DocxHandler' object has no attribute 'insert_citation_field'`)

**Step 3: Implement** per the behaviour list. Building runs (in `docx_fields.py`):

```python
def make_result_run(text, template_r=None, *, superscript=False, no_proof=True):
    r = OxmlElement('w:r')
    rpr = None
    if template_r is not None:
        src = template_r.find(qn('w:rPr'))
        if src is not None:
            rpr = copy.deepcopy(src)
    if rpr is None:
        rpr = OxmlElement('w:rPr')
    for va in rpr.findall(qn('w:vertAlign')):
        rpr.remove(va)
    if no_proof and rpr.find(qn('w:noProof')) is None:
        rpr.insert(0, OxmlElement('w:noProof'))
    if superscript:
        va = OxmlElement('w:vertAlign'); va.set(qn('w:val'), 'superscript'); rpr.append(va)
    r.append(rpr)
    t = OxmlElement('w:t'); t.set(qn('xml:space'), 'preserve'); t.text = text; r.append(t)
    return r


def _fldchar(kind, lock=False):
    r = OxmlElement('w:r'); fc = OxmlElement('w:fldChar'); fc.set(qn('w:fldCharType'), kind)
    if lock: fc.set(qn('w:fldLock'), '1')
    r.append(fc); return r


def _instr(code):
    r = OxmlElement('w:r'); it = OxmlElement('w:instrText'); it.set(qn('xml:space'), 'preserve')
    it.text = code; r.append(it); return r


def build_field_runs(code, result_run, *, fld_lock=True):
    return [_fldchar('begin', fld_lock), _instr(code), _fldchar('separate'), result_run, _fldchar('end')]
```

`rpr.insert(0, noProof)` — python-docx's `CT_RPr` enforces child order when using its API; raw insertion is tolerated by Word for `noProof`, but to be safe insert `noProof` after `w:rStyle`/`w:rFonts`/`w:b`… by appending it *before* `w:vertAlign` (append then re-append `vertAlign` last), which is the schema position (`noProof` precedes `vertAlign`).

`validate_before_save`:

```python
    def validate_before_save(self, expected_cite_fields=None) -> list[str]:
        from ..pipeline.citation_payload import parse_cite_code, parse_bibl_code, PayloadError
        self.invalidate_fields()
        problems = []
        idx = self.fields
        for f in idx.fields:
            if not f.kind.startswith('airefs'):
                continue
            if not f.complete:
                problems.append(f"incomplete {f.kind} field")
            try:
                (parse_cite_code if f.kind == 'airefs_cite' else parse_bibl_code)(f.code)
            except PayloadError as exc:
                problems.append(f"{f.kind} payload does not parse: {exc}")
        for t in self.doc.element.body.iter(f'{{{W_NS}}}t'):
            if t.text and 'ADDIN AIREFS' in t.text:
                problems.append("field code leaked into visible text")
                break
        if expected_cite_fields is not None and idx.airefs_cite != expected_cite_fields:
            problems.append(f"expected {expected_cite_fields} citation fields, found {idx.airefs_cite}")
        return problems
```

**Step 4: Run tests** → `python -m pytest -q` all pass.

**Step 5: Commit**

```bash
git add src/services/docx_fields.py src/services/docx_io.py tests/test_field_writer.py
git commit -m "feat(docx): write AIREFS citation and bibliography fields; validate before save"
```

---

### Task 15: Headless fresh export writing fields; real round-trip test for all styles

**Files:**
- Modify: `src/pipeline/docx_export.py` (add `export_fresh`)
- Modify: `src/pipeline/renumber_plan.py:34-45` (accept `project.existing_citations is None` → empty map)
- Modify: `src/pipeline/bib_format.py` (add `format_bib_entry_from_item(item, number, style)`; `format_bib_entry` unchanged)
- Modify: `src/pipeline/export_stats.py` (add `fields_written`, `unresolved_fields`, `legacy_adopted`, `entries_seeded_uncited`, `uncited_dropped`, `hand_edits_overwritten`, `hand_edits_preserved`, `damaged_fields`, `bibliography_regenerated`, `tables_citations`, and summary lines for the non-zero ones)
- Modify: `src/models/project.py` (`ProjectSettings.embed_citation_fields: bool = True`, `ProjectState.doc_id: str = ""`, `ProjectState.record_order: list[str]`, `ProjectState.uncited: list[str]`)
- Modify: `src/gui/main_window.py:354-486` (`_do_fresh_export` → `return export_fresh(self._project, output_path)`)
- Test: `tests/test_round_trip.py` (rewrite `export_like_fresh` users to call `export_fresh`)

**Behaviour of `export_fresh(project, output_path) -> ExportStats`:**
1. `handler = DocxHandler(project.input_docx_path)`; `layout = parse_csl_layout(style)`.
2. `plan = build_renumber_plan(handler, project)` — with `existing_citations` None the plan uses `ExistingCitationMap()` (heading `-1`), so numbering is first-appearance over all markers; `ensure_record_uuid` on every resolved candidate before numbering.
3. For each marker (document order): resolved candidates → numbers via `plan.renumber_result.number_for_candidate`; `text = render_cluster(cands, numbers, layout)`; `code = build_cite_code(cands, numbers=numbers, render=layout.render_kind, style=style.value, plain=text)`; `handler.insert_citation_field(para, f"({marker_type})", code, text, superscript=layout.is_superscript)`. Unresolved: `build_cite_code([], numbers=[], …, plain="[?]", unresolved=True)` with result `"[?]"`; stats as today.
4. Bibliography: assignments sorted by number; entries via `format_bib_entry(candidate, n, style)` (author-date styles: `build_author_date_bibliography`-style alphabetical order with `number` ignored — reuse `bib_format.format_bib_entry` which already omits the number for author-date; sort alphabetically like `author_date_convert.build_author_date_bibliography`); `order` = record uuids in that rendered order; `doc_id = project.doc_id or uuid4` (store back on the project); `write_bibliography_field(entries, build_bibl_code(...))`. Skip when no entries.
5. `problems = handler.validate_before_save(expected_cite_fields=len(markers))` → raise `ExportBlocked(problems)` if any; `handler.save(output_path)`; set `project.output_docx_path`, `project.record_order`, stats (`fields_written`, `unresolved_fields`, `new_refs_added`, `bibliography_size`).
6. `settings.embed_citation_fields == False` → today's plain-text behaviour (call the current code path, kept as `export_fresh_plain`; this is the escape hatch if Word misbehaves at the acceptance gate).

`main_window._do_fresh_export` becomes a one-liner; delete the moved code.

**Step 1: Write the failing test** (replace `tests/test_round_trip.py`)

```python
"""Round-trip tests through the REAL export code.

fresh export -> reopen must be exact (tracked tier, Task 16) and the legacy parser
must still recover identifiers from the visible bibliography text.
"""
import pytest
from docx import Document

from src.models.citation import Author, CitationCandidate
from src.models.evidence import EvidenceRecord, ReviewDecision
from src.models.project import CitationStyle, ProjectState
from src.models.sentence import MarkerType, SentenceRecord
from src.pipeline.citation_payload import parse_bibl_code, parse_cite_code
from src.pipeline.docx_export import export_fresh
from src.pipeline.existing_citation_parser import ExistingCitationParser
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder


def make_citation(i):
    return CitationCandidate(
        pmid=f"3000000{i}", doi=f"10.1234/test.{i}", title=f"Important Study Number {i}",
        authors=[Author(last_name=f"Author{i}", initials="A")], year=2018 + i,
        journal=f"Journal of Tests {i}", journal_abbrev=f"J Test {i}", volume=str(10 + i),
        pages=f"{i}00-{i}10", source="pubmed")


def make_project(tmp_path, body, style=CitationStyle.NIH_GRANT):
    """body: list of (text_with_markers, [[citations per marker], ...])."""
    b = DocBuilder()
    project = ProjectState(settings={"citation_style": style})
    sid = 0
    for para_idx, (text, per_marker) in enumerate(body):
        b.paragraph(text)
        if per_marker:
            sid += 1
            types = [MarkerType.REF if len(c) == 1 else MarkerType.REFS for c in per_marker]
            sent = SentenceRecord(id=f"S{sid:03d}", paragraph_index=para_idx, raw_text=text,
                                  clean_text=text, marker_type=types[0], marker_count=len(types),
                                  marker_types=types)
            project.sentences.append(sent)
            selected = [c for group in per_marker for c in group] if not sent.searched_per_marker \
                else [group[0] for group in per_marker]
            project.evidence_map[sent.id] = EvidenceRecord(
                sentence_id=sent.id, selected=selected, review_decision=ReviewDecision.ACCEPTED)
    path = b.save(tmp_path / "in.docx")
    project.input_docx_path = str(path)
    return project


C1, C2, C3 = make_citation(1), make_citation(2), make_citation(3)
BODY = [("First finding (REF).", [[C1]]), ("Second finding (REFS).", [[C2, C3]]), ("Recap (REF).", [[C2]])]


@pytest.mark.parametrize("style", list(CitationStyle))
def test_fresh_export_writes_fields_for_every_style(tmp_path, style):
    project = make_project(tmp_path, BODY, style)
    out = tmp_path / "out.docx"
    stats = export_fresh(project, str(out))
    assert stats.fields_written == 3 and stats.unresolved_fields == 0 and stats.bibliography_size == 3
    h = DocxHandler(str(out))
    cites = [f for f in h.fields.fields if f.kind == "airefs_cite"]
    bibl = [f for f in h.fields.fields if f.kind == "airefs_bibl"]
    assert len(cites) == 3 and len(bibl) == 1 and all(f.complete for f in cites + bibl)
    payloads = [parse_cite_code(f.code) for f in cites]
    assert [[it.identity["pmid"] for it in p.items] for p in payloads] == [["30000001"], ["30000002", "30000003"], ["30000002"]]
    b = parse_bibl_code(bibl[0].code)
    assert len(b.order) == 3 and b.style == style.value
    if style in {CitationStyle.APA, CitationStyle.CSE_AUTHOR_DATE, CitationStyle.ELSEVIER_HARVARD,
                 CitationStyle.CHICAGO_AUTHOR_DATE, CitationStyle.ELIFE}:
        assert "Author1" in cites[0].result_text and payloads[0].render == "author-date"
    else:
        assert payloads[1].numbers == [2, 3] and cites[2].result_text.strip("[]()") == "2"
    # visible text never contains a field code
    assert "ADDIN" not in "\n".join(p.text for p in h.get_paragraphs())


def test_fresh_export_reparses_with_legacy_parser(tmp_path):
    project = make_project(tmp_path, BODY)
    out = tmp_path / "out.docx"
    export_fresh(project, str(out))
    h = DocxHandler(str(out))
    # Force the legacy scan on the exported document (fields are transparent to it)
    result = ExistingCitationParser(h)
    m = result._legacy_map()      # helper exposed for tests: runs steps 1-4 on the handler
    assert sorted(m.bib_entries) == [1, 2, 3]
    assert m.bib_entries[1].pmid == "30000001" and m.bib_entries[3].doi == "10.1234/test.3"


def test_unresolved_marker_becomes_unresolved_field(tmp_path):
    project = make_project(tmp_path, [("Claim (REF).", [[C1]]), ("Nothing (REF).", [[]])])
    project.evidence_map["S002"].review_decision = ReviewDecision.REJECTED
    out = tmp_path / "out.docx"
    stats = export_fresh(project, str(out))
    assert stats.unresolved_fields == 1 and stats.fields_written == 2
    h = DocxHandler(str(out))
    unresolved = [f for f in h.fields.fields if f.kind == "airefs_cite" and parse_cite_code(f.code).unresolved]
    assert len(unresolved) == 1 and unresolved[0].result_text == "[?]"


def test_plain_text_export_when_fields_disabled(tmp_path):
    project = make_project(tmp_path, BODY)
    project.settings.embed_citation_fields = False
    out = tmp_path / "out.docx"
    export_fresh(project, str(out))
    h = DocxHandler(str(out))
    assert h.fields.airefs_cite == 0
    assert "References" in [p.text for p in h.get_paragraphs()]
```

Also keep `test_format_bib_entry_always_includes_identifiers` from the old file.

**Step 2: Run to verify failure**

Run: `python -m pytest tests/test_round_trip.py -q`
Expected: FAIL (`ImportError: cannot import name 'export_fresh'`)

**Step 3: Implement** `export_fresh` per the behaviour list, `renumber_plan.build_renumber_plan` tolerating `None`, `ExistingCitationParser._legacy_map()` (public-for-tests wrapper around `_analyze_legacy` on a fresh `ExistingCitationMap`), and the stats/model fields. Then reduce `MainWindow._do_fresh_export` to the delegate call (wrap `ExportBlocked` in `_export_document` as a critical message box).

**Step 4: Run tests** → `python -m pytest -q` all pass (the whole suite, including GUI-free modules).

**Step 5: Commit**

```bash
git add src/pipeline/docx_export.py src/pipeline/renumber_plan.py src/pipeline/bib_format.py src/pipeline/export_stats.py src/models/project.py src/gui/main_window.py src/pipeline/existing_citation_parser.py tests/test_round_trip.py
git commit -m "feat(export): headless fresh export writing citation and bibliography fields"
```

---

### Task 16: Tracked reader — exact, offline reopen

**Files:**
- Create: `src/pipeline/field_citation_reader.py`
- Modify: `src/models/existing_refs.py` (`InTextCitation`: `cid`, `record_uuid`, `cluster_index`, `source`, `user_edited`, `unresolved`; `ExistingBibEntry`: `record_uuid`, `item`, `uris`, `is_uncited`, `entry_hash`; `ExistingCitationMap.body_end_para_idx` property)
- Modify: `src/pipeline/existing_citation_parser.py` (`_analyze_tracked`, constructor `keep_uncited: bool = False`)
- Modify: `src/pipeline/orchestrator.py:147-198` (no-clobber; enrichment skip; `stop_at = existing.body_end_para_idx`)
- Modify: `src/pipeline/renumbering.py:36-47,65-68` (`uuid:` alias first; `key_for_existing` prefers `record_uuid`)
- Test: `tests/test_tracked_reader.py`

**Behaviour of `build_tracked_map(handler, *, keep_uncited=False) -> ExistingCitationMap`:**
1. `idx = handler.fields`; body paragraph index map `{id(p._p): i for i, p in enumerate(handler.get_paragraphs())}`.
2. Walk `idx.fields` in order; keep `kind == 'airefs_cite'`, `depth == 0`, `not deleted`. Parse with `parse_cite_code`; `NewerSchemaError` → report tier `NEWER_VERSION` and return an empty map; `PayloadError` → `damaged` problem, skip the field.
3. Records: first-appearance order over body citations → `record_uuid → number`. Fields in tables (`f.in_table`) are recorded in `tracking.fields_in_tables` and as `ReconcileIssue(kind="table")` but do not take part in numbering.
4. For each body field: `para_idx = index of f.paragraphs[0]`; `char_offset` = offset of `f.result_runs[0]` inside `paragraph.text` computed over `iter_text_runs`; one `InTextCitation` per item with `number`, `is_superscript = payload.render == 'numeric-superscript'`, `cid`, `record_uuid`, `cluster_index`, `source='field'`, `unresolved`, `user_edited = (f.result_text != payload.plain)`. Unresolved fields (no items) get one entry with `number=0`, `unresolved=True`.
5. Duplicate `citationID` → later occurrences get `mint_citation_id()` in memory and a `duplicate` issue; unknown cids are simply accepted (no registry yet) — no issue.
6. `bib_entries[number] = ExistingBibEntry(original_number=number, record_uuid, item, uris, matched_candidate=from_csl_item(item) with record_uuid set, pmid/doi/title/year/journal copied from the candidate, entry_hash from BIBL when available)`.
7. BIBL field: first `kind == 'airefs_bibl'` field; `parse_bibl_code`; `references_heading_para_idx = handler.locate_bibliography_heading(f, payload.heading_text)`; `bibliography_span = (index of f.paragraphs[0], index of f.paragraphs[-1])`; `heading_para_idx_found = heading >= 0`; per-entry hashes compared with the current entry paragraph texts → `entry_edited` issues; uuids in `payload.order`/`payload.uncited` with no field: if `keep_uncited` or in `payload.uncited` → append as `is_uncited=True` entries (their `item` comes from the BIBL entry text as `raw_entry`, which is enough to re-render verbatim); else `uncited` issue. Missing BIBL → problem "bibliography field missing; it will be regenerated", `references_heading_para_idx = -1`, `bibliography_span = (-1, -1)`.
8. `detected_style_is_superscript` / `detected_style_is_author_date` from the first payload's `render`; `tracking = TrackingReport(tier=TRACKED, doc_id, schema_version, field_count, bibl_field_count, record_count, problems, reconcile, fields_in_tables)`; `pending_tracked_changes = idx.pending_tracked_changes`.
9. `ExistingCitationMap.body_end_para_idx`: `references_heading_para_idx` if `>= 0`, else `bibliography_span[0]` if `>= 0`, else `-1`. `DocumentParser` and `orchestrator` use it as `stop_at`.

`CitationKeyIndex._aliases(pmid, doi, title, record_uuid="")` registers `uuid:<u>` first; `key_for_candidate` passes `cand.record_uuid`; `key_for_existing` passes `entry.record_uuid`.

Orchestrator: skip re-running `analyze()` when `project.existing_citations.tracking.tier == TRACKED` (the map was built at load; re-analyse only if `input_docx_hash` changed — main window already handles that); enrichment targets exclude entries with `record_uuid` (they already carry identity).

**Step 1: Write the failing test**

```python
# tests/test_tracked_reader.py
import pytest

from src.models.embedded import DocumentTier
from src.pipeline.docx_export import export_fresh
from src.pipeline.existing_citation_parser import ExistingCitationParser
from src.pipeline.renumbering import NewMarkerInfo, compute_renumbering
from src.services.docx_io import DocxHandler
from tests.test_round_trip import BODY, C1, C2, C3, make_citation, make_project


@pytest.fixture
def exported(tmp_path):
    project = make_project(tmp_path, BODY)
    out = tmp_path / "out.docx"
    export_fresh(project, str(out))
    return project, out


def test_reopen_is_tracked_and_exact(exported, monkeypatch):
    import src.services.pubmed_client as pm
    monkeypatch.setattr(pm.PubMedClient, "search", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(pm.PubMedClient, "fetch_article", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    project, out = exported
    m = ExistingCitationParser(DocxHandler(str(out))).analyze()
    assert m.tracking.tier == DocumentTier.TRACKED
    assert m.tracking.field_count == 3 and m.tracking.record_count == 3 and m.tracking.problems == []
    assert sorted(m.bib_entries) == [1, 2, 3]
    assert [m.bib_entries[n].pmid for n in (1, 2, 3)] == ["30000001", "30000002", "30000003"]
    assert m.bib_entries[2].matched_candidate.title == C2.title
    assert m.bib_entries[2].record_uuid == C2.record_uuid
    cites = {p: [(c.number, c.cid != "", c.source) for c in cs] for p, cs in m.in_text_citations.items()}
    assert cites == {0: [(1, True, "field")], 1: [(2, True, "field"), (3, True, "field")], 2: [(2, True, "field")]}
    assert m.references_heading_para_idx == 3 and m.bibliography_span == (4, 6)
    assert m.body_end_para_idx == 3
    assert m.detected_style_is_superscript is True and m.has_existing_citations


def test_tracked_map_numbers_agree_with_legacy_regex_map(exported):
    _, out = exported
    h = DocxHandler(str(out))
    tracked = ExistingCitationParser(h).analyze()
    legacy = ExistingCitationParser(h)._legacy_map()
    new = [NewMarkerInfo(para_index=0, char_offset=0, citations=[make_citation(9)])]
    a = compute_renumbering(tracked, new)
    b = compute_renumbering(legacy, new)
    assert a.renumber_map == b.renumber_map
    assert {n: x.original_number for n, x in a.assignments.items()} == {n: x.original_number for n, x in b.assignments.items()}


def test_duplicate_cid_is_reminted_and_hand_edit_detected(exported):
    from docx import Document
    from src.services.docx_fields import rewrite_result
    _, out = exported
    h = DocxHandler(str(out))
    f0, f1 = [f for f in h.fields.fields if f.kind == "airefs_cite"][:2]
    import copy
    for r in f0.all_runs:                      # paste a copy of field 0 at the end of paragraph 2
        h.get_paragraphs()[2]._p.append(copy.deepcopy(r))
    rewrite_result(f1, "99", superscript=True)  # hand edit
    h.save(str(out))
    m = ExistingCitationParser(DocxHandler(str(out))).analyze()
    kinds = sorted(i.kind for i in m.tracking.reconcile)
    assert kinds == ["duplicate", "hand_edited"]
    cids = [c.cid for cs in m.in_text_citations.values() for c in cs]
    assert len(cids) == len(set(cids)) == 5


def test_missing_bibliography_field_is_reported_not_fatal(exported):
    _, out = exported
    h = DocxHandler(str(out))
    bibl = [f for f in h.fields.fields if f.kind == "airefs_bibl"][0]
    h.remove_references_section(3, 6)
    h.save(str(out))
    m = ExistingCitationParser(DocxHandler(str(out))).analyze()
    assert m.tracking.tier == DocumentTier.TRACKED
    assert m.references_heading_para_idx == -1 and m.bibliography_span == (-1, -1)
    assert sorted(m.bib_entries) == [1, 2, 3]
    assert any("regenerated" in p for p in m.tracking.problems)


def test_newer_schema_is_read_only(exported):
    from src.services.docx_fields import rewrite_code
    _, out = exported
    h = DocxHandler(str(out))
    f = [f for f in h.fields.fields if f.kind == "airefs_cite"][0]
    rewrite_code(f, f.code.replace('"v":1', '"v":99'))
    h.save(str(out))
    m = ExistingCitationParser(DocxHandler(str(out))).analyze()
    assert m.tracking.tier == DocumentTier.NEWER_VERSION and not m.has_existing_citations
```

**Step 2: Run to verify failure**

Run: `python -m pytest tests/test_tracked_reader.py -q`
Expected: FAIL (`No module named 'src.pipeline.field_citation_reader'` / tier LEGACY)

**Step 3: Implement** per the behaviour list. `_analyze_tracked(result, idx)` in the parser becomes `return build_tracked_map(self.handler, keep_uncited=self.keep_uncited)`.

**Step 4: Run tests** → `python -m pytest -q` all pass. Also run the app on an exported document and confirm the run log shows no "Matching entry" lines.

**Step 5: Commit**

```bash
git add src/pipeline/field_citation_reader.py src/models/existing_refs.py src/pipeline/existing_citation_parser.py src/pipeline/orchestrator.py src/pipeline/renumbering.py tests/test_tracked_reader.py
git commit -m "feat: tracked reader — exact offline reopen from AIREFS fields"
```

---

### Task 17: Tracked export — renumber through fields, rebuild the bibliography in place

**Files:**
- Create: `src/pipeline/tracked_renumber.py`
- Modify: `src/pipeline/docx_export.py` (add `export_tracked`; route `export(project, output_path, decisions)` by tier)
- Modify: `src/gui/main_window.py:345-352,490-657` (`_do_export` routes tracked documents to `export_tracked`; legacy documents keep `_do_insert_export` until Task 20)
- Modify: `src/gui/review_tab.py:616` (export enabled when `total > 0` **or** the document is tracked — a tracked document with zero new markers can still be renumbered/regenerated)
- Test: `tests/test_tracked_round_trip.py`

**Behaviour of `export_tracked(project, output_path, *, decisions=None) -> ExportStats`:**
1. `handler = DocxHandler(input)`; `existing = project.existing_citations` (tracked map, re-read from `handler` when its hash differs); `layout = parse_csl_layout(settings.citation_style)`; if the style is numeric and the document's existing render is numeric, use `layout_for_existing_shape(layout, existing.detected_style_is_superscript)` unless the user switched style family (author-date ↔ numeric), in which case re-render every field.
2. `plan = build_renumber_plan(handler, project)` (existing events come from field order; `seed_entries=settings.keep_uncited_entries`; entries flagged `is_uncited` in the map are seeded regardless).
3. Table fields: for each `airefs_cite` field with `in_table`, compute its items' new numbers; if any differs from `payload.numbers` → `ExportBlocked(["Citations inside tables/text boxes would change number; …"])`.
4. Existing body fields (walk `handler.fields.fields` again, in order, matched to the map by `cid`): items → candidates (`bib_entries[n].matched_candidate` via `record_uuid`); `numbers = [renumber_result.number_for_candidate(c)]`; `text = render_cluster(cands, numbers, layout)`; `code = build_cite_code(cands, numbers=…, render=…, style=…, plain=text, citation_id=cid)`. If `text != f.result_text`: numeric → `rewrite_result` (count `hand_edits_overwritten` when the field was `user_edited`); author-date and `user_edited` → keep the text, `hand_edits_preserved += 1`, and put the current text as `plain`. If `code != f.code` → `rewrite_code`. `existing_refs_renumbered` counts fields whose numbers changed.
5. Adjacent fields with no text between them (`f.end` immediately followed by the next field's `begin`): merge into one cluster (items concatenated, first cid kept, second field's runs removed) before step 4; `duplicates_merged += 1` per merge.
6. New markers: as in `export_fresh` (numbers from the shared result; unresolved fields).
7. Bibliography: entries in assignment order; for each record: if the map's `entry_hash` for it mismatches the current entry text (user edited the entry) → keep the current text verbatim, re-prefixed with the new number (numeric) ; else `format_bib_entry(candidate, n, style)` (author-date: alphabetical, no numbers). `order`, `entry_hashes`, `uncited` (uuids kept via `keep_uncited`), `doc_id` (from the map's BIBL or the project, else minted). If the BIBL field exists → `replace_bibliography_field`; else → `write_bibliography_field` at the end (or after a heading found by style/text) and `bibliography_regenerated = True`. Records with no field and not kept → `uncited_dropped`.
8. `validate_before_save(expected_cite_fields=len(existing body fields) - merged + len(markers))` → `ExportBlocked` on problems; `save`; update `project.output_docx_path`, `doc_id`, `record_order`, `uncited`; stats.

`decisions` (dataclass `ExportDecisions(convert_to_author_date: bool | None = None, allow_tracked_changes: bool = False)`) carries the answers the GUI collects (today's two `QMessageBox.question` prompts) so the pipeline stays headless.

`main_window._do_export`: `existing.tracking.tier == TRACKED` → `export_tracked`; the prompts move to a small `_collect_decisions()` that returns `ExportDecisions` or `None` (cancel).

**Step 1: Write the failing test**

```python
# tests/test_tracked_round_trip.py
"""Multi-session round trips through the real export code (fresh -> edit -> tracked export)."""
import copy

import pytest

from src.models.embedded import DocumentTier
from src.models.evidence import EvidenceRecord, ReviewDecision
from src.models.project import CitationStyle, ProjectState
from src.models.sentence import MarkerType, SentenceRecord
from src.pipeline.citation_payload import parse_bibl_code, parse_cite_code
from src.pipeline.docx_export import ExportBlocked, export_fresh, export_tracked
from src.pipeline.existing_citation_parser import ExistingCitationParser
from src.services.docx_fields import rewrite_result
from src.services.docx_io import DocxHandler
from tests.fixture_builders import make_run
from tests.test_round_trip import BODY, C1, C2, C3, make_citation, make_project


def _reopen(project, path, style=None):
    """Second session: new ProjectState pointing at the exported file, analysed."""
    p2 = ProjectState(settings=copy.deepcopy(project.settings))
    if style is not None:
        p2.settings.citation_style = style
    p2.input_docx_path = str(path)
    p2.existing_citations = ExistingCitationParser(DocxHandler(str(path))).analyze()
    p2.is_insert_mode = True
    return p2


def _add_marker(path, para_text, sentence_id, citations, para_idx):
    """Append a paragraph with a (REF) marker to the docx and return a SentenceRecord + evidence."""
    h = DocxHandler(str(path))
    body = h.doc.element.body
    p = h.doc.add_paragraph(para_text)
    body.insert(para_idx, p._p)     # place before the References heading
    h.save(str(path))
    t = MarkerType.REF if len(citations) == 1 else MarkerType.REFS
    sent = SentenceRecord(id=sentence_id, paragraph_index=para_idx, raw_text=para_text, clean_text=para_text,
                          marker_type=t, marker_count=1, marker_types=[t])
    ev = EvidenceRecord(sentence_id=sentence_id, selected=citations, review_decision=ReviewDecision.ACCEPTED)
    return sent, ev


def _numbers(path):
    h = DocxHandler(str(path))
    return [parse_cite_code(f.code).numbers for f in h.fields.fields if f.kind == "airefs_cite"]


def _bib_texts(path):
    h = DocxHandler(str(path))
    f = [f for f in h.fields.fields if f.kind == "airefs_bibl"][0]
    return f.result_text.split("\n")


@pytest.fixture
def session1(tmp_path):
    project = make_project(tmp_path, BODY)
    out = tmp_path / "s1.docx"
    export_fresh(project, str(out))
    return project, out


def test_add_marker_at_top_renumbers_everything(session1, tmp_path):
    project, out = session1
    c9 = make_citation(9)
    sent, ev = _add_marker(out, "New first claim (REF).", "S001", [c9], 0)
    p2 = _reopen(project, out)
    p2.sentences = [sent]; p2.evidence_map = {sent.id: ev}
    out2 = tmp_path / "s2.docx"
    stats = export_tracked(p2, str(out2))
    assert stats.fields_written == 1 and stats.existing_refs_renumbered == 3
    assert _numbers(out2) == [[1], [2], [3, 4], [3]]
    assert _bib_texts(out2)[0].startswith("1. Author9") and len(_bib_texts(out2)) == 4
    m = ExistingCitationParser(DocxHandler(str(out2))).analyze()
    assert m.tracking.tier == DocumentTier.TRACKED and m.tracking.problems == []
    assert [m.bib_entries[n].pmid for n in (1, 2, 3, 4)] == ["30000009", "30000001", "30000002", "30000003"]


def test_marker_after_references_heading_gets_a_number(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out)); n = len(h.get_paragraphs())
    sent, ev = _add_marker(out, "Appendix claim (REF).", "S001", [make_citation(7)], n)
    p2 = _reopen(project, out); p2.sentences = [sent]; p2.evidence_map = {sent.id: ev}
    out2 = tmp_path / "s2.docx"
    export_tracked(p2, str(out2))
    assert _numbers(out2)[-1] == [4]


def test_delete_sentence_drops_record_and_reports(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out))
    body = h.doc.element.body; body.remove(h.get_paragraphs()[0]._p); h.save(str(out))   # C1 gone
    p2 = _reopen(project, out)
    out2 = tmp_path / "s2.docx"
    stats = export_tracked(p2, str(out2))
    assert stats.uncited_dropped == 1 and len(_bib_texts(out2)) == 2
    assert _numbers(out2) == [[1, 2], [1]]


def test_keep_uncited_setting_keeps_entry(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out)); h.doc.element.body.remove(h.get_paragraphs()[0]._p); h.save(str(out))
    p2 = _reopen(project, out); p2.settings.keep_uncited_entries = True
    out2 = tmp_path / "s2.docx"
    stats = export_tracked(p2, str(out2))
    assert stats.uncited_dropped == 0 and len(_bib_texts(out2)) == 3
    assert _bib_texts(out2)[2].startswith("3. Author1")


def test_deleted_references_section_is_regenerated_identically(session1, tmp_path):
    project, out = session1
    before = _bib_texts(out)
    h = DocxHandler(str(out)); h.remove_references_section(3, 6); h.save(str(out))
    p2 = _reopen(project, out)
    out2 = tmp_path / "s2.docx"
    stats = export_tracked(p2, str(out2))
    assert stats.bibliography_regenerated is True
    assert _bib_texts(out2) == before
    paras = [p.text for p in DocxHandler(str(out2)).get_paragraphs()]
    assert "References" in paras


def test_paragraph_reorder_renumbers(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out)); body = h.doc.element.body
    p0 = h.get_paragraphs()[0]._p; body.remove(p0); body.insert(2, p0); h.save(str(out))
    p2 = _reopen(project, out)
    out2 = tmp_path / "s2.docx"
    export_tracked(p2, str(out2))
    assert _numbers(out2) == [[1, 2], [1], [3]]


def test_pasted_duplicate_gets_new_cid_and_same_number(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out))
    f0 = [f for f in h.fields.fields if f.kind == "airefs_cite"][0]
    for r in f0.all_runs:
        h.get_paragraphs()[2]._p.append(copy.deepcopy(r))
    h.save(str(out))
    p2 = _reopen(project, out)
    out2 = tmp_path / "s2.docx"
    export_tracked(p2, str(out2))
    h2 = DocxHandler(str(out2))
    payloads = [parse_cite_code(f.code) for f in h2.fields.fields if f.kind == "airefs_cite"]
    assert len({p.citation_id for p in payloads}) == 4
    assert [p.numbers for p in payloads] == [[1], [2, 3], [2], [1]]


def test_hand_edited_number_is_overwritten_and_author_date_preserved(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out))
    f = [f for f in h.fields.fields if f.kind == "airefs_cite"][0]
    rewrite_result(f, "42", superscript=True); h.save(str(out))
    p2 = _reopen(project, out)
    out2 = tmp_path / "s2.docx"
    stats = export_tracked(p2, str(out2))
    assert stats.hand_edits_overwritten == 1
    assert DocxHandler(str(out2)).fields.fields[0].result_text == "1"

    # author-date document: hand edits survive
    apa = make_project(tmp_path, BODY, CitationStyle.APA)
    a1 = tmp_path / "apa1.docx"; export_fresh(apa, str(a1))
    h = DocxHandler(str(a1)); f = [f for f in h.fields.fields if f.kind == "airefs_cite"][0]
    rewrite_result(f, "(Author1, 2019, p. 5)", superscript=False); h.save(str(a1))
    p3 = _reopen(apa, a1)
    a2 = tmp_path / "apa2.docx"
    stats = export_tracked(p3, str(a2))
    assert stats.hand_edits_preserved == 1
    assert DocxHandler(str(a2)).fields.fields[0].result_text == "(Author1, 2019, p. 5)"


def test_edited_bibliography_entry_is_kept_verbatim(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out))
    entry_p = h.get_paragraphs()[5]                      # entry 2
    entry_p.runs[-1].text = entry_p.runs[-1].text + " [corrected volume]"
    h.save(str(out))
    p2 = _reopen(project, out)
    out2 = tmp_path / "s2.docx"
    export_tracked(p2, str(out2))
    assert _bib_texts(out2)[1].endswith("[corrected volume]")


def test_content_after_references_survives(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out)); h.doc.add_paragraph("Appendix A"); h.doc.add_paragraph("Extra"); h.save(str(out))
    p2 = _reopen(project, out)
    out2 = tmp_path / "s2.docx"
    export_tracked(p2, str(out2))
    assert [p.text for p in DocxHandler(str(out2)).get_paragraphs()][-2:] == ["Appendix A", "Extra"]


def test_style_switch_numbered_to_apa_and_back_is_idempotent(session1, tmp_path):
    project, out = session1
    p2 = _reopen(project, out, CitationStyle.APA)
    apa = tmp_path / "apa.docx"; export_tracked(p2, str(apa))
    h = DocxHandler(str(apa))
    assert h.fields.fields[0].result_text.startswith("(Author1")
    assert parse_cite_code(h.fields.fields[0].code).render == "author-date"
    p3 = _reopen(p2, apa, CitationStyle.NIH_GRANT)
    back = tmp_path / "back.docx"; export_tracked(p3, str(back))
    assert _numbers(back) == _numbers(out) and _bib_texts(back) == _bib_texts(out)


def test_three_sessions_keep_uuids_stable(session1, tmp_path):
    project, out = session1
    uuids = [parse_cite_code(f.code).items[0].record_uuid for f in DocxHandler(str(out)).fields.fields if f.kind == "airefs_cite"]
    path = out
    for i in range(3):
        p = _reopen(project, path)
        nxt = tmp_path / f"cycle{i}.docx"
        export_tracked(p, str(nxt))
        path = nxt
    after = [parse_cite_code(f.code).items[0].record_uuid for f in DocxHandler(str(path)).fields.fields if f.kind == "airefs_cite"]
    assert after == uuids


def test_tracked_changes_block_unless_overridden(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out))
    f = [f for f in h.fields.fields if f.kind == "airefs_cite"][0]
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    d = OxmlElement("w:del"); d.set(qn("w:id"), "9"); d.set(qn("w:author"), "t"); d.set(qn("w:date"), "2026-01-01T00:00:00Z")
    f.begin.addprevious(d)
    for r in f.all_runs:
        d.append(r)
    h.save(str(out))
    p2 = _reopen(project, out)
    with pytest.raises(ExportBlocked):
        export_tracked(p2, str(tmp_path / "blocked.docx"))
    p2.settings.allow_export_with_tracked_changes = True
    export_tracked(p2, str(tmp_path / "ok.docx"))


def test_table_field_that_would_change_number_blocks(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out))
    f0 = [f for f in h.fields.fields if f.kind == "airefs_cite"][0]
    cell = h.doc.add_table(rows=1, cols=1).cell(0, 0).paragraphs[0]
    for r in f0.all_runs:
        cell._p.append(copy.deepcopy(r))
    h.save(str(out))
    sent, ev = _add_marker(out, "Top (REF).", "S001", [make_citation(9)], 0)
    p2 = _reopen(project, out); p2.sentences = [sent]; p2.evidence_map = {sent.id: ev}
    with pytest.raises(ExportBlocked):
        export_tracked(p2, str(tmp_path / "t.docx"))
```

**Step 2: Run to verify failure**

Run: `python -m pytest tests/test_tracked_round_trip.py -q`
Expected: FAIL (`ImportError: cannot import name 'export_tracked'`)

**Step 3: Implement** `tracked_renumber.py` (`merge_adjacent_fields`, `rewrite_existing_fields`, `blocked_table_fields`, `rebuild_bibliography`) and `export_tracked` per the behaviour list; wire `main_window._do_export` and `review_tab._update_status`.

**Step 4: Run tests** → `python -m pytest -q` all pass.

**Step 5: Commit**

```bash
git add src/pipeline/tracked_renumber.py src/pipeline/docx_export.py src/gui/main_window.py src/gui/review_tab.py tests/test_tracked_round_trip.py
git commit -m "feat(export): tracked export — renumber through fields, rebuild bibliography in place"
```

---

### Task 18: Non-destructive library merge for embedded records

**Files:**
- Modify: `src/services/ref_library.py:116-202` (`upsert_candidate(citation, source="literature", merge=False)`)
- Modify: `src/gui/review_tab.py:555-580, 815-835` (pass `merge=(citation.source == "embedded")`)
- Test: `tests/test_ref_library_merge.py`

**Behaviour:** in `merge` mode an existing row is updated column-wise: text columns keep the existing value when non-empty; `authors_json` keeps the longer list; `abstract`, `mesh_terms_json`, `publication_types_json` keep the existing value when non-empty; `is_retracted` becomes `existing OR new`; `is_review` keeps existing unless new is true; `source` keeps the existing value unless it is `embedded`; `updated_at` refreshed. Insert behaviour unchanged.

**Step 1: Write the failing test**

```python
# tests/test_ref_library_merge.py
from src.models.citation import Author, CitationCandidate
from src.services.ref_library import ReferenceLibrary


def _lib(tmp_path):
    return ReferenceLibrary(str(tmp_path / "lib.db"))


def test_merge_never_degrades_a_rich_row(tmp_path):
    lib = _lib(tmp_path)
    rich = CitationCandidate(pmid="1", title="T", abstract="ABS", mesh_terms=["M1", "M2"],
                             publication_types=["Journal Article"], journal="J",
                             authors=[Author(last_name=f"A{i}") for i in range(12)], source="pubmed")
    assert lib.upsert_candidate(rich, source="accepted_pubmed") == (True, False)
    thin = CitationCandidate(pmid="1", title="T", authors=[Author(last_name="A0")], source="embedded",
                             is_retracted=True, journal_abbrev="J Abbr")
    assert lib.upsert_candidate(thin, source="embedded", merge=True) == (False, True)
    row = lib.search("T", max_results=1)[0]
    assert row.abstract == "ABS" and row.mesh_terms == ["M1", "M2"] and row.publication_types == ["Journal Article"]
    assert len(row.authors) == 12
    assert row.is_retracted is True                     # retraction propagates
    assert row.journal_abbrev == "J Abbr"               # empty column filled
    assert row.source != "embedded"


def test_non_merge_update_still_overwrites(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert_candidate(CitationCandidate(pmid="2", title="T", abstract="old"), source="x")
    lib.upsert_candidate(CitationCandidate(pmid="2", title="T", abstract="new"), source="x")
    assert lib.search("T", max_results=1)[0].abstract == "new"
```

(Adjust `lib.search(...)` to whatever read API exists — `search(query, max_results)` at `ref_library.py:220` returns `CitationCandidate`s; if `source` is not carried on the candidate, assert via a direct `SELECT source FROM references_library`.)

**Step 2: Run to verify failure**

Run: `python -m pytest tests/test_ref_library_merge.py -q`
Expected: FAIL (`TypeError: upsert_candidate() got an unexpected keyword argument 'merge'`)

**Step 3: Implement**: in the update branch, when `merge`, `SELECT` the existing row into a `CitationCandidate` via `_row_to_candidate`, build `merged` per the rules, and run the existing UPDATE with `merged`'s values and the resolved `source`.

**Step 4: Run tests** → `python -m pytest -q` all pass.

**Step 5: Commit**

```bash
git add src/services/ref_library.py src/gui/review_tab.py tests/test_ref_library_merge.py
git commit -m "feat(library): non-destructive merge for embedded records"
```

---

### Task 19: Project file v2, document modes in the GUI, stripped-tracking detection

**Files:**
- Modify: `src/models/project.py` (`ProjectState.schema_version: int = 2`, `doc_tracking: Optional[TrackingReport]`; remove `bibliography_pmids` / `pmid_to_bib_number` after `grep -rn "bibliography_pmids\|pmid_to_bib_number" src` shows no readers)
- Modify: `src/storage/project_io.py` (`upgrade_project_data(data) -> data` called before `model_validate`; v1 files (no `schema_version`) get `schema_version = 2`, dropped keys removed)
- Modify: `src/gui/main_window.py` (`_analyze_document` sets `doc_tracking`; after analysis, if `report.tier == LEGACY and project.record_order and Path(path).resolve() == Path(project.output_docx_path or "").resolve()` → mode `stripped`; `TRACKED` → mode `tracked`; `NEWER_VERSION` → mode `newer-version` with export disabled)
- Modify: `src/gui/inputs_tab.py` (`set_document_mode` texts: tracked "Tracked document: N citations / M references recognised from embedded AI REFs data. New (REF)/(REFS) markers will be added and everything renumbered."; stripped "This document was exported by AI REFs but its tracking data is gone (edited in Google Docs or Pages?). Falling back to text-based detection."; newer-version "…created by a newer AI REFs; opened read-only.")
- Test: `tests/test_project_io_v2.py`

**Step 1: Write the failing test**

```python
# tests/test_project_io_v2.py
import json

from src.models.embedded import DocumentTier, TrackingReport
from src.models.project import ProjectState
from src.storage.project_io import load_project, save_project


def test_v1_project_upgrades_and_drops_dead_fields(tmp_path):
    v1 = {"project_name": "old", "bibliography_pmids": ["1"], "pmid_to_bib_number": {"1": 1},
          "settings": {"citation_style": "nih_grant"}, "sentences": [], "evidence_map": {}}
    path = tmp_path / "old.airefsproj"
    path.write_text(json.dumps(v1))
    p = load_project(str(path))
    assert p.schema_version == 2 and p.project_name == "old"
    assert not hasattr(p, "bibliography_pmids")


def test_v2_round_trip_keeps_tracking(tmp_path):
    p = ProjectState(doc_id="d1", record_order=["u1", "u2"], uncited=["u3"],
                     doc_tracking=TrackingReport(tier=DocumentTier.TRACKED, field_count=2))
    path = tmp_path / "p.airefsproj"
    save_project(p, str(path))
    data = json.loads(path.read_text())
    assert data["schema_version"] == 2 and "anthropic_api_key" not in json.dumps(data["settings"])
    q = load_project(str(path))
    assert q.doc_id == "d1" and q.record_order == ["u1", "u2"] and q.doc_tracking.tier == DocumentTier.TRACKED
```

**Step 2: Run to verify failure**

Run: `python -m pytest tests/test_project_io_v2.py -q`
Expected: FAIL (`schema_version` missing)

**Step 3: Implement** per the file list; then the GUI modes.

**Step 4: Run tests and the app** → `python -m pytest -q` all pass; in the app: export a document, reopen the export → "Tracked document" banner; open the export in Google Docs, download as DOCX, reopen → "tracking data is gone" banner and legacy detection.

**Step 5: Commit**

```bash
git add src/models/project.py src/storage/project_io.py src/gui/main_window.py src/gui/inputs_tab.py tests/test_project_io_v2.py
git commit -m "feat: project file v2 with tracking mirror; tracked/stripped document modes"
```

---

### Task 20: Legacy export headless, with adoption into fields

**Files:**
- Modify: `src/pipeline/docx_export.py` (add `export_legacy`)
- Modify: `src/gui/main_window.py:490-691` (`_do_insert_export` → collect decisions, call `export_legacy`; delete `_build_merged_bibliography`, `_renumber_existing_citations`, `_format_bib_entry`)
- Test: `tests/test_legacy_adoption.py`

**Behaviour of `export_legacy(project, output_path, decisions) -> ExportStats`:** today's `_do_insert_export` steps 1–5, headless, then adoption:
- Adoption applies only when the exported document is fully numeric (no author-date conversion) and every existing in-text citation was matched (`check_export_guard` passed). Then, after renumbering and marker replacement: for every citation-shaped superscript run outside a field, and every bracket group outside a field (`BRACKET_CITE_PATTERN` over `paragraph.text` minus result spans), build items from the assignments (`bib_entries[old].matched_candidate` if enriched, else a minimal `CitationCandidate(title=parsed title, year, raw_entry=body text)` with a fresh `record_uuid`) and replace the run/group with a field via `insert_citation_field` (superscript runs: replace the run in place using the same helper with `marker_text = run text`). `legacy_adopted` counts the sites. New markers get fields as in `export_fresh`. The bibliography is written as a BIBL field (existing entries keep their body text verbatim, stored in `custom.airefs.rawEntry`).
- If adoption is not possible (author-date conversion chosen, or `embed_citation_fields` off), no fields at all are written — plain text export exactly as today — and `stats.legacy_adopted = -1` so the summary says "not tracked".

**Step 1: Write the failing test**

```python
# tests/test_legacy_adoption.py
import pytest

from src.models.embedded import DocumentTier
from src.models.evidence import EvidenceRecord, ReviewDecision
from src.models.project import CitationStyle, ProjectState
from src.models.sentence import MarkerType, SentenceRecord
from src.pipeline.docx_export import ExportBlocked, ExportDecisions, export_legacy
from src.pipeline.existing_citation_parser import ExistingCitationParser
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder
from tests.test_round_trip import make_citation


def _legacy_doc(tmp_path, n_entries=3, cite_all=True):
    b = DocBuilder()
    p = b.paragraph("Old claim")
    b.add_text(p, "1-3" if cite_all else "1", superscript=True)
    b.add_text(p, ". New claim (REF).")
    b.paragraph("References")
    for i in range(1, n_entries + 1):
        b.paragraph(f"{i}. Author{i} A. Old title {i}. J Old. 2010;{i}:1-2. doi:10.1/old{i} PMID: 200{i}")
    return b.save(tmp_path / "legacy.docx")


def _project(path, citations):
    p = ProjectState(settings={"citation_style": CitationStyle.NIH_GRANT})
    p.input_docx_path = str(path)
    p.existing_citations = ExistingCitationParser(DocxHandler(str(path))).analyze()
    p.is_insert_mode = True
    sent = SentenceRecord(id="S001", paragraph_index=0, raw_text="New claim (REF).", clean_text="New claim.",
                          marker_type=MarkerType.REF, marker_count=1, marker_types=[MarkerType.REF])
    p.sentences = [sent]
    p.evidence_map = {"S001": EvidenceRecord(sentence_id="S001", selected=citations, review_decision=ReviewDecision.ACCEPTED)}
    return p


def test_legacy_document_is_adopted_then_reopens_tracked(tmp_path):
    path = _legacy_doc(tmp_path)
    p = _project(path, [make_citation(9)])
    out = tmp_path / "out.docx"
    stats = export_legacy(p, str(out), ExportDecisions())
    assert stats.legacy_adopted == 1 and stats.fields_written == 2 and stats.bibliography_size == 4
    m = ExistingCitationParser(DocxHandler(str(out))).analyze()
    assert m.tracking.tier == DocumentTier.TRACKED and m.tracking.record_count == 4
    assert [m.bib_entries[n].pmid for n in (1, 2, 3, 4)] == ["2001", "2002", "2003", "30000009"]
    assert DocxHandler(str(out)).get_paragraphs()[0].text == "Old claim1-3. New claim 4."


def test_partial_parse_is_blocked_then_seeded_when_guard_lowered(tmp_path):
    path = _legacy_doc(tmp_path, n_entries=50, cite_all=False)
    p = _project(path, [make_citation(9)])
    with pytest.raises(ExportBlocked):
        export_legacy(p, str(tmp_path / "blocked.docx"), ExportDecisions())
    p.settings.min_match_ratio = 0.0
    out = tmp_path / "out.docx"
    stats = export_legacy(p, str(out), ExportDecisions())
    assert stats.entries_seeded_uncited == 49 and stats.bibliography_size == 51


def test_author_date_conversion_writes_no_fields(tmp_path):
    path = _legacy_doc(tmp_path)
    p = _project(path, [make_citation(9)])
    p.settings.citation_style = CitationStyle.APA
    out = tmp_path / "apa.docx"
    stats = export_legacy(p, str(out), ExportDecisions(convert_to_author_date=True))
    assert stats.legacy_adopted == -1
    assert DocxHandler(str(out)).fields.airefs_cite == 0
```

**Step 2: Run to verify failure** → `ImportError: cannot import name 'export_legacy'`.

**Step 3: Implement** per the behaviour list.

**Step 4: Run tests** → `python -m pytest -q` all pass. Run the app end-to-end on a legacy export from the previous app version.

**Step 5: Commit**

```bash
git add src/pipeline/docx_export.py src/gui/main_window.py tests/test_legacy_adoption.py
git commit -m "feat(export): headless legacy export with adoption into tracked fields"
```

---

### Task 21: Manual Word acceptance gate, docs, tag

**Files:**
- Modify: `README.md` (section "Tracked documents": what the fields are, that Word shows them with Alt+F9, never edit numbers by hand, Google Docs/Pages strip them)
- Create: `docs/plans/2026-09-05-word-acceptance-checklist.md` (the checklist below with a column for the result)

**Checklist (Word for Mac; record pass/fail):**
1. Open an AI REFs export: citations render normally; no spelling squiggles under numbers.
2. Alt+F9 shows ` ADDIN AIREFS.CITE {…} ` codes; Alt+F9 again hides them.
3. Select all, F9: nothing changes (fldLock).
4. Save (no edits) → reopen in AI REFs → banner "Tracked document", counts unchanged.
5. Edit body text, add a `(REF)`, save → AI REFs run + export → numbering correct.
6. Delete a citation with Track Changes on, save → AI REFs reports pending tracked changes; accept the change, save → export succeeds and drops the record.
7. Tools → Protect → Document Inspector, remove document properties and personal information → reopen in AI REFs → still tracked.
8. Copy a paragraph containing a citation, paste twice → AI REFs reports duplicates, export renumbers correctly.
9. Google Docs: upload, download as DOCX → AI REFs shows the "tracking data is gone" banner and the legacy path still works.
10. Large document: an NIH proposal with ~200 citations exports and reopens in under 10 s.

If 3, 4 or 7 fail, stop and reconsider the carrier (the `w:sdt` contingency in the design).

Commit and tag: `git commit -am "docs: tracked-document notes and Word acceptance checklist" && git tag phase1-fields`.
