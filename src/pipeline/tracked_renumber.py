"""Renumber a tracked document through its citation fields.

The fields are read again from the handler being written (identity is per
Document instance), merged when adjacent, given their new numbers and
rendered text, and rewritten only where text or payload changed. The
bibliography is rebuilt from the numbering result and replaced in place.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from ..models.citation import CitationCandidate
from ..models.existing_refs import ExistingBibEntry, ExistingCitationMap
from ..models.project import CitationStyle
from ..services.docx_fields import ComplexField, rewrite_code, rewrite_result, unwrap_field
from ..services.docx_io import BIBLIOGRAPHY_HEADING_STYLE, DocxHandler
from .bib_format import format_bib_entry
from .citation_payload import (
    CiteItem, CitePayload, PayloadError, build_cite_code, entry_hash, mint_citation_id,
    parse_cite_code,
)
from .citation_render import CitationLayout, render_cluster
from .csl_mapping import from_csl_item
from .export_stats import ExportStats
from .renumbering import RenumberingResult

logger = logging.getLogger(__name__)


@dataclass
class Cluster:
    """One live citation field in the body and the items it will carry."""
    field: ComplexField
    payload: CitePayload
    cid: str
    items: list[CiteItem] = field(default_factory=list)
    merge_into_previous: bool = False   # adjacent to the previous cluster (no text between)


def collect_clusters(handler: DocxHandler, stats: ExportStats):
    """Live citation fields of the body, in order (read-only).

    Returns (body clusters, table fields). A field whose payload cannot be
    read is counted as damaged (the guard refuses such documents). A field
    with nothing between it and the previous one (``(REF)(REF)``, or a
    paste next to a citation) is flagged for :func:`merge_adjacent_clusters`.
    """
    clusters: list[Cluster] = []
    table_fields: list[tuple[ComplexField, CitePayload]] = []
    seen: set[str] = set()
    for f in handler.fields.fields:
        if f.kind != 'airefs_cite' or f.depth != 0 or f.deleted:
            continue
        try:
            payload = parse_cite_code(f.code)
        except PayloadError:
            stats.damaged_fields += 1
            continue
        if f.out_of_flow:
            table_fields.append((f, payload))
            continue
        cid = payload.citation_id
        if cid in seen:
            cid = mint_citation_id()
        seen.add(cid)
        clusters.append(Cluster(field=f, payload=payload, cid=cid, items=list(payload.items),
                                merge_into_previous=bool(clusters) and _adjacent(clusters[-1].field, f)))
    return clusters, table_fields


def merge_adjacent_clusters(clusters: list[Cluster], stats: ExportStats) -> list[Cluster]:
    """Merge each flagged cluster into its predecessor: items appended, the
    second field's runs removed. Call after the renumbering plan was built,
    so the plan's offsets were read from the unmodified paragraph."""
    merged: list[Cluster] = []
    for cluster in clusters:
        if cluster.merge_into_previous and merged:
            merged[-1].items.extend(cluster.items)
            for r in cluster.field.all_runs:
                if r is not None and r.getparent() is not None:
                    r.getparent().remove(r)
            stats.duplicates_merged += 1
            continue
        merged.append(cluster)
    return merged


def _adjacent(prev: ComplexField, nxt: ComplexField) -> bool:
    """True when *nxt* begins right after *prev* ends, with no run between."""
    if prev.end is None or not prev.complete or not nxt.complete:
        return False
    return prev.end.getnext() is nxt.begin


def candidates_for(items: list[CiteItem], existing: ExistingCitationMap) -> list[CitationCandidate]:
    """The candidates behind a cluster's items: the map's record when the
    reader has it, else the item data embedded in the field."""
    by_uuid = {e.record_uuid: e for e in existing.bib_entries.values() if e.record_uuid}
    out = []
    for item in items:
        entry = by_uuid.get(item.record_uuid)
        if entry is not None and entry.matched_candidate is not None:
            out.append(entry.matched_candidate)
        else:
            cand = from_csl_item(item.item)
            cand.record_uuid = item.record_uuid
            out.append(cand)
    return out


