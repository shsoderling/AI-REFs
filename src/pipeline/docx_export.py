"""Headless export: the hard guard, then the fresh / legacy / tracked writers.

The GUI collects decisions (output path, prompts) and calls in here; nothing
in this module touches Qt, so every export path is testable end to end.
"""

import logging
import re
import uuid
from typing import Optional

from ..models.citation import CitationCandidate
from ..models.embedded import DocumentTier
from ..models.existing_refs import ExistingCitationMap
from ..models.project import CitationStyle, ProjectSettings, ProjectState
from ..services.docx_io import DocxHandler
from .bib_format import format_bib_entry
from .citation_payload import build_bibl_code, build_cite_code, entry_hash
from .citation_render import UNRESOLVED_TEXT, CitationLayout, parse_csl_layout, render_cluster
from .export_stats import ExportStats
from .renumber_plan import build_renumber_plan
from .renumbering import RenumberingResult

logger = logging.getLogger(__name__)

BIBLIOGRAPHY_HEADING = "References"


class ExportBlocked(Exception):
    """The export was refused before any write; ``reasons`` says why."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


def fresh_append_needs_confirmation(existing: Optional[ExistingCitationMap]) -> bool:
    """True when the document has a References heading but no entry AI REFs
    could read (author-date lists, unusual numbering): a fresh export would
    append a second bibliography, which the user may explicitly accept."""
    return (existing is not None and existing.references_heading_para_idx >= 0
            and not existing.bib_entries)


def check_export_guard(existing: Optional[ExistingCitationMap], mode: str,
                       settings: ProjectSettings, allow_fresh_append: bool = False) -> list[str]:
    """Reasons an export in *mode* (``fresh`` / ``legacy`` / ``tracked``) must not run.

    An empty list means go ahead. The guard exists because the legacy path
    deletes and rebuilds the bibliography: a partial parse, a misdetected
    document or another manager's fields would otherwise lose citations.
    *allow_fresh_append* is the user's explicit confirmation for the case
    :func:`fresh_append_needs_confirmation` describes; it never overrides a
    document whose entries were actually parsed.
    """
    reasons: list[str] = []
    if existing is None:
        return reasons
    tracking = existing.tracking
    if tracking is not None and tracking.tier == DocumentTier.FAILED:
        reasons.append("Analysis of the document's existing citations failed: "
                       + "; ".join(tracking.problems))
        return reasons
    if tracking is not None and tracking.foreign_field_count:
        reasons.append(
            f"{tracking.foreign_field_count} citation field(s) from another reference "
            "manager are present. AI REFs does not edit those documents.")
    if mode == "fresh":
        if existing.bib_entries:
            reasons.append(
                "This document already has a References section with numbered entries; "
                "a fresh export would append a second one. Reload it so AI REFs can "
                "detect the existing citations.")
        elif existing.references_heading_para_idx >= 0 and not allow_fresh_append:
            reasons.append(
                "This document has a References heading but no numbered entries AI REFs "
                "can read. Exporting would append a second bibliography; confirm to "
                "treat the document as uncited.")
    if mode == "legacy" and tracking is not None and tracking.problems:
        reasons.append("The existing citations could not be read reliably: "
                       + "; ".join(tracking.problems))
    if mode == "legacy" and existing.bib_entries:
        matched = len({c.number for cites in existing.in_text_citations.values()
                       for c in cites} & set(existing.bib_entries))
        total = len(existing.bib_entries)
        if matched == 0 or matched / total < settings.min_match_ratio:
            reasons.append(
                f"Only {matched} of {total} reference entries could be matched to in-text "
                f"citations ({matched / total:.0%}). Refusing to rebuild the bibliography; "
                "check the document's citation format.")
    if existing.pending_tracked_changes and not settings.allow_export_with_tracked_changes:
        reasons.append(
            "The document has pending tracked changes around citations. Accept or reject "
            "them in Word first (or enable 'Export anyway' in settings).")
    return reasons


# ── fresh export ──────────────────────────────────────────────────────

def _bibliography_sort_key(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower())[:80]


def render_new_bibliography(result: RenumberingResult, style: CitationStyle,
                            layout: CitationLayout) -> tuple[list[str], list[str]]:
    """Entries and record uuids (rendered order) for a numbering made of new
    candidates only. Numeric styles list by number; author-date styles list
    alphabetically without numbers."""
    rows = []
    for num in sorted(result.assignments):
        assignment = result.assignments[num]
        cand = assignment.candidate
        if cand is None:
            continue
        rows.append((format_bib_entry(cand, num, style), cand.record_uuid))
    if layout.is_author_date:
        rows.sort(key=lambda row: _bibliography_sort_key(row[0]))
    return [text for text, _ in rows], [rid for _, rid in rows]


def _write_cluster(handler: DocxHandler, paragraph, marker_text: str, candidates,
                   numbers: list[int], layout: CitationLayout, style: CitationStyle,
                   embed: bool, stats: ExportStats) -> str:
    """Replace one marker with its rendered citation (field or plain run)."""
    unresolved = not candidates
    text = UNRESOLVED_TEXT if unresolved else render_cluster(candidates, numbers, layout)
    superscript = layout.is_superscript and not unresolved
    if embed:
        # Author-date clusters carry no numbers: numbering is a numeric-style notion
        stored_numbers = [] if layout.is_author_date else numbers
        code = build_cite_code(candidates, numbers=stored_numbers, render=layout.render_kind,
                               style=style.value, plain=text, unresolved=unresolved)
        handler.insert_citation_field(paragraph, marker_text, code, text, superscript=superscript)
        stats.fields_written += 1
        if unresolved:
            stats.unresolved_fields += 1
    else:
        handler.replace_marker_by_regex(paragraph, marker_text, text, superscript=superscript)
    return text


def export_fresh(project: ProjectState, output_path: str) -> ExportStats:
    """Export an uncited document: every marker becomes a citation (a tracked
    field unless ``embed_citation_fields`` is off) and a bibliography is
    appended. Raises :class:`ExportBlocked` if the result fails validation;
    nothing is written in that case.
    """
    settings = project.settings
    style = settings.citation_style
    embed = settings.embed_citation_fields
    handler = DocxHandler(project.input_docx_path)
    layout = parse_csl_layout(style)
    plan = build_renumber_plan(handler, project)
    result = plan.renumber_result
    stats = ExportStats(total_markers=len(plan.markers), output_path=output_path)
    logger.info(f"Fresh export: style={style.value} render={layout.render_kind} "
                f"markers={len(plan.markers)} embed={embed}")

    for i, marker_info in enumerate(plan.markers):
        paragraph = marker_info['paragraph']
        marker_text = f"({marker_info['marker_type']})"
        candidates = plan.marker_resolved_map[i]
        sentence = plan.marker_sentences[i]
        numbers = [n for n in (result.number_for_candidate(c) for c in candidates) if n is not None]
        _write_cluster(handler, paragraph, marker_text, candidates, numbers, layout, style,
                       embed, stats)
        if candidates:
            stats.resolved_markers += 1
        else:
            stats.unresolved_markers += 1
            if sentence and sentence.id not in stats.unresolved_sentence_ids:
                stats.unresolved_sentence_ids.append(sentence.id)
            logger.warning(f"  Marker {i} in paragraph {marker_info['para_index']} exported as [?]")

    entries, order = render_new_bibliography(result, style, layout)
    if entries:
        project.doc_id = project.doc_id or str(uuid.uuid4())
        if embed:
            code = build_bibl_code(doc_id=project.doc_id, style=style.value, render=layout.render_kind,
                                   heading_text=BIBLIOGRAPHY_HEADING, order=order, uncited=[],
                                   entry_hashes=[entry_hash(e) for e in entries])
            handler.write_bibliography_field(entries, code, heading_text=BIBLIOGRAPHY_HEADING)
        else:
            handler.append_bibliography(entries)
    if embed:
        problems = handler.validate_before_save(expected_cite_fields=len(plan.markers))
        if problems:
            raise ExportBlocked(problems)
    handler.save(output_path)
    project.output_docx_path = output_path
    project.record_order = list(order)
    project.uncited = []
    stats.new_refs_added = len(entries)
    stats.bibliography_size = len(entries)
    logger.info(f"Exported document with {len(entries)} bibliography entries: {output_path}")
    return stats
