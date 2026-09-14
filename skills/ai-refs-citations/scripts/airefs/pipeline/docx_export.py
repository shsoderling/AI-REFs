"""Headless export: the hard guard, then the fresh / legacy / tracked writers.

The GUI collects decisions (output path, prompts) and calls in here; nothing
in this module touches Qt, so every export path is testable end to end.

Markers are written per :class:`~.renumber_plan.RenumberPlan`: a marker
with reviewed citations becomes a citation cluster, an unresolved
``(REF)``/``(REFS)`` becomes ``[?]`` (a pending field when embedding), and a
marker left as written (the user's choice, or an author-suggested citation
that was never confirmed) keeps its text and takes no number.
"""

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Optional

from ..models.citation import CitationCandidate
from ..models.embedded import DocumentTier
from ..models.existing_refs import ExistingCitationMap
from ..models.project import CitationStyle, ProjectSettings, ProjectState
from ..services.docx_io import DocxHandler
from .author_date_convert import (
    build_author_date_bibliography, build_author_date_labels, convert_in_text_to_author_date,
)
from .bib_format import format_bib_entry
from .citation_numbers import expand_bracket_numbers
from .csl_mapping import ensure_record_uuid
from .existing_citation_parser import (
    BRACKET_CITE_PATTERN, in_field_result, superscript_groups,
)
from .export_slots import ACTION_LEAVE
from .renumber_apply import apply_renumbering
from .citation_payload import build_bibl_code, build_cite_code, entry_hash
from .citation_render import layout_for_existing_shape
from .field_citation_reader import build_tracked_map
from .tracked_renumber import (
    blocked_table_fields, collect_clusters, merge_adjacent_clusters, orphan_heading,
    render_tracked_bibliography, rewrite_clusters,
)
from .citation_render import UNRESOLVED_TEXT, CitationLayout, parse_csl_layout, render_cluster
from .export_stats import ExportStats
from .renumber_plan import RenumberPlan, build_renumber_plan
from .renumbering import RenumberingResult

logger = logging.getLogger(__name__)

BIBLIOGRAPHY_HEADING = "References"
STRIPPED_NOTE = ("This document was exported by AI REFs but its citation tracking data is gone "
                 "(edited in Google Docs or Pages?). Citations are read from the text.")


@dataclass
class ExportDecisions:
    """Answers the GUI collected before a legacy export (see export_legacy)."""
    convert_to_author_date: Optional[bool] = None   # numeric document + author-date style


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
    if tracking is not None and tracking.tier == DocumentTier.NEWER_VERSION:
        reasons.append("This document was created by a newer version of AI REFs and is "
                       "opened read-only. Update AI REFs to edit it.")
        return reasons
    if tracking is not None:
        damaged = sum(1 for i in tracking.reconcile if i.kind == 'damaged')
        if damaged:
            reasons.append(
                f"{damaged} citation field(s) are damaged: their hidden data could not be read, "
                "so AI REFs cannot tell which reference they cite. In Word, show the field codes "
                "(Alt+F9), delete those citations and insert (REF) markers instead, then export.")
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
        blocking = [p for p in tracking.problems if p != STRIPPED_NOTE]
        if blocking:
            reasons.append("The existing citations could not be read reliably: "
                           + "; ".join(blocking))
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
                   embed: bool, stats: ExportStats, occurrence: int = 0) -> str:
    """Replace one marker with its rendered citation (field or plain run).

    *occurrence* is the marker's ordinal among the paragraph's markers with
    the same text: the right one is rewritten even when an earlier marker of
    the paragraph carries the same words (author-date output can look exactly
    like an author-suggested marker such as ``(Smith et al., 2020)``), and
    the ordinal stays valid after renumbering has changed the length of
    citations earlier in the paragraph, which a character offset would not.
    """
    unresolved = not candidates
    text = UNRESOLVED_TEXT if unresolved else render_cluster(candidates, numbers, layout)
    superscript = layout.is_superscript and not unresolved
    if embed:
        # Author-date clusters carry no numbers: numbering is a numeric-style notion
        stored_numbers = [] if layout.is_author_date else numbers
        code = build_cite_code(candidates, numbers=stored_numbers, render=layout.render_kind,
                               style=style.value, plain=text, unresolved=unresolved)
        written = handler.insert_citation_field(paragraph, marker_text, code, text,
                                                superscript=superscript, occurrence=occurrence)
        if written is None:
            raise ExportBlocked([f"The marker {marker_text} could not be found in its paragraph "
                                 f"(\"{paragraph.text[:60]}\"); the document may have changed "
                                 "since the pipeline ran. Reload it and run again."])
        stats.fields_written += 1
        if unresolved:
            stats.unresolved_fields += 1
    else:
        if handler.replace_marker_by_regex(paragraph, marker_text, text, superscript=superscript,
                                           occurrence=occurrence) is None:
            raise ExportBlocked([f"The marker {marker_text} could not be found in its paragraph "
                                 f"(\"{paragraph.text[:60]}\"). Reload the document and run again."])
    return text