def rewrite_clusters(clusters: list[Cluster], existing: ExistingCitationMap,
                     result: RenumberingResult, layout: CitationLayout, style: CitationStyle,
                     stats: ExportStats) -> int:
    """Give every cluster its new numbers, text and payload; rewrite only what
    changed. An unresolved [?] field into which the user typed a (REF) or
    (REFS) marker is unwrapped so the marker pass fills it like any other.
    Returns the number of clusters unwrapped."""
    from .existing_citation_parser import MARKER_PATTERN
    unwrapped = 0
    for cluster in clusters:
        if cluster.payload.unresolved and not cluster.items and MARKER_PATTERN.search(cluster.field.result_text):
            unwrap_field(cluster.field)
            unwrapped += 1
            continue
        cands = candidates_for(cluster.items, existing)
        numbers = [n for n in (result.number_for_candidate(c) for c in cands) if n is not None]
        visible = cluster.field.result_text
        user_edited = bool(cluster.payload.plain) and visible != cluster.payload.plain
        unresolved = not cands
        text = render_cluster(cands, numbers, layout)
        if layout.is_author_date and user_edited and not unresolved:
            text = visible                              # the user's wording wins
            stats.hand_edits_preserved += 1
        superscript = layout.is_superscript and not unresolved
        stored_numbers = [] if layout.is_author_date else numbers
        if stored_numbers != cluster.payload.numbers and not unresolved:
            stats.existing_refs_renumbered += 1
        if text != visible or cluster.payload.render != layout.render_kind:
            rewrite_result(cluster.field, text, superscript=superscript)
            if user_edited and text != visible:
                stats.hand_edits_overwritten += 1
        code = build_cite_code(cands, numbers=stored_numbers, render=layout.render_kind,
                               style=style.value, plain=text, citation_id=cluster.cid,
                               unresolved=unresolved)
        if code.strip() != cluster.field.code:
            rewrite_code(cluster.field, code)
    return unwrapped


def blocked_table_fields(table_fields, existing: ExistingCitationMap, result: RenumberingResult,
                         layout: CitationLayout) -> list[str]:
    """Reasons a table / text-box citation could not be left as it is."""
    reasons = []
    for f, payload in table_fields:
        cands = candidates_for(payload.items, existing)
        numbers = [n for n in (result.number_for_candidate(c) for c in cands) if n is not None]
        stored = [] if layout.is_author_date else numbers
        if stored != payload.numbers or payload.render != layout.render_kind:
            reasons.append(
                f"A citation inside a table or text box ('{f.result_text}') would have to change "
                f"to '{render_cluster(cands, numbers, layout)}'. AI REFs does not renumber "
                "citations in tables yet; move it into the body text or keep its order.")
    return reasons


def render_tracked_bibliography(existing: ExistingCitationMap, result: RenumberingResult,
                                style: CitationStyle, layout: CitationLayout,
                                verbatim_existing: bool = False):
    """Entries, record uuids (rendered order), kept-uncited uuids and hashes.

    Existing entries are re-rendered from their record unless they were
    hand-edited, came from a plain-text document (``raw_entry``), or
    *verbatim_existing* asks to keep every existing entry's wording."""
    import re
    rows = []          # (text, record_uuid, is_uncited)
    for num in sorted(result.assignments):
        a = result.assignments[num]
        if a.is_new:
            cand = a.candidate
            if cand is None:
                continue
            rows.append((format_bib_entry(cand, num, style), cand.record_uuid, False))
            continue
        entry: Optional[ExistingBibEntry] = existing.bib_entries.get(a.original_number)
        if entry is None:
            continue
        edited = bool(entry.raw_text and entry.entry_hash
                      and entry_hash(entry.raw_text) != entry.entry_hash)
        cand = entry.matched_candidate
        adopted_text = cand.raw_entry if cand is not None else ""
        if edited or cand is None or verbatim_existing or adopted_text:
            # Hand-edited, adopted from a plain-text document, or text-only:
            # the entry's own wording is kept, only the number changes.
            body = entry.body or entry.raw_text or adopted_text
            text = body if layout.is_author_date else f"{num}. {body}"
        else:
            text = format_bib_entry(cand, num, style)
        rows.append((text, entry.record_uuid, entry.is_uncited))
    if layout.is_author_date:
        rows.sort(key=lambda row: re.sub(r"[^a-z0-9 ]+", "", row[0].lower())[:80])
    entries = [t for t, _, _ in rows]
    order = [rid for _, rid, _ in rows]
    uncited = [rid for _, rid, is_uncited in rows if is_uncited]
    return entries, order, uncited, [entry_hash(t) for t in entries]


def orphan_heading(handler: DocxHandler):
    """The heading paragraph of a bibliography whose field was deleted: the
    last paragraph styled as our heading (callers check that no bibliography
    field exists in the document)."""
    paragraphs = handler.get_paragraphs()
    for p in reversed(paragraphs):
        style = p.style
        if style is not None and style.name == BIBLIOGRAPHY_HEADING_STYLE:
            return p
    return None
