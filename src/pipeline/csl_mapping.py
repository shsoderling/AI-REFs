"""CitationCandidate <-> CSL-JSON item mapping, record identity and URIs.

The citation fields embedded in a document carry a CSL-JSON ``itemData``
per cited record (the "traveling library") plus an ordered ``uris`` identity
vector. This module is the only place that knows how a CitationCandidate
maps onto that shape, so reading a field back is lossless except for the
abstract (never embedded) and the transient ranking scores.
"""

import hashlib
import re
import uuid

from ..models.citation import Author, CitationCandidate

RECORD_URI_PREFIX = "airefs:record/"
HASH_URI_PREFIX = "airefs:hash/"
DOI_URL_PREFIX = "https://doi.org/"
PUBMED_URL_PREFIX = "https://pubmed.ncbi.nlm.nih.gov/"
PMC_URL_PREFIX = "https://www.ncbi.nlm.nih.gov/pmc/articles/"
MAX_EMBEDDED_MESH_TERMS = 30

_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)


def normalize_doi(doi: str) -> str:
    """Bare, lowercase DOI ('' for empty): strips doi.org URLs and 'doi:'."""
    doi = (doi or "").strip()
    if not doi:
        return ""
    return _DOI_PREFIX_RE.sub("", doi).strip().lower()


def ensure_record_uuid(citation: CitationCandidate) -> str:
    """The candidate's record uuid, minting (and storing) one if it has none."""
    if not citation.record_uuid:
        citation.record_uuid = str(uuid.uuid4())
    return citation.record_uuid


# ── authors ──────────────────────────────────────────────────────────

def _author_to_csl(author: Author) -> dict:
    given = author.first_name or author.initials
    out = {}
    if author.last_name:
        out["family"] = author.last_name
    if given:
        out["given"] = given
    return out


def _author_from_csl(data: dict) -> Author:
    given = (data.get("given") or "").strip()
    tokens = given.replace(".", " ").split()
    looks_like_initials = bool(tokens) and all(len(t) <= 2 and t.isupper() for t in tokens)
    if looks_like_initials:
        return Author(last_name=data.get("family", "") or "", first_name="",
                      initials="".join(tokens))
    initials = "".join(t[0].upper() for t in tokens)
    return Author(last_name=data.get("family", "") or "", first_name=given, initials=initials)


# ── items ────────────────────────────────────────────────────────────

def to_csl_item(citation: CitationCandidate) -> dict:
    """CSL-JSON item for *citation*. Empty values are omitted (except id,
    type and title); the abstract never travels; fields CSL lacks live under
    ``custom.airefs``.
    """
    item = {
        "id": citation.record_uuid,
        "type": "article-journal",
        "title": citation.title or "",
    }
    if citation.authors:
        item["author"] = [_author_to_csl(a) for a in citation.authors]
    for key, value in (
        ("container-title", citation.journal),
        ("container-title-short", citation.journal_abbrev),
        ("volume", citation.volume),
        ("issue", citation.issue),
        ("page", citation.pages),
        ("DOI", normalize_doi(citation.doi)),
        ("PMID", citation.pmid),
        ("PMCID", citation.pmcid),
    ):
        if value:
            item[key] = value
    if citation.year:
        item["issued"] = {"date-parts": [[int(citation.year)]]}
    author_count = max(int(citation.author_count or 0), len(citation.authors))
    item["custom"] = {"airefs": {
        "source": citation.source or "",
        "authorCount": author_count,
        "authorsTruncated": author_count > len(citation.authors),
        "isReview": bool(citation.is_review),
        "retracted": bool(citation.is_retracted),
        "retractionNotice": citation.retraction_notice or "",
        "hasErratum": bool(citation.has_erratum),
        "publicationTypes": list(citation.publication_types or []),
        "meshTerms": list(citation.mesh_terms or [])[:MAX_EMBEDDED_MESH_TERMS],
        "rawEntry": citation.raw_entry or "",
    }}
    return item


