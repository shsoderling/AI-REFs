#!/usr/bin/env python3
"""Write the cited document: markers become citations, a bibliography is built.

    python write_docx.py --plan plan.json --records records.json --decisions decisions.json \
        --out paper_with_refs.docx [--style nih_grant] [--report report.md] [--no-fields]

Uses the same writer as the AI REFs desktop app: every citation becomes a
hidden Word field carrying the reference's record (so the app, or this
skill, can reopen the file, add markers and renumber), the bibliography is a
second field, and documents that already carry citations are renumbered in
place (tracked) or adopted (legacy plain-text numbering).  Markers left
unchanged in decisions.json keep their text and take no number; a pending
(REF)/(REFS) marker becomes a [?] placeholder.
"""

import argparse
import json
from pathlib import Path

from common import die, index_records, load_json, lookup_record  # noqa: E402

from airefs.models.embedded import DocumentTier
from airefs.models.evidence import EvidenceRecord, ReviewDecision
from airefs.models.markers import MarkerConfig
from airefs.models.project import CitationStyle, ProjectState
from airefs.pipeline.docx_export import (
    ExportBlocked, ExportDecisions, check_export_guard, export_fresh, export_legacy, export_tracked,
    fresh_append_needs_confirmation,
)
from airefs.pipeline.document_parser import DocumentParser
from airefs.pipeline.existing_citation_parser import ExistingCitationParser
from airefs.pipeline.export_slots import ACTION_CITE, ACTION_LEAVE
from airefs.pipeline.marker_locator import MarkerLocator
from airefs.pipeline.renumber_plan import build_renumber_plan
from airefs.services.docx_io import DocxHandler

DECISIONS = {
    "accepted": ReviewDecision.ACCEPTED,
    "modified": ReviewDecision.MODIFIED,
    "skipped": ReviewDecision.SKIPPED,
    "leave": ReviewDecision.SKIPPED,
    "unchanged": ReviewDecision.SKIPPED,
    "pending": ReviewDecision.PENDING,
    "rejected": ReviewDecision.REJECTED,
}


def build_project(plan: dict, decisions: dict, records, style: CitationStyle, embed: bool,
                  keep_uncited: bool, allow_tracked_changes: bool) -> tuple[ProjectState, str, dict]:
    docx = plan["docx"]
    if not Path(docx).exists():
        die(f"the document in plan.json no longer exists: {docx}")
    project = ProjectState(input_docx_path=docx)
    project.settings.citation_style = style
    project.settings.embed_citation_fields = embed
    project.settings.keep_uncited_entries = keep_uncited
    project.settings.allow_export_with_tracked_changes = allow_tracked_changes

    handler = DocxHandler(docx)
    existing = ExistingCitationParser(handler, keep_uncited=keep_uncited).analyze()
    project.existing_citations = existing
    project.doc_tracking = existing.tracking
    tracking = existing.tracking
    if tracking is not None and tracking.tier == DocumentTier.TRACKED:
        mode = "tracked"
    elif existing.has_existing_citations:
        mode = "legacy"
    else:
        mode = "fresh"
    project.is_insert_mode = mode != "fresh"

    # Re-parse with the configuration recorded in the plan so sentence ids and
    # markers are exactly the ones the plan (and the decisions) refer to.
    config = MarkerConfig(**plan["marker_config"])
    stop_at = existing.body_end_para_idx if project.is_insert_mode else -1
    sentences = DocumentParser(handler, stop_at_para=stop_at, marker_config=config).parse()
    MarkerLocator(config).locate(sentences)
    project.sentences = sentences
    project.run_marker_config = config
    by_id = {s.id: s for s in sentences if s.marker_type is not None}
    plan_ids = [s["id"] for s in plan["sentences"]]
    if sorted(by_id) != sorted(plan_ids):
        die("the document's markers differ from plan.json (was the file edited?). "
            "Run scan_markers.py again and redo the decisions.")

    justification: dict[str, dict] = {}
    for sid, entry in (decisions.get("sentences") or {}).items():
        sentence = by_id.get(sid)
        if sentence is None:
            die(f"decisions.json names unknown sentence {sid}")
        decision = DECISIONS.get(str(entry.get("decision", "pending")).lower())
        if decision is None:
            die(f"{sid}: unknown decision {entry.get('decision')!r} (accepted, modified, skipped, pending)")
        slots = entry.get("slots") or []
        if slots and not isinstance(slots[0], list):
            slots = [slots]                                   # a flat list means one slot
        n_slots = sentence.slot_count
        if len(slots) < n_slots:
            slots = slots + [[] for _ in range(n_slots - len(slots))]
        if len(slots) > n_slots:
            die(f"{sid}: {len(slots)} slots given but the sentence has {n_slots} marker slot(s)")
        selected, sizes = [], []
        for slot_keys in slots:
            block = []
            for key in slot_keys:
                cand = lookup_record(records, key)
                if cand is None:
                    die(f"{sid}: no record for {key!r}; fetch it with fetch_records.py or add it to records.json")
                cand = cand.model_copy()
                meta = (entry.get("citations") or {}).get(str(key)) or {}
                if meta.get("score") is not None:
                    cand.composite_score = float(meta["score"])
                if meta.get("rationale"):
                    cand.score_rationale = str(meta["rationale"])
                block.append(cand)
            selected.extend(block)
            sizes.append(len(block))
        ev = EvidenceRecord(sentence_id=sid, selected=selected, slot_sizes=sizes, review_decision=decision)
        ev.confidence_rationale = str(entry.get("rationale", "") or "")
        project.evidence_map[sid] = ev
        justification[sid] = entry
    return project, mode, justification