def _count_left(plan: RenumberPlan, i: int, stats: ExportStats) -> None:
    """Account for a marker whose text is kept as written."""
    slot = plan.marker_slots[i]
    if slot is None:
        stats.unmatched_markers += 1
    elif slot.is_skipped:
        stats.skipped_markers += 1
    elif slot.is_suggested:
        stats.left_unverified += 1
    else:
        stats.skipped_markers += 1


def write_new_markers(handler: DocxHandler, plan: RenumberPlan, result: RenumberingResult,
                      layout: CitationLayout, style: CitationStyle, embed: bool,
                      stats: ExportStats) -> set[int]:
    """Write every marker of *plan* into the document.

    Each marker is addressed by its ordinal among the paragraph's markers
    with the same text (``occurrence``), which survives the renumbering of
    existing citations that ran before this; markers are still processed
    right to left within a paragraph so a rendered citation can never be
    counted as a later marker with identical text. Returns the numbers of
    new citations that merged with an existing bibliography entry (legacy
    documents report them).
    """
    stats.total_markers = len(plan.markers)
    merged_duplicate_numbers: set[int] = set()
    order = sorted(range(len(plan.markers)),
                   key=lambda i: (plan.markers[i]['para_index'], -plan.markers[i]['location'][0]))
    for i in order:
        info = plan.markers[i]
        if plan.marker_actions[i] == ACTION_LEAVE:
            _count_left(plan, i, stats)
            logger.info(f"  Marker {i} in paragraph {info['para_index']} left as written: {info['text']}")
            continue
        candidates = plan.marker_resolved_map[i]
        sentence = plan.marker_sentences[i]
        numbers = [n for n in (result.number_for_candidate(c) for c in candidates) if n is not None]
        for n in numbers:
            assignment = result.assignments.get(n)
            if assignment is not None and not assignment.is_new:
                merged_duplicate_numbers.add(n)
        _write_cluster(handler, info['paragraph'], info['text'], candidates, numbers, layout,
                       style, embed, stats, occurrence=info.get('occurrence', 0))
        if candidates:
            stats.resolved_markers += 1
        else:
            stats.unresolved_markers += 1
            if sentence and sentence.id not in stats.unresolved_sentence_ids:
                stats.unresolved_sentence_ids.append(sentence.id)
            logger.warning(f"  Marker {i} in paragraph {info['para_index']} exported as [?]")
    return merged_duplicate_numbers


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
    stats = ExportStats(output_path=output_path)
    logger.info(f"Fresh export: style={style.value} render={layout.render_kind} "
                f"markers={len(plan.markers)} embed={embed}")

    write_new_markers(handler, plan, result, layout, style, embed, stats)

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
        problems = handler.validate_before_save(expected_cite_fields=plan.written_count)
        if problems:
            raise ExportBlocked(problems)
    handler.save(output_path)
    project.output_docx_path = output_path
    project.record_order = list(order)
    project.uncited = []
    project.entry_hashes = [entry_hash(e) for e in entries]
    stats.new_refs_added = len(entries)
    stats.bibliography_size = len(entries)
    logger.info(f"Exported document with {len(entries)} bibliography entries: {output_path}")
    return stats


