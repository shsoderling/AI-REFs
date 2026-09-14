#!/usr/bin/env python3
"""Cite the markers that sit inside table cells, after the main write.

    python fill_table_markers.py --docx paper_with_refs.docx --out paper_final.docx \
        --assign table_citations.json

`scan_markers.py` reports markers in table cells under
`document.markers_in_tables`, because the pipeline (like the app) reads body
paragraphs only and would leave them in the document as literal `(REF)` text.
This fills them in with the numbers of papers that are *already* in the
document's bibliography.

Two honest limitations to pass on to the user:

* the number written here is plain text, not a tracked field, so a later pass
  that renumbers the document will not update it: re-run this script after any
  later write;
* only papers already cited in the body can be used, since this script does
  not touch the bibliography.  For a paper cited nowhere else, move the
  sentence out of the table instead.

assignments file:

    {"markers": [{"location": "table 1 row 2, column 1", "text": "(REF)",
                  "keys": ["27680697"]}]}

`location` and `text` are copied from `document.markers_in_tables`; `keys` are
PMIDs, DOIs or PMC ids of papers already in the document.
"""

import argparse
import json
from pathlib import Path

from common import die, load_json  # noqa: E402

from airefs.models.project import CitationStyle
from airefs.pipeline.citation_render import parse_csl_layout
from airefs.pipeline.existing_citation_parser import ExistingCitationParser
from airefs.services.docx_io import DocxHandler


def cell_paragraphs(handler):
    """Every table-cell paragraph with the location string scan_markers.py prints."""
    out = []

    def walk(table, path):
        for r, row in enumerate(table.rows):
            for c, cell in enumerate(row.cells):
                loc = f"{path} row {r + 1}, column {c + 1}"
                for para in cell.paragraphs:
                    out.append((loc, para))
                for nested in cell.tables:
                    walk(nested, f"{path} (nested)")

    for t, table in enumerate(getattr(handler.doc, "tables", []) or []):
        walk(table, f"table {t + 1}")
    return out


def number_map(existing) -> dict:
    """Identifier -> the number that paper already has in this document."""
    numbers = {}
    for n, entry in existing.bib_entries.items():
        for key in ((entry.pmid or "").strip(), (entry.doi or "").strip().lower()):
            if key:
                numbers[key] = n
        cand = entry.matched_candidate
        if cand is not None and (cand.pmcid or "").strip():
            numbers[cand.pmcid.strip().upper()] = n
    return numbers


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docx", required=True, help="document written by write_docx.py")
    ap.add_argument("--out", required=True, help="output .docx (never the input)")
    ap.add_argument("--assign", required=True, help="assignments JSON (see this script's header)")
    ap.add_argument("--style", default="nih_grant", help="citation style id, for superscript vs bracket")
    args = ap.parse_args()

    for path in (args.docx, args.assign):
        if not Path(path).exists():
            die(f"no such file: {path}")
    if Path(args.out).resolve() == Path(args.docx).resolve():
        die("refusing to overwrite the input document; choose another --out")
    try:
        layout = parse_csl_layout(CitationStyle(args.style))
    except ValueError:
        die(f"unknown style {args.style!r}")

    assignments = load_json(args.assign).get("markers") or []
    if not assignments:
        die("the assignments file lists no markers")

    handler = DocxHandler(args.docx)
    existing = ExistingCitationParser(handler).analyze()
    numbers = number_map(existing)
    if not numbers:
        die("this document has no readable bibliography; run write_docx.py first")

    paragraphs = cell_paragraphs(handler)
    written, problems = [], []
    for item in assignments:
        loc = str(item.get("location", "")).strip()
        text = str(item.get("text", "")).strip()
        keys = [str(k).strip() for k in (item.get("keys") or []) if str(k).strip()]
        if not (loc and text and keys):
            problems.append(f"{loc or '?'}: needs location, text and keys")
            continue
        nums = []
        for key in keys:
            n = numbers.get(key) or numbers.get(key.lower()) or numbers.get(key.upper())
            if n is None:
                problems.append(f"{loc}: {key} is not in this document's bibliography; "
                                "cite it in the body first, or move the sentence out of the table")
                nums = []
                break
            nums.append(n)
        if not nums:
            continue
        rendered = layout.prefix + layout.delimiter.join(str(n) for n in sorted(set(nums))) + layout.suffix
        done = False
        for cell_loc, para in paragraphs:
            if cell_loc != loc or text not in para.text:
                continue
            if handler.replace_marker_by_regex(para, text, rendered,
                                               superscript=layout.is_superscript) is not None:
                written.append({"location": loc, "marker": text, "numbers": nums, "text": rendered})
                done = True
                break
        if not done:
            problems.append(f"{loc}: no marker {text!r} found there")

    if written:
        handler.save(args.out)
    print(json.dumps({
        "output": str(Path(args.out).resolve()) if written else None,
        "filled": written,
        "problems": problems,
        "note": ("These numbers are plain text, not tracked citations: a later renumbering pass "
                 "will not update them, so re-run this script after any later write."),
    }, indent=2))
    if problems and not written:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