def _first_year(item: dict) -> int:
    issued = item.get("issued") or {}
    parts = issued.get("date-parts") or []
    try:
        return int(parts[0][0])
    except (IndexError, TypeError, ValueError):
        return 0


def from_csl_item(item: dict) -> CitationCandidate:
    """CitationCandidate from a CSL-JSON item written by :func:`to_csl_item`
    (unknown keys ignored, missing keys defaulted). The original ``source``
    is restored when the item recorded it; ``"embedded"`` otherwise. A
    candidate that came from a field always carries a ``record_uuid``.
    """
    airefs = (item.get("custom") or {}).get("airefs") or {}
    authors = [_author_from_csl(a) for a in item.get("author") or [] if isinstance(a, dict)]
    try:
        author_count = int(airefs.get("authorCount") or 0)
    except (TypeError, ValueError):
        author_count = 0
    return CitationCandidate(
        record_uuid=str(item.get("id") or ""),
        title=item.get("title") or "",
        authors=authors,
        author_count=author_count if author_count > len(authors) else 0,
        journal=item.get("container-title") or "",
        journal_abbrev=item.get("container-title-short") or "",
        volume=str(item.get("volume") or ""),
        issue=str(item.get("issue") or ""),
        pages=str(item.get("page") or ""),
        doi=normalize_doi(item.get("DOI") or ""),
        pmid=str(item.get("PMID") or ""),
        pmcid=str(item.get("PMCID") or ""),
        year=_first_year(item),
        source=str(airefs.get("source") or "embedded"),
        is_review=bool(airefs.get("isReview", False)),
        is_retracted=bool(airefs.get("retracted", False)),
        retraction_notice=airefs.get("retractionNotice") or "",
        has_erratum=bool(airefs.get("hasErratum", False)),
        publication_types=list(airefs.get("publicationTypes") or []),
        mesh_terms=list(airefs.get("meshTerms") or []),
        raw_entry=airefs.get("rawEntry") or "",
    )


# ── identity URIs ────────────────────────────────────────────────────

def _title_hash(citation: CitationCandidate) -> str:
    basis = citation.title or citation.raw_entry or ""
    norm = re.sub(r"[^a-z0-9]+", " ", basis.lower()).strip()
    return hashlib.sha1(f"{norm}|{citation.year or 0}".encode("utf-8")).hexdigest()


def build_uris(citation: CitationCandidate) -> list[str]:
    """Ordered identity vector: record uuid, DOI, PMID, PMCID; a title hash
    only when the record has neither DOI nor PMID."""
    uris = [RECORD_URI_PREFIX + ensure_record_uuid(citation)]
    doi = normalize_doi(citation.doi)
    if doi:
        uris.append(DOI_URL_PREFIX + doi)
    if citation.pmid:
        uris.append(f"{PUBMED_URL_PREFIX}{citation.pmid.strip()}/")
    if citation.pmcid:
        uris.append(f"{PMC_URL_PREFIX}{citation.pmcid.strip()}/")
    if not doi and not citation.pmid:
        uris.append(HASH_URI_PREFIX + _title_hash(citation))
    return uris


def identity_from_uris(uris: list[str]) -> dict:
    """Parse the identifiers back out of a ``uris`` list."""
    ident = {"record_uuid": "", "doi": "", "pmid": "", "pmcid": ""}
    for uri in uris or []:
        uri = (uri or "").strip()
        if uri.startswith(RECORD_URI_PREFIX) and not ident["record_uuid"]:
            ident["record_uuid"] = uri[len(RECORD_URI_PREFIX):]
        elif uri.startswith(DOI_URL_PREFIX) and not ident["doi"]:
            ident["doi"] = normalize_doi(uri)
        elif uri.startswith(PUBMED_URL_PREFIX) and not ident["pmid"]:
            ident["pmid"] = uri[len(PUBMED_URL_PREFIX):].strip("/")
        elif uri.startswith(PMC_URL_PREFIX) and not ident["pmcid"]:
            ident["pmcid"] = uri[len(PMC_URL_PREFIX):].strip("/")
    return ident