# ── tracked export ────────────────────────────────────────────────────

def export_tracked(project: ProjectState, output_path: str) -> ExportStats:
    """Export a tracked document: renumber through its citation fields, add
    fields for new markers and rebuild the bibliography field in place.

    The document's fields are the source of truth, so the map is re-read
    from the file being written (offline). Raises :class:`ExportBlocked`
    when the guard refuses (pending tracked changes, damaged analysis), a
    table citation would have to change, or validation fails; nothing is
    written in those cases.
    """
    settings = project.settings
    style = settings.citation_style
    handler = DocxHandler(project.input_docx_path)
    existing = build_tracked_map(handler, keep_uncited=settings.keep_uncited_entries)
    project.existing_citations = existing
    reasons = check_export_guard(existing, "tracked", settings)
    if reasons:
        raise ExportBlocked(reasons)
    layout = parse_csl_layout(style)
    stats = ExportStats(output_path=output_path)
    stats.uncited_dropped = sum(1 for i in existing.tracking.reconcile if i.kind == 'uncited')

    clusters, table_fields = collect_clusters(handler, stats)
    stats.tables_citations = len(table_fields)
    plan = build_renumber_plan(handler, project)
    clusters = merge_adjacent_clusters(clusters, stats)   # after the plan read the offsets
    result = plan.renumber_result
    logger.info(f"Tracked export: style={style.value} clusters={len(clusters)} "
                f"markers={len(plan.markers)} records={len(result.assignments)}")

    blocked = blocked_table_fields(table_fields, existing, result, layout)
    if blocked:
        raise ExportBlocked(blocked)

    # 1. existing citations: new numbers, text and payload
    unwrapped = rewrite_clusters(clusters, existing, result, layout, style, stats)
    handler.invalidate_fields()          # runs were replaced: rebuild the identity index

    # 2. new markers
    write_new_markers(handler, plan, result, layout, style, True, stats)

    # 3. bibliography, replaced in place (or regenerated when its field is gone)
    entries, order, uncited, hashes = render_tracked_bibliography(existing, result, style, layout)
    project.doc_id = project.doc_id or existing.tracking.doc_id or str(uuid.uuid4())
    bibl_field = next((f for f in handler.fields.fields
                       if f.kind == 'airefs_bibl' and f.depth == 0 and not f.deleted), None)
    if entries:
        code = build_bibl_code(doc_id=project.doc_id, style=style.value, render=layout.render_kind,
                               heading_text=BIBLIOGRAPHY_HEADING, order=order, uncited=uncited,
                               entry_hashes=hashes)
        if bibl_field is not None:
            handler.replace_bibliography_field(bibl_field, entries, code)
        else:
            heading = orphan_heading(handler)
            if heading is not None:
                handler.insert_bibliography_after(heading, entries, code)
            else:
                handler.write_bibliography_field(entries, code, heading_text=BIBLIOGRAPHY_HEADING)
            stats.bibliography_regenerated = True
    elif bibl_field is not None and existing.bibliography_span[0] >= 0:
        # Every citation is gone: remove the now-empty bibliography and its heading
        start = existing.references_heading_para_idx
        if start < 0:
            start = existing.bibliography_span[0]
        handler.remove_references_section(start, existing.bibliography_span[1])

    expected = len(clusters) - unwrapped + len(table_fields) + plan.written_count + sum(
        1 for f in handler.fields.fields if f.kind == 'airefs_cite' and f.deleted)
    problems = handler.validate_before_save(expected_cite_fields=expected)
    if problems:
        raise ExportBlocked(problems)
    handler.save(output_path)
    project.output_docx_path = output_path
    project.record_order = list(order)
    project.uncited = list(uncited)
    project.entry_hashes = list(hashes)
    stats.new_refs_added = sum(1 for a in result.assignments.values() if a.is_new)
    stats.bibliography_size = len(entries)
    stats.entries_seeded_uncited = len(result.seeded_uncited)
    logger.info(f"Tracked export complete: {len(entries)} bibliography entries: {output_path}")
    return stats


