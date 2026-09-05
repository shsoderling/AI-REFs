"""Expand and collapse numeric citation lists: "1, 3-5" <-> [1, 3, 4, 5].

Deliberately import-free so the existing-citation parser and the renumbering
writer can share one definition without a circular import.
"""

import re


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
