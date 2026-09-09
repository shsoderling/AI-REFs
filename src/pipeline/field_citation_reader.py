"""Read a tracked document: its AIREFS.CITE fields become the citation map.

The citation fields, walked in document order, are the single source of
truth: record identity and data come from their payloads, bibliography
membership and numbering from first appearance. The AIREFS.BIBL field is
a cache (rendered entry texts, hashes, kept-uncited records); when it is
missing the bibliography is simply regenerated at the next export. Nothing
here touches the network.
"""

import logging
from typing import Optional

from ..models.citation import CitationCandidate
from ..models.embedded import DocumentTier, ReconcileIssue, TrackingReport
from ..models.existing_refs import ExistingBibEntry, ExistingCitationMap, InTextCitation
from ..services.docx_fields import ComplexField
from ..services.docx_io import DocxHandler, iter_text_runs, run_text
from .citation_payload import (
    BiblPayload, CitePayload, NewerSchemaError, PayloadError, entry_hash,
    mint_citation_id, parse_bibl_code, parse_cite_code,
)
from .csl_mapping import from_csl_item

logger = logging.getLogger(__name__)


def _strip_number(text: str) -> str:
    from .existing_citation_parser import BIB_ENTRY_PATTERN
    m = BIB_ENTRY_PATTERN.match(text)
    return m.group(2).strip() if m else text


def _result_offset(paragraph, field: ComplexField) -> int:
    """Char offset in ``paragraph.text`` where the field's result starts."""
    targets = {id(r) for r in field.result_runs}
    if not targets and field.separate is not None:
        targets = {id(field.separate)}
    offset = 0
    for r in iter_text_runs(paragraph):
        if id(r) in targets:
            return offset
        offset += len(run_text(r))
    return offset


def _entry_from_text(number: int, text: str, record_uuid: str, hash_: str) -> ExistingBibEntry:
    """A bibliography entry known only by its rendered text (kept uncited)."""
    from .existing_citation_parser import ExistingCitationParser
    entry = ExistingBibEntry(original_number=number, raw_text=text, body=_strip_number(text),
                             record_uuid=record_uuid, is_uncited=True, entry_hash=hash_)
    ExistingCitationParser._extract_bib_fields(entry, entry.body)
    return entry


