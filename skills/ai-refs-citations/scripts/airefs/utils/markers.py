"""Grammar for citation markers.

Recognised parentheticals (the whole content must parse, otherwise the
parenthetical is ordinary prose and left alone):

    (REF)  (REFS)
    (PMID: 32879322)  (PMID 32879322)  (PMIDs: 32879322, 12345678)
    (PMC11413553)  (PMCID: PMC11413553)  (PMC11413553, PMC3159129)
    (doi: 10.1101/2024.01.03.574066)  (https://doi.org/10.1038/s41586-024-07487-w)
    (Battison et al. 2024)  (Battison et al., 2024)  (Smith and Jones 2020)
    (Smith & Jones, 2020a)  (van der Berg et al. 2019)  (Smith 2019, 2021)
    (see Smith et al. 2020; Jones 2021)  (PMID: 32879322; Battison et al. 2024)
    (REF, PMID: 32879322)   -> verify the suggestion AND search for one more

Items are separated by ';', ',' or ' and '.  A bare number after a PMID
item is another PMID; a bare year after an author-year item re-uses the
same authors (APA style "Smith 2019, 2021").

The public entry points are :func:`find_markers` and :func:`strip_markers`.
"""

import re
from dataclasses import dataclass
from typing import Optional

from ..models.markers import (
    MarkerConfig, MarkerSpec, MarkerType, SuggestedCitation, SuggestionKind,
)

# ── Building blocks ─────────────────────────────────────────────────────

# A parenthetical, allowing one nested level so DOIs such as
# "10.1016/S0140-6736(20)30183-5" are reachable.  Innermost pairs are still
# tried on their own when the outer pair is not a marker.
PAREN_RE = re.compile(r'\((?:[^()]|\([^()]*\))*\)')

# Words that may precede a citation list: "(see Smith 2020)", "(e.g., PMID: 1)"
_LEADIN_RE = re.compile(
    r'(?:see\s+also|see|e\.?g\.?|reviewed\s+in|but\s+see|cf\.?|also|'
    r'for\s+(?:a\s+)?reviews?\s+see|for\s+(?:a\s+)?reviews?|'
    r'reviewed\s+by|as\s+in|from|in|biorxiv|medrxiv|preprint|preprints|'
    r'available\s+at)\s*[,:]?\s+',
    re.IGNORECASE,
)

# Separators between items.
_SEP_RE = re.compile(r'\s*(?:;|,|\s+and\s+)\s*', re.IGNORECASE)

# Optional trailing period / whitespace after the last item.
_TRAIL_RE = re.compile(r'\s*\.?\s*$')

# (REF) / (REFS) tokens -- uppercase only, as in earlier app versions.
_REF_RE = re.compile(r'(REFS?)(?![A-Za-z0-9_])')

# PMCID: PMC11413553 / PMCID: PMC11413553 / PMC 11413553
_PMCID_RE = re.compile(r'(?:PMCID\s*:?\s*(?:PMC)?|PMC)\s?(\d{4,9})(?![A-Za-z0-9_])', re.IGNORECASE)

# PMID: 32879322 / PMID 32879322 / PMIDs: 1, 2
_PMID_RE = re.compile(r'(?:PMIDs?|PubMed(?:\s+IDs?)?)\s*:?\s*(\d{1,9})(?![A-Za-z0-9_])', re.IGNORECASE)

# DOI with optional prefix; a bare "10.xxxx/..." is accepted too.
_DOI_RE = re.compile(
    r'(?:doi\s*:?\s*|https?://(?:dx\.)?doi\.org/|doi\.org/|'
    r'https?://(?:www\.)?(?:biorxiv|medrxiv)\.org/content/)?'
    r'(10\.\d{4,9}/(?:[^\s;,()"\'?#]|\([^\s()]*\))+)',
    re.IGNORECASE,
)

# A bare number: only meaningful right after a PMID item.
_BARE_NUMBER_RE = re.compile(r'(\d{1,9})(?![A-Za-z0-9_.])')

# A bare year: only meaningful right after an author-year item.
_BARE_YEAR_RE = re.compile(r'((?:19|20)\d{2})([a-z])?(?![A-Za-z0-9_])')

