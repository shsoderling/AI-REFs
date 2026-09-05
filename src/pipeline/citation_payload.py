"""Build and parse the JSON payloads of AIREFS.CITE and AIREFS.BIBL fields.

A citation field's instruction text is " ADDIN AIREFS.CITE {json} " where the
JSON follows the shape of csl-citation.json (citationID, properties,
citationItems[].{id, uris, itemData}) plus an "airefs" block with the
rendered numbers and render kind. The bibliography field's payload is a
cache of the rendered order and per-entry hashes; membership and order are
always recomputable from the citation fields.

Parsing is deliberately lenient: the JSON is whatever lies between the
first '{' and the last '}', unknown keys are ignored, a missing citationID
or record id is minted and reported, and only a payload written by a newer
schema version refuses to load.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..models.citation import CitationCandidate
from .csl_mapping import build_uris, ensure_record_uuid, identity_from_uris, to_csl_item

FIELD_SCHEMA_VERSION = 1
CITE_TOKEN = "AIREFS.CITE"
BIBL_TOKEN = "AIREFS.BIBL"
CSL_SCHEMA = "https://resource.citationstyles.org/schema/latest/input/json/csl-citation.json"
MAX_ITEM_BYTES = 16384
MAX_AUTHORS_WHEN_TRUNCATED = 30
RENDER_KINDS = ("numeric-superscript", "numeric-bracket", "numeric-paren", "author-date")

_BASE32_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
_CITATION_ID_LENGTH = 12


class PayloadError(ValueError):
    """The field code carries no usable JSON payload."""


class NewerSchemaError(PayloadError):
    """The payload was written by a newer AI REFs; open read-only."""


def mint_citation_id() -> str:
    """A fresh per-cluster id: 12 lowercase base32 characters."""
    return "".join(secrets.choice(_BASE32_ALPHABET) for _ in range(_CITATION_ID_LENGTH))


def entry_hash(text: str) -> str:
    """Hash of a rendered bibliography entry, for hand-edit detection."""
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()


def is_cite_code(code: str) -> bool:
    return (code or "").strip().startswith(f"ADDIN {CITE_TOKEN}")


def is_bibl_code(code: str) -> bool:
    return (code or "").strip().startswith(f"ADDIN {BIBL_TOKEN}")


def extract_json(code: str) -> str:
    """The substring from the first '{' to the last '}' of a field code."""
    code = code or ""
    start, end = code.find("{"), code.rfind("}")
    if start < 0 or end < start:
        raise PayloadError("field code contains no JSON payload")
    return code[start:end + 1]


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _loads(code: str) -> dict:
    try:
        data = json.loads(extract_json(code))
    except json.JSONDecodeError as exc:
        raise PayloadError(f"field payload is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PayloadError("field payload is not a JSON object")
    return data


def _check_version(version) -> int:
    try:
        version = int(version)
    except (TypeError, ValueError):
        version = FIELD_SCHEMA_VERSION
    if version > FIELD_SCHEMA_VERSION:
        raise NewerSchemaError(
            f"field schema v{version} is newer than the supported v{FIELD_SCHEMA_VERSION}")
    return version


# ── citation fields ───────────────────────────────────────────────────

@dataclass
class CiteItem:
    record_uuid: str
    uris: list[str]
    item: dict                       # CSL-JSON itemData (raw; from_csl_item() turns it into a candidate)
    identity: dict                   # record_uuid / doi / pmid / pmcid parsed from uris


@dataclass
class CitePayload:
    citation_id: str
    items: list[CiteItem] = field(default_factory=list)
    numbers: list[int] = field(default_factory=list)
    render: str = ""
    style: str = ""
    plain: str = ""
    unresolved: bool = False
    version: int = FIELD_SCHEMA_VERSION
    problems: list[str] = field(default_factory=list)


def _guarded_item(citation: CitationCandidate) -> dict:
    """CSL item for *citation*, with the size safeguard applied."""
    item = to_csl_item(citation)
    if len(_dumps(item).encode("utf-8")) > MAX_ITEM_BYTES and len(item.get("author", [])) > MAX_AUTHORS_WHEN_TRUNCATED:
        item["author"] = item["author"][:MAX_AUTHORS_WHEN_TRUNCATED]
        item["custom"]["airefs"]["authorsTruncated"] = True
    return item


def build_cite_code(items: list[CitationCandidate], *, numbers: list[int], render: str,
                    style: str, plain: str, citation_id: str | None = None,
                    unresolved: bool = False) -> str:
    """The instruction text of an AIREFS.CITE field for one citation cluster.

    Mints a record uuid on any candidate lacking one (stored on the
    candidate, so the same record keeps its identity across fields).
    """
    citation_items = []
    for citation in items:
        ensure_record_uuid(citation)
        citation_items.append({
            "id": citation.record_uuid,
            "uris": build_uris(citation),
            "itemData": _guarded_item(citation),
        })
    payload = {
        "schema": CSL_SCHEMA,
        "citationID": citation_id or mint_citation_id(),
        "properties": {"formattedCitation": plain, "plainCitation": plain, "noteIndex": 0},
        "citationItems": citation_items,
        "airefs": {
            "v": FIELD_SCHEMA_VERSION,
            "style": style,
            "render": render,
            "numbers": [int(n) for n in numbers],
            "unresolved": bool(unresolved),
        },
    }
    return f" ADDIN {CITE_TOKEN} {_dumps(payload)} "


def parse_cite_code(code: str) -> CitePayload:
    data = _loads(code)
    airefs = data.get("airefs") or {}
    if not isinstance(airefs, dict):
        airefs = {}
    version = _check_version(airefs.get("v", FIELD_SCHEMA_VERSION))
    problems: list[str] = []
    cid = str(data.get("citationID") or "")
    if not cid:
        cid = mint_citation_id()
        problems.append("citationID missing; minted a new one")
    items: list[CiteItem] = []
    for raw in data.get("citationItems") or []:
        if not isinstance(raw, dict):
            continue
        item = raw.get("itemData") or {}
        if not isinstance(item, dict):
            item = {}
        uris = [str(u) for u in (raw.get("uris") or [])]
        identity = identity_from_uris(uris)
        record_uuid = str(raw.get("id") or identity.get("record_uuid") or item.get("id") or "")
        if not record_uuid:
            record_uuid = str(uuid.uuid4())
            problems.append("record id missing; minted a new one")
        if not item.get("id"):
            item = dict(item, id=record_uuid)
        items.append(CiteItem(record_uuid=record_uuid, uris=uris, item=item, identity=identity))
    props = data.get("properties") or {}
    if not isinstance(props, dict):
        props = {}
    numbers = []
    for n in airefs.get("numbers") or []:
        try:
            numbers.append(int(n))
        except (TypeError, ValueError):
            problems.append(f"ignored non-numeric citation number {n!r}")
    return CitePayload(
        citation_id=cid, items=items, numbers=numbers,
        render=str(airefs.get("render") or ""), style=str(airefs.get("style") or ""),
        plain=str(props.get("plainCitation") or ""),
        unresolved=bool(airefs.get("unresolved", False)),
        version=version, problems=problems,
    )


# ── bibliography field ────────────────────────────────────────────────

@dataclass
class BiblPayload:
    doc_id: str = ""
    style: str = ""
    render: str = ""
    heading_text: str = ""
    order: list[str] = field(default_factory=list)          # record uuids in rendered order
    uncited: list[str] = field(default_factory=list)        # record uuids kept without a citation
    entry_hashes: list[str] = field(default_factory=list)   # parallel to order
    version: int = FIELD_SCHEMA_VERSION
    exported_at: str = ""
    problems: list[str] = field(default_factory=list)


def build_bibl_code(*, doc_id: str, style: str, render: str, heading_text: str,
                    order: list[str], uncited: list[str], entry_hashes: list[str]) -> str:
    payload = {
        "v": FIELD_SCHEMA_VERSION,
        "docId": doc_id,
        "style": style,
        "render": render,
        "headingText": heading_text,
        "order": list(order),
        "uncited": list(uncited),
        "entryHashes": list(entry_hashes),
        "app": "AI REFs",
        "exportedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return f" ADDIN {BIBL_TOKEN} {_dumps(payload)} "


def parse_bibl_code(code: str) -> BiblPayload:
    data = _loads(code)
    version = _check_version(data.get("v", FIELD_SCHEMA_VERSION))
    problems: list[str] = []
    order = [str(u) for u in (data.get("order") or [])]
    hashes = [str(h) for h in (data.get("entryHashes") or [])]
    if hashes and len(hashes) != len(order):
        problems.append("entryHashes does not match order; hashes ignored")
        hashes = []
    return BiblPayload(
        doc_id=str(data.get("docId") or ""), style=str(data.get("style") or ""),
        render=str(data.get("render") or ""), heading_text=str(data.get("headingText") or ""),
        order=order, uncited=[str(u) for u in (data.get("uncited") or [])],
        entry_hashes=hashes, version=version, exported_at=str(data.get("exportedAt") or ""),
        problems=problems,
    )
