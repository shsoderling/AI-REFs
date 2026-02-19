"""Stage 1: Parse a DOCX document into sentence records."""

import re
import logging
from ..models.sentence import SentenceRecord
from ..services.docx_io import DocxHandler

logger = logging.getLogger(__name__)

# ── Sentence-boundary detection ──────────────────────────────────────────
# Abbreviations common in scientific writing that should NOT trigger a
# sentence split when followed by a space.  Sorted roughly by frequency.
_ABBREVIATIONS = [
    # Latin
    "et al", "e.g", "i.e", "vs", "cf", "etc", "viz",
    # Titles / honorifics
    "Dr", "Prof", "Mr", "Mrs", "Ms", "Jr", "Sr", "St",
    # Scientific figures / tables / equations
    "Fig", "Figs", "Eq", "Eqs", "Tab", "Suppl", "Supp",
    "Ref", "Refs", "Sect", "Sec", "Ch", "Vol", "No", "Nos",
    "Pt",
    # Units / measures
    "approx", "ca", "dept", "est", "max", "min",
    # Journals / organizations (common abbreviated forms)
    "Natl", "Acad", "Sci", "Proc", "Biol", "Chem", "Phys",
    "Med", "Soc", "Assoc", "Univ", "Int", "Am", "J",
    # Geographic
    "U.S.A", "U.S", "U.K",
]

# Build the sentence-split regex.  The idea: find positions that look like
# sentence boundaries (period/!/? followed by whitespace) but REJECT
# positions preceded by known abbreviations, single-letter initials, or
# decimal numbers.
#
# We assemble a single compiled regex with alternations so the decision is
# made in one pass — no greedy sequential merging that can swallow later
# sentence boundaries.

# Escape dots in abbreviations for regex, require a trailing literal dot
_abbrev_patterns = []
for a in _ABBREVIATIONS:
    escaped = re.escape(a.rstrip("."))
    _abbrev_patterns.append(escaped)

# Sort longest-first so "et al" matches before "al"
_abbrev_patterns.sort(key=len, reverse=True)

# Negative lookbehind components — we must use a split approach because
# Python re doesn't support variable-length lookbehinds.  Instead, we
# use re.finditer to locate TRUE sentence boundaries and slice manually.

_BOUNDARY_RE = re.compile(r'[.!?]\s+')


def _is_false_boundary(text: str, dot_pos: int, after_pos: int) -> bool:
    """Return True if the boundary at *dot_pos* is NOT a real sentence end.

    Args:
        text:      full paragraph text
        dot_pos:   index of the `.` / `!` / `?` character
        after_pos: index of the first non-space character after the boundary
    """
    ch = text[dot_pos]

    # `!` and `?` almost always end a sentence
    if ch != ".":
        return False

    # ── Get the token ending at dot_pos ──
    # Walk backwards from dot_pos to find the start of the token
    tok_end = dot_pos  # points at the dot itself
    tok_start = tok_end
    while tok_start > 0 and not text[tok_start - 1].isspace():
        tok_start -= 1
    token = text[tok_start:tok_end]  # e.g. "al" or "2.5" or "(e.g"

    # Strip leading punctuation like opening parens: "(e.g" → "e.g"
    token_clean = token.lstrip("([\"'")

    if not token_clean:
        return False

    # ── Rule B: decimal number (e.g. "2.5") ──
    if re.fullmatch(r'\d+\.\d+', token_clean):
        return True

    # ── Rule C: single uppercase letter as person's initial ("A. B. Smith") ──
    # Only suppress the split when followed by another single uppercase letter
    # (another initial like "B."), since that pattern is unambiguously initials.
    # We deliberately do NOT suppress "Y. The..." or "Z. Smith..." because
    # single-letter variables at end-of-sentence are common in scientific text.
    if len(token_clean) == 1 and token_clean.isupper() and after_pos < len(text):
        next_word_m = re.match(r'([A-Za-z])', text[after_pos:])
        if next_word_m:
            # Next char is a single letter followed by a period → another initial
            remaining = text[after_pos:]
            if len(remaining) >= 2 and remaining[1] == "." and remaining[0].isupper():
                return True

    # ── Rule D: dotted initials like "U.S." or "A.B.C." ──
    # Require at least one internal dot (e.g. "U.S", "A.B") so a standalone
    # single letter isn't mistaken for an initial.
    if "." in token_clean and re.fullmatch(r'([A-Z]\.)+[A-Z]', token_clean):
        return True

    # ── Rule E: known abbreviation ──
    # Normalise: lowercase, strip internal dots
    normalised = token_clean.replace(".", "").lower()
    # Also check a two-word window for multi-word abbreviations like "et al"
    word_start = tok_start
    if word_start > 0:
        ws = word_start - 1
        while ws > 0 and text[ws - 1] == " ":
            ws -= 1
        while ws > 0 and not text[ws - 1].isspace():
            ws -= 1
        two_word = text[ws:tok_end].replace(".", "").lower().strip()
        two_word = two_word.lstrip("([\"'")
    else:
        two_word = normalised

    is_abbreviation = False
    for abbr in _ABBREVIATIONS:
        abbr_norm = abbr.replace(".", "").lower()
        if normalised == abbr_norm or two_word == abbr_norm:
            is_abbreviation = True
            break

    if is_abbreviation:
        return True

    # ── Rule A: next word starts lowercase → likely mid-sentence ──
    # Applied LAST, only when the token is NOT a known abbreviation/initial/
    # decimal, so that "et al. found" is handled by Rule E above and we don't
    # accidentally swallow later real boundaries.
    if after_pos < len(text) and text[after_pos].islower():
        return True

    return False


