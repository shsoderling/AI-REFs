"""Apply a computed renumbering to a document's existing in-text citations.

Headless (no Qt) so it can be exercised by tests and reused by previews.
"""

import logging

from ..models.existing_refs import ExistingCitationMap
from .citation_numbers import expand_bracket_numbers, format_bracket_numbers  # noqa: F401  (re-exported)
from .existing_citation_parser import (
    BRACKET_CITE_PATTERN, CITATION_SHAPE_PATTERN, in_field_result,
)

logger = logging.getLogger(__name__)


def renumber_bracket_group(group_text: str, renumber_map: dict[int, int]) -> str:
    """Renumber and normalize a bracket citation group like '1, 3-5'."""
    numbers = expand_bracket_numbers(group_text)
    if not numbers:
        return group_text
    mapped = [renumber_map.get(n, n) for n in numbers]

    # De-duplicate while preserving first occurrence order.
    seen = set()
    ordered = []
    for n in mapped:
        if n not in seen:
            seen.add(n)
            ordered.append(n)

    return format_bracket_numbers(ordered)


def apply_renumbering(handler, existing: ExistingCitationMap,
                      renumber_map: dict[int, int]):
    """Update all existing in-text citation numbers using the renumber_map.

    Walks superscript runs in body paragraphs (before the References heading)
    and rewrites each citation-shaped run as its mapped, re-collapsed group
    (a range "3-5" expands to 3, 4, 5 first, so its middle number is mapped
    too), then renumbers bracketed citation groups.

    Runs that belong to a Word field and bracket groups inside a field's
    cached result are left alone: a field result is rewritten through the
    field API, never as plain text.

    Each superscript run is rewritten in one pass, so collisions cannot
    cascade (e.g. 7->10 then a later run containing 10 being re-mapped).
    Bracket groups are replaced in two phases via unique sentinels for the
    same reason: replacing [1]->[2] directly would let a later [2]->[3]
    replacement match the freshly inserted token.
    """
    refs_start = existing.references_heading_para_idx
    cite_runs = handler.find_superscript_citation_runs()    # skips in-field runs

    # Filter to only body paragraphs
    body_runs = [r for r in cite_runs if r['para_index'] < refs_start]

    for run_info in body_runs:
        run = run_info['run']
        text = run.text
        # Only touch runs that are purely citation-shaped — protects
        # superscripts like the "2+" in Ca2+ from being renumbered.
        if not CITATION_SHAPE_PATTERN.match(text or ""):
            continue
        new_text = renumber_bracket_group(text, renumber_map)
        # format_bracket_numbers separates with ", "; keep the run's own
        # tight "1,2" shape unless it already used ", ".
        if ", " not in text:
            new_text = new_text.replace(", ", ",")
        if new_text != text:
            run.text = new_text

    if body_runs:
        logger.info(f"Renumbered superscript citations in {len(body_runs)} runs")

    bracket_updates = 0
    for para_idx, para in enumerate(handler.get_paragraphs()):
        if para_idx >= refs_start:
            break

        para_text = para.text
        if "[" not in para_text:
            continue

        result_spans = handler.fields.result_spans(para)
        replacements: list[tuple[str, str, str]] = []
        for i, match in enumerate(BRACKET_CITE_PATTERN.finditer(para_text)):
            if in_field_result(match, result_spans):
                continue
            old_token = match.group(0)
            inner = match.group(1)
            new_inner = renumber_bracket_group(inner, renumber_map)
            new_token = f"[{new_inner}]"
            if new_token != old_token:
                # Private-use chars guarantee the sentinel never collides
                # with document text or the bracket pattern.
                sentinel = f"{i}"
                replacements.append((old_token, sentinel, new_token))

        # superscript=None: rewrite the token in place, keeping the run's
        # vertical alignment (superscript-bracket styles write a superscript
        # "[3]", which must stay superscript after renumbering).
        for old_token, sentinel, _ in replacements:
            handler.replace_marker_by_regex(para, old_token, sentinel,
                                            superscript=None)
        for _, sentinel, new_token in replacements:
            handler.replace_marker_by_regex(para, sentinel, new_token,
                                            superscript=None)
            bracket_updates += 1

    if bracket_updates:
        logger.info(f"Renumbered bracket citations in {bracket_updates} locations")
