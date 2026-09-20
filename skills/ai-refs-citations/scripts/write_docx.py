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
import re
from pathlib import Path
from typing import Optional

from common import die, index_records, load_json, lookup_record, record_keys  # noqa: E402

from airefs.models.embedded import DocumentTier
from airefs.models.evidence import EvidenceRecord, ReviewDecision
from airefs.models.markers import MarkerConfig
from airefs.models.project import CitationStyle, ProjectState, get_csl_path
from airefs.pipeline.docx_export import (
    ExportBlocked, ExportDecisions, check_export_guard, export_fresh, export_legacy, export_tracked,
    fresh_append_needs_confirmation,
)
from airefs.pipeline.citation_render import parse_csl_layout
from airefs.pipeline.document_parser import DocumentParser
from airefs.pipeline.existing_citation_parser import ExistingCitationParser
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


def _norm_doi(doi: str) -> str:
    """A DOI as it compares: lower case, no URL prefix, no bioRxiv version suffix."""
    doi = (doi or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
    doi = doi.strip().rstrip(".")
    if doi.startswith("10.1101/"):
        doi = re.sub(r"v\d+$", "", doi)
    return doi


def suggestion_mismatches(plan: dict, project: ProjectState) -> list[str]:
    """Author-suggested identifiers whose slot holds some other paper.

    The one invariant of an author-suggested citation is that it stays the
    author's citation: a PMID, PMC id or DOI the author wrote must end up
    pointing at that record, or at nothing.  This checks it in code rather
    than trusting the review to have gone right.
    """
    problems = []
    for sent in plan["sentences"]:
        ev = project.evidence_map.get(sent["id"])
        if ev is None or not ev.selected:
            continue
        ranges = ev.slot_ranges(sent["slot_count"])
        for marker in sent["markers"]:
            if marker["kind"] != "SUGGESTED":
                continue
            slot = marker["slot"]
            if slot >= len(ranges):
                continue
            start, end = ranges[slot]
            block = ev.selected[start:end]
            if not block:
                continue                                     # unresolved: text kept, fine
            have_pmids = {(c.pmid or "").strip() for c in block}
            have_pmcids = {(c.pmcid or "").strip().upper() for c in block}
            have_dois = {_norm_doi(c.doi) for c in block}
            for sug in marker["suggestions"]:
                kind, value = sug.get("kind", ""), str(sug.get("value", "")).strip()
                if kind == "pmid" and value and value not in have_pmids:
                    problems.append(f"{sent['id']} {marker['text']}: PMID {value} is not among the cited records")
                elif kind == "pmcid" and value and value.upper() not in have_pmcids:
                    problems.append(f"{sent['id']} {marker['text']}: {value} is not among the cited records")
                elif kind == "doi" and value and _norm_doi(value) not in have_dois:
                    problems.append(f"{sent['id']} {marker['text']}: doi {value} is not among the cited records")
    return problems


def style_prints_pmcid(style: CitationStyle) -> bool:
    """Whether the style's reference entries carry the PMC id (NIH grant, NLM)."""
    try:
        return 'variable="PMCID"' in Path(get_csl_path(style)).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False


def records_missing_pmcid(project: ProjectState) -> list[str]:
    """Cited PubMed records with no PMC id, in a style that would print one.

    The NIH grant style ends each entry with the PMCID and prints the PMID
    only when the record has none, so a record written down without its PMC
    id (the connector reports it as identifiers.pmc) quietly turns into a
    "PMID:" entry in the grant. Reported, not fixed: a paper that is not in
    PMC keeps its PMID, and only a lookup tells the two cases apart.
    """
    if not style_prints_pmcid(project.settings.citation_style):
        return []
    missing: dict[str, str] = {}
    for ev in project.evidence_map.values():
        for cand in ev.selected:
            if cand.pmid and not cand.pmcid:
                missing[cand.pmid] = f"{cand.first_author_year} (PMID {cand.pmid})"
    return [missing[pmid] for pmid in sorted(missing)]


def build_project(plan: dict, decisions: dict, records, style: CitationStyle, embed: bool,
                  keep_uncited: bool, allow_tracked_changes: bool,
                  min_match_ratio: Optional[float] = None,
                  bibliography_format: str = "style") -> tuple[ProjectState, str, dict]:
    docx = plan["docx"]
    if not Path(docx).exists():
        die(f"the document in plan.json no longer exists: {docx}")
    project = ProjectState(input_docx_path=docx)
    project.settings.citation_style = style
    project.settings.embed_citation_fields = embed
    project.settings.keep_uncited_entries = keep_uncited
    project.settings.allow_export_with_tracked_changes = allow_tracked_changes
    project.settings.bibliography_format = bibliography_format
    if min_match_ratio is not None:
        project.settings.min_match_ratio = min_match_ratio

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
    # Same condition scan_markers.py used, so both stop parsing at the same
    # paragraph and the sentence ids line up.
    project.is_insert_mode = mode != "fresh" and existing.has_existing_citations

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
    # One object per paper, shared by every citation of it.  Two copies of the
    # same paper would be minted as two records in the document's hidden field
    # list, and a later pass would then read more references than are printed.
    by_identity: dict[str, object] = {}

    def identity_of(cand) -> str:
        return ((cand.pmid or "").strip()
                or (cand.doi or "").strip().lower()
                or (cand.pmcid or "").strip().upper()
                or (cand.title or "").strip().lower())

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
                ident = identity_of(cand)
                shared = by_identity.get(ident)
                cand = shared if shared is not None else cand.model_copy()
                meta = (entry.get("citations") or {}).get(str(key)) or {}
                if meta.get("score") is not None and (shared is None or not cand.composite_score):
                    cand.composite_score = float(meta["score"])
                if meta.get("rationale") and (shared is None or not cand.score_rationale):
                    cand.score_rationale = str(meta["rationale"])
                by_identity[ident] = cand
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
    missing_pmcid = records_missing_pmcid(project)
    if missing_pmcid:
        lines.append(f"- {len(missing_pmcid)} PubMed record(s) have no PMC id on file, so their entries "
                     "print the PMID instead of the PMCID: " + "; ".join(missing_pmcid))
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
                # The decisions file may key a citation by any identifier of the
                # paper, and with or without a PMID:/doi: prefix.
                meta = {}
                for candidate_key in (entry.get("citations") or {}):
                    if lookup_record({k: cand for k in record_keys(cand)}, candidate_key) is not None:
                        meta = (entry.get("citations") or {})[candidate_key] or {}
                        break
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
    ap.add_argument("--allow-retracted", action="store_true",
                    help="write a citation to a retracted paper (refused otherwise)")
    ap.add_argument("--allow-suggestion-substitution", action="store_true",
                    help="allow a slot to hold a paper other than the identifier the author wrote "
                         "(for a deliberate preprint-to-journal swap)")
    ap.add_argument("--bibliography", choices=["style", "nlm"], default="style",
                    help="'style' (default): reference entries follow the citation style's own "
                         "CSL rules. 'nlm': the single NLM-like format every style shared "
                         "before, which documents from earlier app versions carry.")
    ap.add_argument("--append-ids", action="store_true",
                    help="append the DOI and PMID to entries whose style omits them (helps a "
                         "document that later loses its hidden fields be re-matched)")
    ap.add_argument("--min-match-ratio", type=float,
                    help="legacy documents: fraction of reference entries that must match an in-text "
                         "citation before the bibliography is rebuilt (default 0.5)")
    args = ap.parse_args()

    for path in [args.plan, args.decisions] + list(args.records):
        if not Path(path).exists():
            die(f"no such file: {path}")
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

    bibliography_format = ("nlm" if args.bibliography == "nlm"
                           else "style_with_ids" if args.append_ids else "style")
    records = index_records(args.records)
    project, mode, justification = build_project(
        plan, decisions, records, style, embed=not args.no_fields,
        keep_uncited=args.keep_uncited, allow_tracked_changes=args.allow_tracked_changes,
        min_match_ratio=args.min_match_ratio, bibliography_format=bibliography_format)

    # A tracked document is always rewritten with fields; saying otherwise would
    # hand the user a tracked file while telling them it is plain text.
    if args.no_fields and mode == "tracked":
        die("this document already carries AI REFs fields, and a tracked rewrite always writes "
            "fields. To get a field-free document, export the original (untracked) file instead.")

    retracted = sorted({f"{c.first_author_year} ({c.pmid or c.doi})"
                        for ev in project.evidence_map.values() for c in ev.selected if c.is_retracted})
    if retracted and not args.allow_retracted:
        die("refusing to cite retracted paper(s): " + "; ".join(retracted)
            + ". Choose another paper, or pass --allow-retracted if the retraction is the point "
              "of the sentence.")

    # A numeric legacy document written in an author-date style leaves the old
    # numbers pointing at a list that no longer has numbers.
    if (mode == "legacy" and args.convert_author_date != "yes"
            and parse_csl_layout(style).is_author_date
            and project.existing_citations.in_text_citations):
        die(f"this document cites by number but {style.value} is an author-date style, so the "
            "existing numbers would point at an unnumbered list. Pass --convert-author-date yes "
            "to rewrite the existing citations as author-date too (the result is plain text, not "
            "tracked), or choose a numeric style.")

    mismatched = suggestion_mismatches(plan, project)
    if mismatched and not args.allow_suggestion_substitution:
        die("a marker's slot holds a paper the author did not name: " + " | ".join(mismatched)
            + ". Cite what the author wrote, leave the slot empty when it could not be resolved, "
              "or pass --allow-suggestion-substitution for a deliberate preprint-to-journal swap.")

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
    missing_pmcid = records_missing_pmcid(project)
    print(json.dumps({
        "output": str(out.resolve()),
        "mode": mode,
        "style": style.value,
        "tracked_fields": stats.fields_written > 0,
        "bibliography": bibliography_format,
        "retracted_cited": retracted if args.allow_retracted else [],
        "summary": stats.summary_lines(),
        "unresolved_sentences": stats.unresolved_sentence_ids,
        "sentences_without_decision": pending,
        "missing_pmcid": missing_pmcid,
        "missing_pmcid_hint": ("these entries print the PMID because the record has no PMC id; "
                               "look the ids up (convert_article_ids, or fetch_records.py), add "
                               "them to the records file and write again") if missing_pmcid else None,
        "report": str(Path(args.report).resolve()) if args.report else None,
    }, indent=2))


if __name__ == "__main__":
    main()
