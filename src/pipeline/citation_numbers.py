"""Expand and collapse numeric citation lists: "1, 3-5" <-> [1, 3, 4, 5].

Imports nothing from the project, so the existing-citation parser and the
renumbering writer can share one definition without a circular import.
"""

import re


def expand_bracket_numbers(group_text: str, *, lenient: bool = False) -> list[int]:
    """Expand citation list/range text into explicit numbers.

    A token that is neither a number nor a well-formed range is dropped:
    a bracket group matched by ``BRACKET_CITE_PATTERN`` never holds one.
    With ``lenient`` such a token contributes the numbers written in it
    instead ("3-" -> [3], "1, 3-" -> [1, 3]). Word splits a superscript run
    at revision (rsid) boundaries, so a run may hold only a piece of its
    list, and no citation may go unreported because of that.
    """
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
        elif lenient:
            numbers.extend(int(n) for n in re.findall(r'\d+', token))

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