def _split_sentences(text: str) -> list[str]:
    """Split *text* into sentences, respecting scientific abbreviations.

    Uses a single-pass approach: find all candidate boundaries with a regex,
    filter out false positives (abbreviations, initials, decimals), then
    slice the text at the true boundaries.
    """
    boundaries: list[int] = []  # indices where we should split (start of whitespace)

    for m in _BOUNDARY_RE.finditer(text):
        dot_pos = m.start()       # position of . / ! / ?
        after_pos = m.end()       # first char after the whitespace

        if not _is_false_boundary(text, dot_pos, after_pos):
            boundaries.append(after_pos)

    # Slice text at each boundary
    sentences: list[str] = []
    prev = 0
    for b in boundaries:
        chunk = text[prev:b].strip()
        if chunk:
            sentences.append(chunk)
        prev = b
    # Last segment
    tail = text[prev:].strip()
    if tail:
        sentences.append(tail)

    return sentences if sentences else [text]


class DocumentParser:
    """Parse a DOCX document into a list of SentenceRecords."""

    def __init__(self, handler: DocxHandler, stop_at_para: int = -1):
        """
        Args:
            handler: DocxHandler for the source document
            stop_at_para: If >= 0, skip paragraphs at or beyond this index.
                          Used in insert mode to exclude the existing References section.
        """
        self.handler = handler
        self.stop_at_para = stop_at_para

    def parse(self) -> list[SentenceRecord]:
        """Parse the document into sentences."""
        sentences = []
        current_section = ""
        sentence_counter = 0

        for para_idx, para in enumerate(self.handler.get_paragraphs()):
            # In insert mode, stop before the existing References section
            if self.stop_at_para >= 0 and para_idx >= self.stop_at_para:
                break
            text = para.text.strip()
            if not text:
                continue

            # Detect section headings (heuristic: short, no period, possibly bold)
            if len(text) < 80 and not text.endswith('.') and para.style.name.startswith('Heading'):
                current_section = text
                continue

            # Split paragraph into sentences
            raw_sentences = _split_sentences(text)
            for sent_idx, raw in enumerate(raw_sentences):
                raw = raw.strip()
                if not raw:
                    continue

                sentence_counter += 1
                sid = f"S{sentence_counter:03d}"

                # Clean text (strip markers for display)
                clean = re.sub(r'\s*\((REFS?)\)', '', raw).strip()

                sentences.append(SentenceRecord(
                    id=sid,
                    paragraph_index=para_idx,
                    sentence_index=sent_idx,
                    raw_text=raw,
                    clean_text=clean,
                    section=current_section or None,
                ))

        logger.info(f"Parsed {len(sentences)} sentences from document")
        return sentences