def build_tracked_map(handler: DocxHandler, *, keep_uncited: bool = False) -> ExistingCitationMap:
    """The citation map of a document that carries AIREFS.CITE fields."""
    idx = handler.fields
    report = TrackingReport(tier=DocumentTier.TRACKED)
    result = ExistingCitationMap(tracking=report)
    report.field_count = idx.airefs_cite
    report.bibl_field_count = idx.airefs_bibl
    report.foreign_field_count = idx.foreign
    report.fields_in_tables = idx.out_of_flow
    result.pending_tracked_changes = idx.pending_tracked_changes

    paragraphs = handler.get_paragraphs()
    para_index = {id(p._p): i for i, p in enumerate(paragraphs)}

    # ── 1. parse every live citation field ─────────────────────────
    parsed: list[tuple[ComplexField, CitePayload]] = []
    seen_cids: set[str] = set()
    for f in idx.fields:
        if f.kind != 'airefs_cite' or f.depth != 0 or f.deleted:
            continue
        try:
            payload = parse_cite_code(f.code)
        except NewerSchemaError as exc:
            report.tier = DocumentTier.NEWER_VERSION
            report.problems.append(str(exc))
            return ExistingCitationMap(tracking=report)
        except PayloadError as exc:
            report.reconcile.append(ReconcileIssue(
                kind='damaged', message=f"a citation field could not be read ({exc}); skipped"))
            continue
        report.schema_version = max(report.schema_version, payload.version)
        for problem in payload.problems:
            report.reconcile.append(ReconcileIssue(kind='payload', cid=payload.citation_id,
                                                   message=problem))
        if not f.out_of_flow:
            if payload.citation_id in seen_cids:            # copy/paste duplicated the cluster
                payload.citation_id = mint_citation_id()
                report.reconcile.append(ReconcileIssue(
                    kind='duplicate', cid=payload.citation_id,
                    message="a citation was pasted; the copy received a new id"))
            seen_cids.add(payload.citation_id)
        parsed.append((f, payload))

    # ── 2. records in first-appearance order over body fields ──────
    records: dict[str, tuple[dict, list[str]]] = {}
    order: list[str] = []
    body_fields: list[tuple[int, int, ComplexField, CitePayload]] = []
    for f, payload in parsed:
        pi = para_index.get(id(f.paragraphs[0])) if f.paragraphs else None
        if f.out_of_flow or pi is None:
            report.reconcile.append(ReconcileIssue(
                kind='table', cid=payload.citation_id,
                message="a citation inside a table or text box is not renumbered"))
            continue
        for item in payload.items:
            if item.record_uuid not in records:
                records[item.record_uuid] = (item.item, item.uris)
                order.append(item.record_uuid)
        body_fields.append((pi, _result_offset(paragraphs[pi], f), f, payload))
    number_of = {rid: n for n, rid in enumerate(order, start=1)}

    # ── 3. the bibliography field (a cache) ────────────────────────
    bibl = next((f for f in idx.fields if f.kind == 'airefs_bibl' and f.depth == 0 and not f.deleted), None)
    bibl_payload: Optional[BiblPayload] = None
    entry_text: dict[str, tuple[str, str]] = {}        # record uuid -> (rendered text, stored hash)
    if bibl is None:
        report.problems.append(
            "The bibliography field is missing (deleted in Word?); it will be regenerated "
            "from the citation fields at the next export.")
    else:
        try:
            bibl_payload = parse_bibl_code(bibl.code)
        except NewerSchemaError as exc:
            report.tier = DocumentTier.NEWER_VERSION
            report.problems.append(str(exc))
            return ExistingCitationMap(tracking=report)
        except PayloadError as exc:
            report.problems.append(
                f"The bibliography field could not be read ({exc}); it will be regenerated.")
        first = para_index.get(id(bibl.paragraphs[0]), -1) if bibl.paragraphs else -1
        last = para_index.get(id(bibl.paragraphs[-1]), -1) if bibl.paragraphs else -1
        result.bibliography_span = (first, last)
        if bibl_payload is not None:
            report.doc_id = bibl_payload.doc_id
            heading = handler.locate_bibliography_heading(bibl, bibl_payload.heading_text)
            result.references_heading_para_idx = heading
            result.heading_para_idx_found = heading >= 0
            texts = ([paragraphs[i].text.strip() for i in range(first, last + 1)]
                     if 0 <= first <= last else [])
            if texts and len(texts) == len(bibl_payload.order):
                hashes = bibl_payload.entry_hashes or [""] * len(texts)
                for rid, text, h in zip(bibl_payload.order, texts, hashes):
                    entry_text[rid] = (text, h)
            elif texts:
                report.reconcile.append(ReconcileIssue(
                    kind='entry_mismatch',
                    message=f"the bibliography has {len(texts)} entries but the field lists "
                            f"{len(bibl_payload.order)}; entry texts are regenerated"))
            for problem in bibl_payload.problems:
                report.problems.append(problem)

    # ── 4. bibliography entries from the records ───────────────────
    for rid in order:
        n = number_of[rid]
        item, uris = records[rid]
        cand = from_csl_item(item)
        cand.record_uuid = rid
        text, stored_hash = entry_text.get(rid, ("", ""))
        if text and stored_hash and entry_hash(text) != stored_hash:
            report.reconcile.append(ReconcileIssue(
                kind='entry_edited', message=f"bibliography entry {n} was edited by hand; "
                                            "it is kept verbatim until regenerated"))
        result.bib_entries[n] = ExistingBibEntry(
            original_number=n, raw_text=text, body=_strip_number(text) if text else "",
            title=cand.title, authors_str=", ".join(a.display for a in cand.authors),
            year=cand.year, journal=cand.journal_abbrev or cand.journal, doi=cand.doi,
            pmid=cand.pmid, matched_candidate=cand, record_uuid=rid, item=item, uris=list(uris),
            entry_hash=stored_hash)

    # ── 5. records the bibliography lists but nothing cites ────────
    if bibl_payload is not None:
        listed = list(bibl_payload.order) + [u for u in bibl_payload.uncited if u not in bibl_payload.order]
        for rid in listed:
            if rid in records:
                continue
            text, stored_hash = entry_text.get(rid, ("", ""))
            if keep_uncited or rid in bibl_payload.uncited:
                n = len(result.bib_entries) + 1
                result.bib_entries[n] = _entry_from_text(n, text, rid, stored_hash)
            else:
                report.reconcile.append(ReconcileIssue(
                    kind='uncited', message=f"reference '{(text or rid)[:60]}' has no citation "
                                            "left; it will be dropped from the bibliography"))

    # ── 6. in-text citations from the fields ───────────────────────
    for pi, offset, f, payload in body_fields:
        visible = f.result_text
        user_edited = bool(payload.plain) and visible != payload.plain
        if user_edited:
            report.reconcile.append(ReconcileIssue(
                kind='hand_edited', cid=payload.citation_id,
                message=f"citation text '{visible}' differs from the generated '{payload.plain}'"))
        is_superscript = payload.render == 'numeric-superscript'
        cites = result.in_text_citations.setdefault(pi, [])
        if not payload.items:
            report.reconcile.append(ReconcileIssue(
                kind='unresolved', cid=payload.citation_id,
                message="an unresolved [?] citation is still waiting for a reference"))
            cites.append(InTextCitation(char_offset=offset, number=0, is_superscript=is_superscript,
                                        cid=payload.citation_id, source='field',
                                        user_edited=user_edited, unresolved=True))
            continue
        for k, item in enumerate(payload.items):
            cites.append(InTextCitation(
                char_offset=offset, number=number_of[item.record_uuid], is_superscript=is_superscript,
                cid=payload.citation_id, record_uuid=item.record_uuid, cluster_index=k,
                source='field', user_edited=user_edited))
    for cites in result.in_text_citations.values():
        cites.sort(key=lambda c: (c.char_offset, c.cluster_index))

    result.max_existing_number = len(result.bib_entries)
    first_render = next((p.render for _, p in parsed if p.render), "")
    result.detected_style_is_superscript = first_render == 'numeric-superscript'
    result.detected_style_is_author_date = first_render == 'author-date'
    report.record_count = len(records)
    logger.info(f"Tracked document: {report.field_count} citation fields, "
                f"{report.record_count} records, {len(report.reconcile)} reconcile issue(s)")
    return result