# Author names.  A "word" is a capitalised run of letters, allowing an
# internal apostrophe or hyphen (O'Brien, Smith-Jones, McDonald, Müller).
_PARTICLE_WORDS = {
    "van", "von", "de", "del", "della", "der", "den", "la", "le", "du", "di", "da",
    "dos", "das", "el", "al", "ter", "ten", "zu", "af", "bin", "ibn",
}
_PARTICLE = r"(?:" + "|".join(sorted(_PARTICLE_WORDS)) + r")"
# A name word never is the "ET" / "AL" of an upper-case "ET AL." ("Al-Hasani" is fine).
_NOT_ET_AL = r"(?![Ee][Tt](?:\s|$)|[Aa][Ll](?:[.\s,;]|$))"
_WORD = rf"{_NOT_ET_AL}[A-ZÀ-Þ\u0100-\u024F][^\W\d_]*(?:['’\-‐‑][^\W\d_]+)*"
_NAME = rf"(?:{_PARTICLE}\s+)*{_WORD}(?:\s+(?:{_PARTICLE}\s+)*{_WORD}){{0,2}}"
# Optional initials after a surname: "Smith, J.A." / "Smith J. A."
_INITIALS = r"(?:,?\s*(?:[A-Z]\.\s?){1,3})?"
# "et al." in any case, with or without periods ("et al", "et. al.", "ET AL.")
_ET_AL = r"[eE][tT]\.?\s+[aA][lL]\.?"
# The year must be set off from the authors by whitespace or a comma so that
# product codes such as "R2023b" are not read as a year.
_YEAR_SEP = r"(?:\s*,\s*|\s+)"
_AUTHOR_YEAR_RE = re.compile(
    rf"(?P<a1>{_NAME}){_INITIALS}"
    rf"(?:(?P<etal>\s*,?\s*{_ET_AL})|\s*,?\s*(?:and|&)\s+(?P<a2>{_NAME}){_INITIALS})?"
    rf"{_YEAR_SEP}(?P<year>(?:19|20)\d{{2}})(?P<suffix>[a-z])?(?![A-Za-z0-9_])"
)

# Words that are never the first word of an author name, whatever follows.
_NEVER_AUTHOR = {
    "figure", "fig", "figs", "figures", "table", "tables", "eq", "equation",
    "section", "sec", "chapter", "suppl", "supplementary", "supplement",
    "appendix", "panel", "vol", "volume", "issue", "since", "until",
    "before", "after", "during", "circa", "between", "through", "from", "by",
    "in", "on", "at", "to", "as", "of", "fy", "approximately", "approx",
    "about", "around", "est", "ca", "c", "n", "p", "pp", "no", "nos",
    "number", "total", "mean", "median", "unpublished", "personal",
}

# Words that read as a date, a status or a timeline entry when they stand
# alone before a year ("(May 2020)", "(Early 2025)").  With "et al." or a
# co-author they are ordinary surnames (May, Grant, Page, Winter, Born...).
_AUTHOR_STOPLIST = {
    # dates
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "spring", "summer", "fall", "autumn", "winter",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "since", "until", "before", "after", "during", "circa", "between", "through",
    "from", "by", "in", "on", "at", "to", "as", "of",
    "fy", "year", "years", "quarter", "q1", "q2", "q3", "q4",
    # document parts
    "figure", "fig", "figs", "figures", "table", "tables", "eq", "equation",
    "section", "sec", "chapter", "suppl", "supplementary", "supplement",
    "appendix", "box", "panel", "page", "pages", "vol", "volume", "issue",
    # bibliographic / status words
    "established", "founded", "copyright", "version", "release", "revised",
    "updated", "accessed", "retrieved", "published", "submitted", "received",
    "accepted", "effective", "expires", "deadline", "due", "born", "died",
    "approximately", "approx", "about", "around", "est", "ca", "c",
    "n", "p", "pp", "no", "nos", "number", "total", "mean", "median",
    "cohort", "batch", "class", "grant", "award", "protocol", "study",
    "pilot", "phase", "wave", "round", "cycle",
    # timeline / status words
    "early", "late", "mid", "expected", "anticipated", "projected", "planned",
    "estimated", "target", "start", "starting", "beginning", "end", "ending",
    "recruitment", "baseline", "approved", "amended", "renewal", "resubmission",
    "post", "pre", "current", "present", "ongoing", "completed", "launched",
    "adopted", "enacted", "ratified", "signed",
}

# First words of a lead-in phrase; when followed by another capitalised word
# they introduce the citation rather than being part of the surname.
_LEADIN_FIRST_WORDS = {
    "see", "cf", "also", "from", "in", "as", "reviewed", "for", "but",
    "available", "biorxiv", "medrxiv", "preprint", "preprints", "eg",
}

# Words anywhere in a "name" that mark an organisation rather than an author.
# Consortium / project names are deliberately NOT listed: "(GTEx Consortium 2020)"
# is a real citation form that PubMed indexes under the collective author.
_ORG_WORDS = {
    "university", "universities", "institute", "institutes", "center", "centre",
    "inc", "ltd", "llc", "corp", "corporation", "company", "foundation",
    "hospital", "college", "department", "dept", "laboratory", "laboratories",
    "lab", "labs", "agency", "ministry", "school", "clinic", "gmbh", "atlas",
    "technologies", "biosciences", "pharmaceuticals", "unpublished",
    "organization", "organisation", "association", "society", "biobank",
    "reporter", "prize", "conference", "meeting", "committee", "council",
    "illustrator", "prism", "matlab", "biorender",
}


