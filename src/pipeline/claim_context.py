"""What the search agent and the verifier are told about a claim.

A bare sentence is often not enough to search well ("These mice also
showed the deficit" needs the sentences before it).  ``build_claim_context``
gathers the section heading, the preceding sentences, the rest of the
paragraph and the next sentence from the parsed document, and
``ClaimContext`` renders them so the model knows which sentence to cite
and which text is context only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Optional

from ..models.sentence import SentenceRecord

_MARKER_SPLIT = re.compile(r"\(REFS?\)")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}")
_STOPWORDS = {
    "the", "and", "for", "that", "with", "this", "these", "those", "from", "have", "has",
    "had", "are", "was", "were", "been", "being", "which", "their", "there", "than",
    "then", "into", "also", "such", "both", "each", "more", "most", "other", "some",
    "our", "its", "not", "but", "can", "may", "via", "upon", "during", "after", "before",
    "between", "within", "without", "using", "used", "shown", "show", "showed", "found",
    "however", "although", "whereas", "while", "where", "when", "they", "them", "here",
    "well", "known", "previous", "previously", "recent", "recently", "study", "studies",
    "results", "result", "data", "role", "roles", "effect", "effects", "level", "levels",
    "increase", "increased", "decrease", "decreased", "including", "important",
}
CONTEXT_HEADER = ("CONTEXT — for understanding the claim only. Do NOT search for or cite "
                  "references for these sentences:")


@dataclass
class ClaimContext:
    claim: str
    section: str = ""
    preceding: list[str] = field(default_factory=list)   # oldest first
    following: list[str] = field(default_factory=list)
    paragraph: str = ""                                    # claim shown inside [[ ]]
    sub_claim: str = ""                                    # per-marker: text before this (REF)
    trailing: str = ""                                     # per-marker: text after it
    exclusions: list[str] = field(default_factory=list)   # identifiers already assigned

    # ── derived ──────────────────────────────────────────────────────

    @property
    def has_context(self) -> bool:
        return bool(self.section or self.preceding or self.following or self.paragraph)

    def keywords(self, limit: int = 8) -> list[str]:
        """Content words of the claim (sub-claim first), for passage selection."""
        seen: list[str] = []
        for source in (self.sub_claim, self.claim):
            for word in _WORD.findall(source or ""):
                low = word.lower()
                if low in _STOPWORDS or low in seen:
                    continue
                seen.append(low)
                if len(seen) >= limit:
                    return seen
        return seen

    def with_marker(self, sub_claim: str, trailing: str, exclusions: list[str]) -> "ClaimContext":
        return replace(self, sub_claim=sub_claim, trailing=trailing, exclusions=list(exclusions))

    # ── rendering ────────────────────────────────────────────────────

    def to_context_block(self) -> str:
        """The context lines shared by the agent, the verifier and the chat."""
        if not self.has_context:
            return ""
        lines = [CONTEXT_HEADER]
        if self.section:
            lines.append(f"Section: {self.section}")
        if self.preceding:
            lines.append("Preceding: " + " ".join(f"\"{s}\"" for s in self.preceding))
        if self.paragraph:
            lines.append(f"Paragraph: {self.paragraph}")
        if self.following:
            lines.append("Following: " + " ".join(f"\"{s}\"" for s in self.following))
        return "\n".join(lines)

    def to_claim_block(self, num_refs: int = 1) -> str:
        count = "1 reference" if num_refs == 1 else f"{num_refs} references"
        lines = [f"CLAIM — find {count} that support this sentence:", f"\"{self.claim}\""]
        if self.sub_claim:
            line = f"Specifically the part ending at this (REF) marker: \"{self.sub_claim}\""
            if self.trailing:
                line += f" (followed by: \"{self.trailing[:80]}\")"
            lines.append(line)
        return "\n".join(lines)

    def to_agent_message(self, num_refs: int = 1, extras: str = "") -> str:
        blocks = [self.to_claim_block(num_refs)]
        context = self.to_context_block()
        if context:
            blocks.append(context)
        if extras:
            blocks.append(extras)
        if self.exclusions:
            blocks.append(
                "IMPORTANT: The following identifiers have already been assigned to "
                "other (REF) markers in this sentence. You MUST find a DIFFERENT "
                "paper — do NOT select any of these: " + ", ".join(sorted(self.exclusions))
            )
        return "\n\n".join(blocks)


# ── builders ─────────────────────────────────────────────────────────

def sub_claims_for(sentence: SentenceRecord) -> list[tuple[str, str]]:
    """Per marker: (text before the marker, text after it), from the raw text.

    Uses the marker spans recorded by the locator when they are present and
    consistent with the raw text (they cover author-suggested citations such
    as ``(Smith et al. 2020)`` too); otherwise splits at ``(REF)``/``(REFS)``.
    """
    raw = sentence.raw_text or ""
    spans = _marker_spans(sentence, raw)
    if spans:
        out = []
        for i, (start, end) in enumerate(spans):
            prev_end = spans[i - 1][1] if i > 0 else 0
            next_start = spans[i + 1][0] if i + 1 < len(spans) else len(raw)
            out.append((raw[prev_end:start].strip(), raw[end:next_start].strip()))
        return out
    parts = _MARKER_SPLIT.split(raw)
    out = []
    for i in range(max(sentence.marker_count, 0)):
        before = parts[i].strip() if i < len(parts) else ""
        after = parts[i + 1].strip() if i + 1 < len(parts) else ""
        out.append((before, after))
    return out


def _marker_spans(sentence: SentenceRecord, raw: str) -> list[tuple[int, int]]:
    """(start, end) of every marker, or [] when the spans cannot be trusted."""
    spans: list[tuple[int, int]] = []
    last_end = 0
    for m in sentence.markers or []:
        if not (last_end <= m.start < m.end <= len(raw)):
            return []
        if m.text and raw[m.start:m.end] != m.text:
            return []
        spans.append((m.start, m.end))
        last_end = m.end
    return spans


def _window(text: str, anchor: int, anchor_len: int, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    half = max(0, (max_chars - anchor_len) // 2)
    start = max(0, anchor - half)
    end = min(len(text), start + max_chars)
    start = max(0, end - max_chars)
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet.lstrip()
    if end < len(text):
        snippet = snippet.rstrip() + "…"
    return snippet


def build_claim_context(sentences: list[SentenceRecord], sentence: SentenceRecord, *,
                        before: int = 2, after: int = 1,
                        max_paragraph_chars: int = 1200) -> ClaimContext:
    """Context for *sentence* from the document-ordered sentence list.

    Preceding sentences come from the same section (up to ``before``), the
    following sentence only from the same paragraph, and the paragraph is
    the claim's siblings with the claim itself inside [[ ]], windowed to
    ``max_paragraph_chars`` around the claim.
    """
    ctx = ClaimContext(claim=sentence.clean_text, section=sentence.section or "")
    try:
        pos = next(i for i, s in enumerate(sentences)
                   if s is sentence or (s.id and s.id == sentence.id))
    except StopIteration:
        return ctx

    def usable(s: SentenceRecord) -> bool:
        return bool(s.clean_text and s.clean_text.strip())

    preceding = []
    for s in reversed(sentences[:pos]):
        if len(preceding) >= before:
            break
        if (s.section or "") != (sentence.section or ""):
            break
        if usable(s):
            preceding.insert(0, s.clean_text.strip())
    ctx.preceding = preceding

    following = []
    for s in sentences[pos + 1:]:
        if len(following) >= after or s.paragraph_index != sentence.paragraph_index:
            break
        if usable(s):
            following.append(s.clean_text.strip())
    ctx.following = following

    siblings = [s for s in sentences if s.paragraph_index == sentence.paragraph_index and usable(s)]
    if len(siblings) > 1:
        pieces = []
        anchor = 0
        for s in siblings:
            text = s.clean_text.strip()
            if s is sentence or (s.id and s.id == sentence.id):
                anchor = len(" ".join(pieces)) + (1 if pieces else 0)
                text = f"[[{text}]]"
            pieces.append(text)
        joined = " ".join(pieces)
        ctx.paragraph = _window(joined, anchor, len(sentence.clean_text) + 4, max_paragraph_chars)
    return ctx


def bare_context(sentence: SentenceRecord) -> ClaimContext:
    """A context holding only the claim (used when no document is available)."""
    return ClaimContext(claim=sentence.clean_text, section=sentence.section or "")


def find_sentence(sentences: list[SentenceRecord], sentence_id: str) -> Optional[SentenceRecord]:
    return next((s for s in sentences if s.id == sentence_id), None)
