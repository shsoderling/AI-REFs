"""JATS full-text handling: body paragraphs and keyword-ranked passages.

Europe PMC returns open-access articles as JATS XML.  ``parse_jats_body``
flattens the ``<body>`` into (section title, paragraph) pairs, skipping
tables, figures, formulas and the back matter; ``select_passages`` picks
the paragraphs that best match a few keywords so an agent or verifier can
read a bounded excerpt instead of the whole paper.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Iterable

SKIP_TAGS = {
    "ref-list", "table-wrap", "table", "fig", "fig-group", "supplementary-material",
    "disp-formula", "back", "ack", "fn-group", "glossary", "app-group", "boxed-text",
    "graphic", "media", "alternatives", "tex-math", "mml:math",
}
MIN_PARAGRAPH_CHARS = 40
PARAGRAPH_CAP = 1200
_EVIDENCE_SECTIONS = re.compile(r"result|discussion|conclusion|finding", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9]+")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_jats_body(xml_text: str) -> list[tuple[str, str]]:
    """Flatten a JATS article body into ``[(section_title, paragraph), ...]``.

    Section titles are the chain of enclosing ``<sec><title>`` texts joined
    with " > ".  Reference lists, tables, figures, formulas and back matter
    are skipped; paragraphs shorter than ``MIN_PARAGRAPH_CHARS`` are dropped.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    body = None
    for elem in root.iter():
        if _local(elem.tag) == "body":
            body = elem
            break
    if body is None:
        return []

    out: list[tuple[str, str]] = []

    def walk(elem, titles: list[str]):
        for child in list(elem):
            tag = _local(child.tag)
            if tag in SKIP_TAGS:
                continue
            if tag == "sec":
                title = ""
                for sub in list(child):
                    if _local(sub.tag) == "title":
                        title = _collapse("".join(sub.itertext()))
                        break
                walk(child, titles + ([title] if title else []))
            elif tag == "p":
                text = _collapse("".join(child.itertext()))
                if len(text) >= MIN_PARAGRAPH_CHARS:
                    out.append((" > ".join(titles), text))
            elif tag == "title":
                continue
            else:
                walk(child, titles)

    walk(body, [])
    return out


def _stems(words: Iterable[str]) -> set[str]:
    stems = set()
    for w in words:
        for token in _WORD.findall(w.lower()):
            if len(token) >= 3:
                stems.add(token[:5])
    return stems


def select_passages(paragraphs: list[tuple[str, str]], keywords: list[str],
                    max_passages: int = 8, max_chars: int = 6000) -> list[dict]:
    """Rank paragraphs by distinct keyword hits and return a bounded excerpt.

    Each result is ``{"section", "text", "score"}``.  A paragraph in a
    results/discussion section gets a one-point bonus; ties keep document
    order.  Paragraphs are capped at ``PARAGRAPH_CAP`` characters and the
    total at ``max_chars``.  When nothing matches, the first paragraphs in
    document order are returned so the caller still sees something.
    """
    if not paragraphs:
        return []
    wanted = _stems(keywords)

    scored = []
    for index, (section, text) in enumerate(paragraphs):
        para_stems = _stems(text.split())
        hits = len(wanted & para_stems) if wanted else 0
        bonus = 1 if (hits and _EVIDENCE_SECTIONS.search(section or "")) else 0
        scored.append((hits + bonus, index, section, text))

    if not any(s[0] for s in scored):
        ordered = scored
    else:
        ordered = sorted(scored, key=lambda s: (-s[0], s[1]))

    chosen = []
    total = 0
    for score, index, section, text in ordered:
        if len(chosen) >= max_passages:
            break
        snippet = text[:PARAGRAPH_CAP].rstrip()
        if len(text) > PARAGRAPH_CAP:
            snippet += "…"
        if total + len(snippet) > max_chars:
            continue
        total += len(snippet)
        chosen.append({"section": section, "text": snippet, "score": score, "index": index})

    chosen.sort(key=lambda p: p["index"])   # present in reading order
    for p in chosen:
        p.pop("index", None)
    return chosen
