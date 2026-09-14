"""Shared helpers for the ai-refs-citations scripts.

Every script imports this first: it puts the vendored ``airefs`` package on
the path, reads the NCBI credentials (flag > environment > config file) and
knows how to turn records into ``CitationCandidate`` objects.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Iterable, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

CONFIG_PATH = Path.home() / ".ai_refs" / "skill_config.json"

from airefs.models.citation import CitationCandidate  # noqa: E402


def die(message: str, code: int = 2) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def load_json(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dump_json(data, path: Optional[str | Path]) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if path:
        Path(path).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def read_config() -> dict:
    try:
        return load_json(CONFIG_PATH)
    except (OSError, ValueError):
        return {}


def save_config(email: str, api_key: str = "") -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = read_config()
    if email:
        data["ncbi_email"] = email
    if api_key:
        data["ncbi_api_key"] = api_key
    CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def ncbi_credentials(email: Optional[str], api_key: Optional[str], save: bool = False) -> tuple[str, str]:
    """NCBI email (required by E-utilities) and optional API key."""
    cfg = read_config()
    email = (email or os.environ.get("AIREFS_NCBI_EMAIL") or cfg.get("ncbi_email") or "").strip()
    api_key = (api_key or os.environ.get("AIREFS_NCBI_API_KEY") or cfg.get("ncbi_api_key") or "").strip()
    if not email:
        die("an NCBI email is required (E-utilities policy): pass --email, set AIREFS_NCBI_EMAIL, "
            "or run once with --email you@institution.edu --save-config")
    if save:
        save_config(email, api_key)
    return email, api_key


def library_path(option: Optional[str]) -> Optional[str]:
    """Resolve the --library option: 'none', 'auto' (the app's default library
    when it exists), or an explicit path."""
    from airefs.services.ref_library import DEFAULT_LIBRARY_PATH
    option = (option or "auto").strip()
    if option.lower() == "none":
        return None
    if option.lower() == "auto":
        return str(DEFAULT_LIBRARY_PATH) if Path(DEFAULT_LIBRARY_PATH).exists() else None
    path = Path(option).expanduser()
    if not path.exists():
        # A named path that is not there is a typo, not "no library": saying
        # nothing would quietly search the wrong thing.
        die(f"no reference library at {path} (pass --library auto or --library none)")
    return str(path)


def network_reachable(timeout: float = 10.0) -> tuple[bool, str]:
    """Can this shell reach PubMed at all?

    Sandboxed shells (Claude desktop, Cowork, CI) often have no outbound
    network even though the PubMed/bioRxiv connectors work fine.  Without this
    check a lookup failure looks exactly like "that identifier does not exist",
    which would make you tell the user their PMID is wrong.
    """
    import requests
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi"
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code >= 500:
            return False, f"PubMed answered HTTP {r.status_code}"
        return True, ""
    except Exception as exc:                                  # noqa: BLE001 - any failure means offline
        return False, str(exc)[:300]


OFFLINE_HINT = ("this shell cannot reach PubMed, so nothing could be looked up here. "
                "Use the PubMed / bioRxiv / Paperclip connectors instead and write the "
                "records into records.json by hand (references/file-formats.md); "
                "scan_markers.py and write_docx.py work offline.")


def split_list(value: Optional[str]) -> list[str]:
    if not value:
        return []
    out = []
    for part in value.replace(";", ",").split(","):
        part = part.strip()
        if part:
            out.append(part)
    return out


# ── records ──────────────────────────────────────────────────────────

def candidate_from_record(record: dict) -> CitationCandidate:
    """A CitationCandidate from a records.json entry (tolerant of extra keys
    and of hand-written records that only carry the bibliographic basics)."""
    data = dict(record)
    data.pop("key", None)
    data.pop("source_detail", None)
    authors = []
    for a in data.get("authors") or []:
        if isinstance(a, str):
            last, _, initials = a.partition(",")
            authors.append({"last_name": last.strip(), "initials": initials.strip()})
        elif isinstance(a, dict):
            authors.append(a)
    data["authors"] = authors
    try:
        data["year"] = int(data.get("year") or 0)
    except (TypeError, ValueError):
        data["year"] = 0
    known = set(CitationCandidate.model_fields)
    return CitationCandidate(**{k: v for k, v in data.items() if k in known})


def record_keys(cand: CitationCandidate) -> list[str]:
    """Every identifier a decisions file may use to point at this record."""
    keys = []
    if cand.pmid:
        keys.append(cand.pmid.strip())
    if cand.doi:
        keys.append(cand.doi.strip().lower())
    if cand.pmcid:
        keys.append(cand.pmcid.strip().upper())
    return keys


def index_records(record_files: Iterable[str | Path]) -> dict[str, CitationCandidate]:
    """Map pmid / lower-case doi / PMCID -> candidate across several records files
    (later files win, so a hand-corrected record can override a fetched one)."""
    index: dict[str, CitationCandidate] = {}
    for path in record_files:
        data = load_json(path)
        records = data.get("records", data) if isinstance(data, dict) else data
        for rec in records:
            cand = candidate_from_record(rec)
            explicit = str(rec.get("key", "") or "").strip()
            for key in record_keys(cand) + ([explicit] if explicit else []):
                index[key] = cand
    return index


def lookup_record(index: dict[str, CitationCandidate], key: str) -> Optional[CitationCandidate]:
    key = str(key).strip()
    for variant in (key, key.lower(), key.upper(), key.replace("https://doi.org/", "").lower()):
        if variant in index:
            return index[variant]
    if key.upper().startswith("PMID:"):
        return index.get(key[5:].strip())
    if key.upper().startswith("DOI:"):
        return index.get(key[4:].strip().lower())
    return None