def write_report(path: str, plan: dict, project: ProjectState, mode: str, stats, justification: dict) -> None:
    """A markdown justification report: one section per sentence."""
    handler = DocxHandler(project.input_docx_path)
    numbers = {}
    try:
        rplan = build_renumber_plan(handler, project)
        for i, info in enumerate(rplan.markers):
            for cand in rplan.marker_resolved_map[i]:
                n = rplan.renumber_result.number_for_candidate(cand)
                if n is not None:
                    numbers[cand.pmid or cand.doi or cand.title] = n
    except Exception:
        pass
    lines = [f"# Citation report for {Path(project.input_docx_path).name}", "",
             f"Document mode: {mode}. Style: {project.settings.citation_style.value}.", "",
             "## Summary", ""]
    lines += [f"- {line.strip()}" for line in stats.summary_lines()]
    lines += ["", "## Sentences", ""]
    for s in plan["sentences"]:
        ev = project.evidence_map.get(s["id"])
        entry = justification.get(s["id"], {})
        decision = ev.review_decision.value if ev else "pending"
        lines.append(f"### {s['id']} ({decision})")
        lines.append("")
        lines.append(f"> {s['raw_text']}")
        lines.append("")
        if ev is None or not ev.selected:
            if ev is not None and ev.review_decision == ReviewDecision.SKIPPED:
                lines.append("Left unchanged.")
            else:
                lines.append("No citation (marker exported as [?] if it was (REF)/(REFS)).")
            lines.append("")
            continue
        ranges = ev.slot_ranges(s["slot_count"])
        for slot, (start, end) in enumerate(ranges):
            marker_text = s["markers"][slot]["text"] if slot < len(s["markers"]) else f"slot {slot + 1}"
            for cand in ev.selected[start:end]:
                key = cand.pmid or cand.doi or cand.title
                meta = (entry.get("citations") or {}).get(key) or (entry.get("citations") or {}).get(cand.doi.lower() if cand.doi else "", {}) or {}
                n = numbers.get(key)
                head = f"- **{marker_text}** → " + (f"[{n}] " if n else "") + f"{cand.first_author_year}. {cand.title}"
                ident = " | ".join(x for x in (f"PMID {cand.pmid}" if cand.pmid else "",
                                              f"doi:{cand.doi}" if cand.doi else "",
                                              cand.pmcid or "") if x)
                lines.append(head + (f" ({ident})" if ident else ""))
                if meta.get("score") is not None:
                    lines.append(f"  - Score: {meta['score']}/100. {meta.get('rationale', '')}".rstrip())
                if meta.get("verdict"):
                    quote = f' "{meta["quote"]}"' if meta.get("quote") else ""
                    lines.append(f"  - Verification: {meta['verdict']}{quote}")
                for w in meta.get("warnings", []) or []:
                    lines.append(f"  - Warning: {w}")
        if entry.get("rationale"):
            lines.append(f"- Assessment: {entry['rationale']}")
        lines.append("")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", required=True, help="plan.json from scan_markers.py")
    ap.add_argument("--records", action="append", required=True,
                    help="records.json (repeatable; from fetch_records.py / resolve_suggestions.py)")
    ap.add_argument("--decisions", required=True, help="decisions.json (see references/file-formats.md)")
    ap.add_argument("--out", required=True, help="output .docx (never the input file)")
    ap.add_argument("--style", help="citation style id, e.g. nih_grant, vancouver, apa, nature "
                                    "(default: decisions.json 'style', else nih_grant)")
    ap.add_argument("--no-fields", action="store_true",
                    help="plain-text citations instead of tracked Word fields (not reopenable by the app)")
    ap.add_argument("--report", help="also write a markdown justification report here")
    ap.add_argument("--convert-author-date", choices=["no", "yes"], default="no",
                    help="legacy numeric document + author-date style: convert existing citations too")
    ap.add_argument("--allow-fresh-append", action="store_true",
                    help="document has a References heading but no readable entries: append a new bibliography anyway")
    ap.add_argument("--keep-uncited", action="store_true", help="tracked documents: keep entries nothing cites")
    ap.add_argument("--allow-tracked-changes", action="store_true",
                    help="export even if citations carry pending Word tracked changes")
    args = ap.parse_args()

    plan = load_json(args.plan)
    decisions = load_json(args.decisions)
    style_id = args.style or decisions.get("style") or "nih_grant"
    try:
        style = CitationStyle(style_id)
    except ValueError:
        die(f"unknown style {style_id!r}; one of: {', '.join(s.value for s in CitationStyle)}")
    out = Path(args.out)
    if out.resolve() == Path(plan["docx"]).resolve():
        die("refusing to overwrite the input document; choose another --out")

    records = index_records(args.records)
    project, mode, justification = build_project(
        plan, decisions, records, style, embed=not args.no_fields,
        keep_uncited=args.keep_uncited, allow_tracked_changes=args.allow_tracked_changes)

    reasons = check_export_guard(project.existing_citations, mode, project.settings,
                                 allow_fresh_append=args.allow_fresh_append)
    if reasons:
        if mode == "fresh" and fresh_append_needs_confirmation(project.existing_citations):
            reasons.append("(pass --allow-fresh-append to treat the document as uncited)")
        die("export blocked: " + " | ".join(reasons))

    try:
        if mode == "tracked":
            stats = export_tracked(project, str(out))
        elif mode == "legacy":
            stats = export_legacy(project, str(out),
                                  ExportDecisions(convert_to_author_date=(args.convert_author_date == "yes")))
        else:
            stats = export_fresh(project, str(out))
    except ExportBlocked as exc:
        die("export blocked: " + " | ".join(exc.reasons))

    if args.report:
        write_report(args.report, plan, project, mode, stats, justification)

    pending = [s["id"] for s in plan["sentences"] if s["id"] not in project.evidence_map]
    print(json.dumps({
        "output": str(out.resolve()),
        "mode": mode,
        "style": style.value,
        "tracked_fields": not args.no_fields,
        "summary": stats.summary_lines(),
        "unresolved_sentences": stats.unresolved_sentence_ids,
        "sentences_without_decision": pending,
        "report": str(Path(args.report).resolve()) if args.report else None,
    }, indent=2))


if __name__ == "__main__":
    main()
