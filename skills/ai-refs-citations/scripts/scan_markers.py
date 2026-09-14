#!/usr/bin/env python3
"""Scan a Word document for citation markers and write plan.json.

    python scan_markers.py --docx paper.docx --out plan.json [--no-ids] [--no-author-year]

The plan lists every sentence that carries a marker ((REF), (REFS), or an
author-suggested citation such as (PMID: 32879322) or (Smith et al. 2020)),
with the claim, its context (section, neighbouring sentences) and, for
sentences with several markers, the sub-claim each marker belongs to.  It
also says how the document itself was read: fresh (no citations yet),
tracked (earlier AI REFs export with hidden fields), legacy (plain-text
numbered citations) or foreign (another reference manager's fields).
"""

import argparse
from pathlib import Path

from common import dump_json, die  # noqa: E402  (puts airefs on sys.path)

from airefs.models.embedded import DocumentTier
from airefs.models.markers import MarkerConfig, MarkerType
from airefs.utils.markers import find_markers as find_marker_specs
from airefs.pipeline.claim_context import build_claim_context, sub_claims_for
from airefs.pipeline.document_parser import DocumentParser
from airefs.pipeline.existing_citation_parser import ExistingCitationParser
from airefs.pipeline.marker_locator import MarkerLocator
from airefs.services.docx_io import DocxHandler


def document_mode(existing) -> str:
    """Same banner logic as the app's main window."""
    tracking = existing.tracking
    if tracking is not None:
        if tracking.tier == DocumentTier.FAILED:
            return "failed"
        if tracking.tier == DocumentTier.NEWER_VERSION:
            return "newer-version"
        if tracking.foreign_field_count:
            return "foreign"
        if tracking.tier == DocumentTier.TRACKED:
            return "tracked"
    return "legacy" if existing.has_existing_citations else "fresh"


def markers_out_of_flow(handler, config: MarkerConfig) -> list[dict]:
    """Markers inside tables and text boxes.

    The document parser reads ``document.paragraphs`` only, exactly as the app
    does, so a marker in a table cell is never searched and would survive into
    the exported file as literal text.  Reporting it lets you tell the user
    instead of letting it slip through.
    """
    found = []

    def walk(table, path: str):
        for r, row in enumerate(table.rows):
            for c, cell in enumerate(row.cells):
                for para in cell.paragraphs:
                    for spec in find_marker_specs(para.text, config):
                        found.append({"location": f"{path} row {r + 1}, column {c + 1}",
                                      "text": spec.text, "paragraph_text": para.text[:200]})
                for nested in cell.tables:
                    walk(nested, f"{path} (nested)")

    for t, table in enumerate(getattr(handler.doc, "tables", []) or []):
        walk(table, f"table {t + 1}")
    return found