@dataclass
class _Item:
    end: int
    suggestion: Optional[SuggestedCitation] = None
    ref_token: str = ""          # "REF" or "REFS" when the item is a search token


@dataclass
class ParsedMarker:
    """Result of parsing the inside of a parenthetical."""
    kind: MarkerType
    suggestions: list[SuggestedCitation]
    extra_search: int = 0


# ── Public API ──────────────────────────────────────────────────────────

# Exact pattern used by app versions before suggested markers existed.
_LEGACY_RE = re.compile(r'\((REFS?)\)')

# Innermost parentheticals inside an outer pair that is not itself a marker.
_INNER_PAREN_RE = re.compile(r'\(([^()]*)\)')


def find_markers(text: str, config: Optional[MarkerConfig] = None) -> list[MarkerSpec]:
    """Return every citation marker in *text*, in order of appearance."""
    config = config or MarkerConfig.all_on()
    markers: list[MarkerSpec] = []

    if not config.detect_ids and not config.detect_author_year:
        # Detection off (or an old project file): behave exactly like the
        # original app, which only knew the literal tokens (REF) and (REFS).
        for m in _LEGACY_RE.finditer(text or ""):
            kind = MarkerType.REFS if m.group(1) == "REFS" else MarkerType.REF
            markers.append(MarkerSpec(text=m.group(0), kind=kind, start=m.start(), end=m.end()))
        return markers

    for m in PAREN_RE.finditer(text or ""):
        outer = m.group(0)
        parsed = parse_marker_content(outer[1:-1], config)
        if parsed is not None:
            markers.append(MarkerSpec(
                text=outer,
                kind=parsed.kind,
                start=m.start(),
                end=m.end(),
                suggestions=parsed.suggestions,
                extra_search=parsed.extra_search,
            ))
            continue
        # The outer pair is prose; its innermost parentheticals may still be
        # markers, e.g. "(see Smith et al. (2020) and (PMID: 1))".
        for inner in _INNER_PAREN_RE.finditer(outer):
            parsed = parse_marker_content(inner.group(1), config)
            if parsed is None:
                continue
            markers.append(MarkerSpec(
                text=inner.group(0),
                kind=parsed.kind,
                start=m.start() + inner.start(),
                end=m.start() + inner.end(),
                suggestions=parsed.suggestions,
                extra_search=parsed.extra_search,
            ))
    return markers


def strip_markers(text: str, config: Optional[MarkerConfig] = None) -> str:
    """Remove every marker (and the whitespace before it) from *text*."""
    if not text:
        return ""
    out = text
    for marker in reversed(find_markers(text, config)):
        start = marker.start
        while start > 0 and out[start - 1].isspace():
            start -= 1
        out = out[:start] + out[marker.end:]
    return out.strip()


def parse_marker_content(inner: str, config: Optional[MarkerConfig] = None) -> Optional[ParsedMarker]:
    """Parse the text between the parentheses.  Returns None for ordinary prose."""
    config = config or MarkerConfig.all_on()
    s = (inner or "").strip()
    if not s:
        return None

    items: list[_Item] = []
    pos = 0
    n = len(s)
    last_item: Optional[_Item] = None

    while True:
        item = _match_item(s, pos, config, last_item)
        if item is None:
            # Skip lead-in words such as "see", "e.g.,", "reviewed in" (possibly
            # several) and try again.
            lead_pos = pos
            while True:
                lead = _LEADIN_RE.match(s, lead_pos)
                if not lead or lead.end() == lead_pos:
                    break
                lead_pos = lead.end()
                item = _match_item(s, lead_pos, config, last_item)
                if item is not None:
                    break
        if item is None:
            return None
        items.append(item)
        last_item = item
        pos = item.end

        trail = _TRAIL_RE.match(s, pos)
        if trail is not None and trail.end() == n:
            break

        sep = _SEP_RE.match(s, pos)
        if sep is None or sep.end() == pos:
            return None
        pos = sep.end()
        if pos >= n:
            return None  # dangling separator, e.g. "(PMID: 1,)"

    suggestions = [it.suggestion for it in items if it.suggestion is not None]
    ref_tokens = [it.ref_token for it in items if it.ref_token]

    if not suggestions:
        if not ref_tokens:
            return None
        if "REFS" in ref_tokens:
            return ParsedMarker(kind=MarkerType.REFS, suggestions=[])
        if len(ref_tokens) == 1:
            return ParsedMarker(kind=MarkerType.REF, suggestions=[])
        # "(REF, REF)": several references wanted, same as (REFS)
        return ParsedMarker(kind=MarkerType.REFS, suggestions=[])

    extra = -1 if "REFS" in ref_tokens else ref_tokens.count("REF")
    return ParsedMarker(kind=MarkerType.SUGGESTED, suggestions=suggestions, extra_search=extra)


