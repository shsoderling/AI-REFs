"""Apply a computed renumbering to a document's existing in-text citations.

Headless (no Qt) so it can be exercised by tests and reused by previews.
"""

import re
import logging

from ..models.existing_refs import ExistingCitationMap
from .existing_citation_parser import BRACKET_CITE_PATTERN, CITATION_SHAPE_PATTERN

logger = logging.getLogger(__name__)


def expand_bracket_numbers(group_text: str) -> list[int]:
    """Expand citation list/range text into explicit numbers."""
    numbers: list[int] = []
    for part in re.split(r'[;,]\s*', group_text):
        token = part.strip()
        if not token:
            continue

        bounds = re.split(r'\s*[-–]\s*', token)
        if len(bounds) == 2 and bounds[0].isdigit() and bounds[1].isdigit():
            start = int(bounds[0])
            end = int(bounds[1])
            if start <= end:
                numbers.extend(range(start, end + 1))
            else:
                numbers.extend(range(start, end - 1, -1))
        elif token.isdigit():
            numbers.append(int(token))

    return numbers


def format_bracket_numbers(numbers: list[int]) -> str:
    """Format a list of citation numbers as compact ranges."""
    if not numbers:
        return ""

    chunks = []
    start = prev = numbers[0]
    for n in numbers[1:]:
        if n == prev + 1:
            prev = n
            continue

        if start == prev:
            chunks.append(str(start))
        elif prev - start >= 2:
            chunks.append(f"{start}-{prev}")
        else:
            chunks.extend([str(start), str(prev)])
        start = prev = n

    if start == prev:
        chunks.append(str(start))
    elif prev - start >= 2:
        chunks.append(f"{start}-{prev}")
    else:
        chunks.extend([str(start), str(prev)])

    return ", ".join(chunks)


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
    and replaces each citation number according to the map, then renumbers
    bracketed citation groups.

    Superscript runs use a single-pass re.sub with a callback to avoid
    cascading collisions (e.g. 7->10 then a later run containing 10 being
    re-mapped).  Bracket groups are replaced in two phases via unique
    sentinels for the same reason: replacing [1]->[2] directly would let a
    later [2]->[3] replacement match the freshly inserted token.
    """
    refs_start = existing.references_heading_para_idx
    cite_runs = handler.find_superscript_citation_runs()

    # Filter to only body paragraphs
    body_runs = [r for r in cite_runs if r['para_index'] < refs_start]

    def _replace_num(m):
        old_num = int(m.group())
        return str(renumber_map.get(old_num, old_num))

    for run_info in body_runs:
        run = run_info['run']
        text = run.text
        # Only touch runs that are purely citation-shaped — protects
        # superscripts like the "2+" in Ca2+ from being renumbered.
        if not CITATION_SHAPE_PATTERN.match(text or ""):
            continue
        new_text = re.sub(r'\d+', _replace_num, text)
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

        replacements: list[tuple[str, str, str]] = []
        for i, match in enumerate(BRACKET_CITE_PATTERN.finditer(para_text)):
            old_token = match.group(0)
            inner = match.group(1)
            new_inner = renumber_bracket_group(inner, renumber_map)
            new_token = f"[{new_inner}]"
            if new_token != old_token:
                # Private-use chars guarantee the sentinel never collides
                # with document text or the bracket pattern.
                sentinel = f"{i}"
                replacements.append((old_token, sentinel, new_token))

        for old_token, sentinel, _ in replacements:
            handler.replace_marker_by_regex(para, old_token, sentinel,
                                            superscript=False)
        for _, sentinel, new_token in replacements:
            handler.replace_marker_by_regex(para, sentinel, new_token,
                                            superscript=False)
            bracket_updates += 1

    if bracket_updates:
        logger.info(f"Renumbered bracket citations in {bracket_updates} locations")