def scan(docx: str, detect_ids: bool, detect_author_year: bool, keep_uncited: bool = False) -> dict:
    handler = DocxHandler(docx)
    existing = ExistingCitationParser(handler, keep_uncited=keep_uncited).analyze()
    mode = document_mode(existing)
    insert_mode = mode in ("legacy", "foreign", "tracked") and existing.has_existing_citations
    stop_at = existing.body_end_para_idx if insert_mode else -1

    config = MarkerConfig(detect_ids=detect_ids, detect_author_year=detect_author_year)
    sentences = DocumentParser(handler, stop_at_para=stop_at, marker_config=config).parse()
    MarkerLocator(config).locate(sentences)
    marked = [s for s in sentences if s.marker_type is not None]

    out_sentences = []
    counts = {"marked_sentences": len(marked), "markers": 0, "ref": 0, "refs": 0, "suggested": 0, "suggestions": 0}
    for s in marked:
        ctx = build_claim_context(sentences, s)
        parts = sub_claims_for(s)
        markers = []
        for i, m in enumerate(s.markers):
            counts["markers"] += 1
            if m.kind == MarkerType.SUGGESTED:
                counts["suggested"] += 1
                counts["suggestions"] += len(m.suggestions)
            elif m.kind == MarkerType.REFS:
                counts["refs"] += 1
            else:
                counts["ref"] += 1
            sub_claim, trailing = parts[i] if i < len(parts) else ("", "")
            markers.append({
                "index": i,
                "slot": i if s.searched_per_marker else 0,
                "text": m.text,
                "kind": m.kind.value,
                "extra_search": m.extra_search,
                "sub_claim": sub_claim if len(s.markers) > 1 else "",
                "trailing": trailing if len(s.markers) > 1 else "",
                "suggestions": [
                    {**sg.model_dump(), "kind": sg.kind.value, "label": sg.label}
                    for sg in m.suggestions
                ],
            })
        out_sentences.append({
            "id": s.id,
            "paragraph_index": s.paragraph_index,
            "section": s.section or "",
            "raw_text": s.raw_text,
            "clean_text": s.clean_text,
            "slot_count": s.slot_count,
            "searched_per_marker": s.searched_per_marker,
            "context": {
                "preceding": ctx.preceding,
                "following": ctx.following,
                "paragraph": ctx.paragraph,
            },
            "markers": markers,
        })

    tracking = existing.tracking
    document = {
        "mode": mode,
        "insert_mode": insert_mode,
        "existing_references": len(existing.bib_entries),
        "references_heading_paragraph": existing.references_heading_para_idx,
        "existing_style": ("author-date" if existing.detected_style_is_author_date
                           else ("superscript" if existing.detected_style_is_superscript else "bracket")),
        "pending_tracked_changes": existing.pending_tracked_changes,
        "problems": list(tracking.problems) if tracking else [],
        "reconcile": [{"kind": r.kind, "message": r.message} for r in (tracking.reconcile if tracking else [])],
        "field_count": tracking.field_count if tracking else 0,
        "foreign_field_count": tracking.foreign_field_count if tracking else 0,
        "paragraphs": len(handler.get_paragraphs()),
        # [?] left by an earlier export: still missing citations.  They are not
        # markers any more, so tell the user to replace each with (REF).
        "unresolved_placeholders": sum(p.text.count("[?]") for p in handler.get_paragraphs()),
        # Markers the pipeline cannot reach (table cells, text boxes).
        "markers_in_tables": markers_out_of_flow(handler, config),
    }
    return {
        "schema": 1,
        "docx": str(Path(docx).resolve()),
        "marker_config": {"detect_ids": detect_ids, "detect_author_year": detect_author_year},
        "document": document,
        "counts": counts,
        "sentences": out_sentences,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docx", required=True, help="Word document with (REF)/(REFS)/suggested markers")
    ap.add_argument("--out", help="write plan.json here (default: print to stdout)")
    ap.add_argument("--no-ids", action="store_true", help="do not treat (PMID: n), (PMC n), (doi: ...) as markers")
    ap.add_argument("--no-author-year", action="store_true", help="do not treat (Author et al. YEAR) as markers")
    ap.add_argument("--keep-uncited", action="store_true",
                    help="tracked documents: keep bibliography entries whose citations were deleted")
    args = ap.parse_args()
    if not Path(args.docx).exists():
        die(f"no such file: {args.docx}")
    plan = scan(args.docx, detect_ids=not args.no_ids, detect_author_year=not args.no_author_year,
                keep_uncited=args.keep_uncited)
    dump_json(plan, args.out)
    if args.out:
        c = plan["counts"]
        d = plan["document"]
        print(f"{args.out}: {c['marked_sentences']} marked sentence(s), {c['markers']} marker(s) "
              f"({c['ref']} REF, {c['refs']} REFS, {c['suggested']} author-suggested); "
              f"document is {d['mode']}" + (f" with {d['existing_references']} existing references"
                                            if d['existing_references'] else "")
              + (f"; {d['unresolved_placeholders']} leftover [?] placeholder(s)"
                 if d['unresolved_placeholders'] else "")
              + (f"; {len(d['markers_in_tables'])} marker(s) inside tables, which are NOT processed"
                 if d['markers_in_tables'] else ""))


if __name__ == "__main__":
    main()