# ── Item matching ───────────────────────────────────────────────────────

def _match_item(s: str, pos: int, config: MarkerConfig, last: Optional[_Item]) -> Optional[_Item]:
    m = _REF_RE.match(s, pos)
    if m:
        return _Item(end=m.end(), ref_token=m.group(1))

    if config.detect_ids:
        m = _PMCID_RE.match(s, pos)
        if m:
            return _Item(end=m.end(), suggestion=SuggestedCitation(
                kind=SuggestionKind.PMCID,
                raw=m.group(0).strip(),
                value=f"PMC{m.group(1)}",
            ))

        m = _PMID_RE.match(s, pos)
        if m:
            return _Item(end=m.end(), suggestion=SuggestedCitation(
                kind=SuggestionKind.PMID,
                raw=m.group(0).strip(),
                value=m.group(1),
            ))

        m = _DOI_RE.match(s, pos)
        if m:
            doi = m.group(1).rstrip(".")
            end = m.start(1) + len(doi)
            return _Item(end=end, suggestion=SuggestedCitation(
                kind=SuggestionKind.DOI,
                raw=s[pos:end].strip(),
                value=doi,
            ))

        # Bare number continuing a PMID list: "(PMID: 1, 2, 3)"
        if last is not None and last.suggestion is not None \
                and last.suggestion.kind == SuggestionKind.PMID:
            m = _BARE_NUMBER_RE.match(s, pos)
            if m:
                return _Item(end=m.end(), suggestion=SuggestedCitation(
                    kind=SuggestionKind.PMID, raw=m.group(0), value=m.group(1),
                ))

    if config.detect_author_year:
        # Bare year continuing an author-year list: "(Smith 2019, 2021)"
        if last is not None and last.suggestion is not None \
                and last.suggestion.kind == SuggestionKind.AUTHOR_YEAR:
            m = _BARE_YEAR_RE.match(s, pos)
            if m:
                prev = last.suggestion
                year = int(m.group(1))
                suffix = m.group(2) or ""
                return _Item(end=m.end(), suggestion=SuggestedCitation(
                    kind=SuggestionKind.AUTHOR_YEAR,
                    raw=m.group(0),
                    value=f"{prev.author} {year}{suffix}",
                    author=prev.author,
                    coauthor=prev.coauthor,
                    et_al=prev.et_al,
                    year=year,
                    year_suffix=suffix,
                ))

        m = _AUTHOR_YEAR_RE.match(s, pos)
        if m:
            author = _normalise_name(m.group("a1"))
            coauthor = _normalise_name(m.group("a2") or "")
            if not _looks_like_author(author, bool(m.group("etal")), coauthor) \
                    or (coauthor and not _looks_like_author(coauthor, False, author)):
                return None
            year = int(m.group("year"))
            suffix = m.group("suffix") or ""
            return _Item(end=m.end(), suggestion=SuggestedCitation(
                kind=SuggestionKind.AUTHOR_YEAR,
                raw=m.group(0).strip(),
                value=f"{author} {year}{suffix}",
                author=author,
                coauthor=coauthor,
                et_al=bool(m.group("etal")),
                year=year,
                year_suffix=suffix,
            ))

    return None


def _normalise_name(name: str) -> str:
    """Collapse whitespace inside a name."""
    return re.sub(r"\s+", " ", (name or "").strip())


def _looks_like_author(name: str, has_et_al: bool, other: str) -> bool:
    """Reject 'names' that are dates, document parts, organisations or acronyms."""
    words = [w.lower().strip("'’-") for w in name.split()]
    if not words:
        return False
    if words[0] in _NEVER_AUTHOR:
        return False
    # "(See Smith 2020)": a lead-in word followed by the real name.  A lone
    # "See et al. 2020" is a genuine (rare) surname and passes.
    if len(words) > 1 and words[0] in _LEADIN_FIRST_WORDS:
        return False
    if any(w in _ORG_WORDS for w in words):
        return False
    first_real = next((w for w in name.split() if w.lower() not in _PARTICLE_WORDS), name.split()[0])
    if not first_real[0].isupper():
        return False
    lone = len(words) == 1 and not has_et_al and not other
    if lone:
        if words[0] in _AUTHOR_STOPLIST:
            return False
        # A lone short all-caps token such as "NIH", "FDA", "WHO" is an organisation
        # (its reports are not in PubMed); "SMITH et al." or "NIH and Smith" still pass.
        raw = name.strip()
        if raw.isupper() and 2 <= len(raw) <= 5:
            return False
    return True