def looks_stripped(existing: Optional[ExistingCitationMap], path: str,
                   previous_output_path: Optional[str], previous_entry_hashes: list[str]) -> bool:
    """True when a document without citation fields is very likely a former
    AI REFs export whose fields were stripped (Google Docs, Pages, RTF...):
    it is the file the project last exported, or at least half of the
    bibliography entries written then are still there verbatim."""
    if existing is None or existing.tracking is None:
        return False
    if existing.tracking.tier not in (DocumentTier.LEGACY, DocumentTier.STRIPPED) \
            or existing.tracking.field_count:
        return False
    if previous_output_path:
        try:
            from pathlib import Path
            if Path(path).resolve() == Path(previous_output_path).resolve():
                return True
        except OSError:
            pass
    if not previous_entry_hashes or not existing.bib_entries:
        return False
    found = {entry_hash(e.raw_text) for e in existing.bib_entries.values()}
    matched = sum(1 for h in previous_entry_hashes if h in found)
    return matched * 2 >= len(previous_entry_hashes)


# ── legacy (plain-text) export ────────────────────────────────────────

def merged_numeric_bibliography(existing: ExistingCitationMap, result: RenumberingResult,
                                style: CitationStyle) -> list[str]:
    """Plain-text merged bibliography: new entries formatted, existing ones
    re-used verbatim with their new number."""
    entries = []
    for num in sorted(result.assignments):
        assignment = result.assignments[num]
        if assignment.is_new and assignment.candidate:
            entries.append(format_bib_entry(assignment.candidate, num, style))
            continue
        old_entry = existing.bib_entries.get(assignment.original_number)
        if old_entry:
            body = old_entry.body or re.sub(r'^\[?\d+[.\s)\]]*\s*', '', old_entry.raw_text, count=1)
            entries.append(f"{num}. {body}")
        else:
            entries.append(f"{num}. [Missing reference]")
    return entries


def _identify_existing_entries(existing: ExistingCitationMap) -> dict[int, CitationCandidate]:
    """Give every parsed entry a record (its enriched candidate, else a minimal
    one carrying the verbatim entry text) and a stable record uuid."""
    by_old: dict[int, CitationCandidate] = {}
    for old_num, entry in existing.bib_entries.items():
        cand = entry.matched_candidate or CitationCandidate(
            title=entry.title, year=entry.year, journal=entry.journal,
            doi=entry.doi, pmid=entry.pmid)
        cand.raw_entry = entry.body or entry.raw_text
        if entry.record_uuid:
            cand.record_uuid = entry.record_uuid
        entry.record_uuid = ensure_record_uuid(cand)
        entry.matched_candidate = cand
        by_old[old_num] = cand
    return by_old


def _strict_numbers(text: str):
    """Numbers of a citation list, or None when the text is malformed."""
    strict = expand_bracket_numbers(text)
    if not strict or strict != expand_bracket_numbers(text, lenient=True):
        return None
    return strict


def _legacy_sites(handler: DocxHandler, existing: ExistingCitationMap):
    """Every plain-text citation site of the body: ('sup', paragraph, runs,
    text, start) for superscript groups and ('bracket', paragraph, None,
    token, start) for bracket groups, in document order."""
    heading = existing.references_heading_para_idx
    sites = []
    for para_idx, para in enumerate(handler.get_paragraphs()):
        if heading >= 0 and para_idx >= heading:
            break
        fields = handler.fields
        for runs, text, start in superscript_groups(para, fields):
            sites.append(('sup', para, runs, text, start))
        if "[" in para.text:
            spans = fields.result_spans(para)
            for m in BRACKET_CITE_PATTERN.finditer(para.text):
                if not in_field_result(m, spans):
                    sites.append(('bracket', para, None, m.group(0), m.start()))
    return sites


def unadoptable_sites(handler: DocxHandler, existing: ExistingCitationMap) -> int:
    """Citation sites (before renumbering) whose numbers are malformed or do
    not all belong to a parsed bibliography entry. Adoption is all or
    nothing: one such site keeps the whole document untracked."""
    count = 0
    for kind, _para, _runs, text, _start in _legacy_sites(handler, existing):
        inner = text[1:-1] if kind == 'bracket' else text
        numbers = _strict_numbers(inner)
        if numbers is None or any(n not in existing.bib_entries for n in numbers):
            count += 1
    return count


def adopt_existing_sites(handler: DocxHandler, existing: ExistingCitationMap,
                         result: RenumberingResult, layout: CitationLayout,
                         style: CitationStyle, stats: ExportStats) -> int:
    """Wrap every plain-text numeric citation of the body (already renumbered)
    into an AIREFS field, so the next session reads the document exactly.
    Call only when :func:`unadoptable_sites` returned 0. A superscript list
    Word had split into several runs is joined into one run first. Returns
    the number of sites adopted."""
    cand_by_old = _identify_existing_entries(existing)
    new_to_old = {a.final_number: a.original_number
                  for a in result.assignments.values() if not a.is_new}

    def cands_for(numbers):
        cands = []
        for n in numbers:
            old = new_to_old.get(n)
            if old is None or old not in cand_by_old:
                return None
            cands.append(cand_by_old[old])
        return cands or None

    adopted = 0
    for kind, para, runs, text, start in _legacy_sites(handler, existing):
        inner = text[1:-1] if kind == 'bracket' else text
        numbers = _strict_numbers(inner)
        cands = cands_for(numbers) if numbers else None
        if cands is None:
            raise ExportBlocked([f"citation '{text}' could not be matched to a reference while "
                                 "adopting the document; export aborted before any write"])
        code = build_cite_code(cands, numbers=sorted(set(numbers)), render=layout.render_kind,
                               style=style.value, plain=text)
        if kind == 'sup':
            first = runs[0]._r
            if len(runs) > 1:                     # join the split list into one run
                from docx.oxml.ns import qn
                for extra in runs[1:]:
                    extra._r.getparent().remove(extra._r)
                for child in list(first):
                    if child.tag != qn('w:rPr'):
                        first.remove(child)
                from airefs.services.docx_fields import _text_elem
                first.append(_text_elem(text))
            handler.wrap_run_in_field(first, code, text, superscript=True)
        else:
            handler.insert_citation_field(para, text, code, text, superscript=False, start=start)
        adopted += 1
    stats.fields_written += adopted
    return adopted


def export_legacy(project: ProjectState, output_path: str,
                  decisions: Optional[ExportDecisions] = None) -> ExportStats:
    """Export a plain-text (legacy) document: renumber its existing citations,
    add the new ones and rebuild the bibliography in place.

    Numeric documents are adopted into tracked fields (every existing
    citation site becomes a field, the bibliography a field) unless
    embedding is off; an author-date conversion stays plain text, so a
    document is either fully tracked or not tracked at all. Raises
    :class:`ExportBlocked` when the guard refuses or validation fails.
    """
    decisions = decisions or ExportDecisions()
    settings = project.settings
    style = settings.citation_style
    handler = DocxHandler(project.input_docx_path)
    existing = project.existing_citations
    if existing is None:
        raise ExportBlocked(["The document's existing citations have not been analysed."])
    reasons = check_export_guard(existing, "legacy", settings)
    if reasons:
        raise ExportBlocked(reasons)

    layout = parse_csl_layout(style)
    is_author_date = layout.is_author_date
    convert = False
    labels = None
    if is_author_date and existing.in_text_citations:
        labels = build_author_date_labels(existing)
        if labels.missing:
            if decisions.convert_to_author_date:
                raise ExportBlocked([
                    f"Author/year information could not be determined for {len(labels.missing)} "
                    "existing reference(s); enable 'Enrich existing' and re-run the pipeline, "
                    "or export in the document's numeric style."])
            is_author_date = False
        elif decisions.convert_to_author_date:
            convert = True
        else:
            is_author_date = False
    if not is_author_date:
        # New numeric citations follow the document's existing shape
        layout = layout_for_existing_shape(layout, existing.detected_style_is_superscript)
    stats_unadoptable = unadoptable_sites(handler, existing) if not is_author_date else 0
    # All or nothing: a document is tracked only if every citation site can be
    adopt = settings.embed_citation_fields and not is_author_date and stats_unadoptable == 0

    plan = build_renumber_plan(handler, project)
    result = plan.renumber_result
    renumber_map = result.renumber_map
    stats = ExportStats(output_path=output_path)
    stats.entries_seeded_uncited = len(result.seeded_uncited)
    stats.unadoptable_sites = stats_unadoptable
    logger.info(f"Legacy export: style={style.value} existing={len(existing.bib_entries)} "
                f"markers={len(plan.markers)} author_date={is_author_date} adopt={adopt} "
                f"unadoptable={stats_unadoptable}")

    # 1. existing in-text citations (before the new markers, so the inserted
    #    text is never re-processed)
    if convert:
        stats.citations_converted = convert_in_text_to_author_date(
            handler, existing, labels.labels,
            prefix=layout.prefix, suffix=layout.suffix, delimiter=layout.delimiter)
    elif not is_author_date and any(o != n for o, n in renumber_map.items()):
        apply_renumbering(handler, existing, renumber_map)

    # 2. new markers
    merged_duplicate_numbers = write_new_markers(handler, plan, result, layout, style, adopt, stats)
    stats.duplicates_merged = len(merged_duplicate_numbers)

    # 3. adopt the existing citation sites into fields
    stats.legacy_adopted = adopt_existing_sites(handler, existing, result, layout, style, stats) if adopt else -1

    # 4. the bibliography, rebuilt where the old one was
    span_end = existing.bibliography_span[1]
    removed = handler.remove_references_section(
        existing.references_heading_para_idx, span_end if span_end >= 0 else None)
    logger.info(f"Removed {removed} References paragraphs for {len(existing.bib_entries)} entries")
    if adopt:
        for old_num in result.seeded_uncited:
            entry = existing.bib_entries.get(old_num)
            if entry is not None:
                entry.is_uncited = True
        entries, order, uncited, hashes = render_tracked_bibliography(
            existing, result, style, layout, verbatim_existing=True)
        project.doc_id = project.doc_id or str(uuid.uuid4())
        if entries:
            code = build_bibl_code(doc_id=project.doc_id, style=style.value, render=layout.render_kind,
                                   heading_text=BIBLIOGRAPHY_HEADING, order=order, uncited=uncited,
                                   entry_hashes=hashes)
            handler.write_bibliography_field(entries, code, heading_text=BIBLIOGRAPHY_HEADING)
        problems = handler.validate_before_save(
            expected_cite_fields=plan.written_count + stats.legacy_adopted)
        if problems:
            raise ExportBlocked(problems)
        project.record_order = list(order)
        project.uncited = list(uncited)
        project.entry_hashes = list(hashes)
    else:
        if is_author_date:
            entries = build_author_date_bibliography(existing, result, style)
        else:
            entries = merged_numeric_bibliography(existing, result, style)
        if entries:
            handler.append_bibliography(entries)

    handler.save(output_path)
    project.output_docx_path = output_path
    stats.new_refs_added = sum(1 for a in result.assignments.values() if a.is_new)
    stats.existing_refs_renumbered = sum(1 for o, n in renumber_map.items() if o != n)
    stats.bibliography_size = len(entries)
    logger.info(f"Legacy export complete: {len(entries)} bibliography entries: {output_path}")
    return stats
