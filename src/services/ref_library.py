"""Persistent AI REFs user library + EndNote export import utilities."""

import json
import logging
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..models.citation import CitationCandidate, Author

logger = logging.getLogger(__name__)

_DEFAULT_LIBRARY_DIR = Path.home() / ".ai_refs"
DEFAULT_LIBRARY_PATH = _DEFAULT_LIBRARY_DIR / "ref_manager_library.db"


@dataclass
class ImportReport:
    """Import summary for library ingestion operations."""
    total_records: int = 0
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


class ReferenceLibrary:
    """Persistent local reference library used by agents and review sync."""

    def __init__(self, db_path: str = ""):
        if not db_path:
            _DEFAULT_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
            db_path = str(DEFAULT_LIBRARY_PATH)

        requested = str(Path(db_path).expanduser())
        candidates = [requested]
        # Fallbacks for restricted environments.
        candidates.append(str(Path.cwd() / ".ai_refs" / "ref_manager_library.db"))
        candidates.append("/tmp/ai_refs_ref_manager_library.db")

        self._conn = None
        self.db_path = requested
        last_exc: Optional[Exception] = None
        tried: set[str] = set()
        for candidate in candidates:
            if candidate in tried:
                continue
            tried.add(candidate)
            try:
                Path(candidate).parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(candidate, check_same_thread=False)
                conn.execute("PRAGMA journal_mode=WAL")
                self._conn = conn
                self.db_path = candidate
                if candidate != requested:
                    logger.warning(
                        f"Reference library fallback in use: '{requested}' -> '{candidate}'"
                    )
                break
            except Exception as exc:
                last_exc = exc
                continue

        if self._conn is None:
            raise sqlite3.OperationalError(
                f"Unable to open reference library database at '{requested}': {last_exc}"
            )

        self._create_tables()

    def _create_tables(self):
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS references_library (
                key TEXT PRIMARY KEY,
                pmid TEXT,
                doi TEXT,
                title TEXT NOT NULL,
                authors_json TEXT NOT NULL,
                year INTEGER NOT NULL DEFAULT 0,
                journal TEXT NOT NULL DEFAULT '',
                journal_abbrev TEXT NOT NULL DEFAULT '',
                volume TEXT NOT NULL DEFAULT '',
                issue TEXT NOT NULL DEFAULT '',
                pages TEXT NOT NULL DEFAULT '',
                abstract TEXT NOT NULL DEFAULT '',
                mesh_terms_json TEXT NOT NULL DEFAULT '[]',
                publication_types_json TEXT NOT NULL DEFAULT '[]',
                is_retracted INTEGER NOT NULL DEFAULT 0,
                is_review INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'literature',
                added_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_ref_library_pmid ON references_library(pmid);
            CREATE INDEX IF NOT EXISTS idx_ref_library_doi ON references_library(doi);
            CREATE INDEX IF NOT EXISTS idx_ref_library_year ON references_library(year);
            """
        )
        self._migrate_columns()

    def _migrate_columns(self):
        """Add columns introduced after the first release to older library files."""
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(references_library)").fetchall()
        }
        if "pmcid" not in cols:
            self._conn.execute(
                "ALTER TABLE references_library ADD COLUMN pmcid TEXT NOT NULL DEFAULT ''"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ref_library_pmcid ON references_library(pmcid)"
            )
            self._conn.commit()

    # Column list shared by every SELECT so row unpacking stays in one place.
    _SELECT_COLUMNS = (
        "key, pmid, doi, title, authors_json, year, journal, journal_abbrev, "
        "volume, issue, pages, abstract, mesh_terms_json, publication_types_json, "
        "is_retracted, is_review, source, updated_at, pmcid"
    )

    # ------------------------------------------------------------------
    # CRUD + search
    # ------------------------------------------------------------------

    def count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM references_library"
        ).fetchone()
        return int(row[0]) if row else 0

    def upsert_candidate(
        self,
        citation: CitationCandidate,
        source: str = "literature",
    ) -> tuple[bool, bool]:
        """Upsert a citation.

        Returns `(inserted, updated)`.
        """
        if not citation.title and not citation.pmid and not citation.doi:
            return False, False

        key = self._key_for_candidate(citation)
        if not key:
            return False, False

        now = time.time()
        row = self._conn.execute(
            "SELECT key FROM references_library WHERE key = ?",
            (key,),
        ).fetchone()

        pmcid = self._normalize_pmcid(citation.pmcid)
        values = (
            key,
            citation.pmid,
            self._normalize_doi(citation.doi),
            citation.title,
            json.dumps([a.model_dump() for a in citation.authors]),
            int(citation.year or 0),
            citation.journal,
            citation.journal_abbrev,
            citation.volume,
            citation.issue,
            citation.pages,
            citation.abstract,
            json.dumps(citation.mesh_terms or []),
            json.dumps(citation.publication_types or []),
            1 if citation.is_retracted else 0,
            1 if citation.is_review else 0,
            source or "literature",
        )

        if row is None:
            self._conn.execute(
                """
                INSERT INTO references_library (
                    key, pmid, doi, title, authors_json, year, journal, journal_abbrev,
                    volume, issue, pages, abstract, mesh_terms_json, publication_types_json,
                    is_retracted, is_review, source, added_at, updated_at, pmcid
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*values, now, now, pmcid),
            )
            self._conn.commit()
            return True, False

        self._conn.execute(
            """
            UPDATE references_library
            SET pmid = ?, doi = ?, title = ?, authors_json = ?, year = ?, journal = ?,
                journal_abbrev = ?, volume = ?, issue = ?, pages = ?, abstract = ?,
                mesh_terms_json = ?, publication_types_json = ?, is_retracted = ?,
                is_review = ?, source = ?, updated_at = ?,
                pmcid = CASE WHEN ? != '' THEN ? ELSE pmcid END
            WHERE key = ?
            """,
            (
                citation.pmid,
                self._normalize_doi(citation.doi),
                citation.title,
                json.dumps([a.model_dump() for a in citation.authors]),
                int(citation.year or 0),
                citation.journal,
                citation.journal_abbrev,
                citation.volume,
                citation.issue,
                citation.pages,
                citation.abstract,
                json.dumps(citation.mesh_terms or []),
                json.dumps(citation.publication_types or []),
                1 if citation.is_retracted else 0,
                1 if citation.is_review else 0,
                source or "literature",
                now,
                pmcid,
                pmcid,
                key,
            ),
        )
        self._conn.commit()
        return False, True

    def add_candidates(
        self,
        citations: list[CitationCandidate],
        source: str = "literature",
    ) -> tuple[int, int]:
        imported = 0
        updated = 0
        for citation in citations:
            ins, upd = self.upsert_candidate(citation, source=source)
            if ins:
                imported += 1
            elif upd:
                updated += 1
        return imported, updated

    def search(self, query: str, max_results: int = 10) -> list[CitationCandidate]:
        """Search the local library and return ranked candidate matches."""
        max_results = max(1, min(int(max_results or 10), 50))
        query = (query or "").strip()

        rows = self._conn.execute(
            f"""
            SELECT {self._SELECT_COLUMNS}
            FROM references_library
            ORDER BY updated_at DESC
            LIMIT 3000
            """
        ).fetchall()
        if not rows:
            return []

        if not query:
            out = [self._row_to_candidate(r) for r in rows[:max_results]]
            for cand in out:
                cand.source = "user_library"
            return out

        tokens = self._tokenize(query)
        doi_query = self._normalize_doi(query)
        pmid_query = query if query.isdigit() else ""
        pmcid_query = self._normalize_pmcid(query) if query.upper().startswith("PMC") else ""

        scored: list[tuple[float, CitationCandidate]] = []
        for row in rows:
            cand = self._row_to_candidate(row)
            cand.source = "user_library"
            score = self._score_candidate(cand, tokens, pmid_query, doi_query, pmcid_query)
            if score <= 0:
                continue
            cand.composite_score = score
            cand.score_rationale = "Matched in user library"
            scored.append((score, cand))

        scored.sort(key=lambda x: (x[0], x[1].year), reverse=True)
        return [cand for _, cand in scored[:max_results]]

    # ── Identifier lookups (author-suggested citations) ────────────────

    def _fetch_where(self, where: str, params: tuple) -> list[CitationCandidate]:
        rows = self._conn.execute(
            f"SELECT {self._SELECT_COLUMNS} FROM references_library WHERE {where} "
            "ORDER BY updated_at DESC LIMIT 50",
            params,
        ).fetchall()
        out = []
        for row in rows:
            cand = self._row_to_candidate(row)
            cand.source = "user_library"
            out.append(cand)
        return out

    def find_by_pmid(self, pmid: str) -> Optional[CitationCandidate]:
        pmid = (pmid or "").strip()
        if not pmid:
            return None
        found = self._fetch_where("pmid = ?", (pmid,))
        return found[0] if found else None

    def find_by_doi(self, doi: str) -> Optional[CitationCandidate]:
        doi = self._normalize_doi(doi)
        if not doi:
            return None
        found = self._fetch_where("lower(doi) = ?", (doi,))
        return found[0] if found else None

    def find_by_pmcid(self, pmcid: str) -> Optional[CitationCandidate]:
        pmcid = self._normalize_pmcid(pmcid)
        if not pmcid:
            return None
        found = self._fetch_where("upper(pmcid) = ?", (pmcid,))
        return found[0] if found else None

    def find_by_author_year(
        self, last_name: str, year: int, coauthor: str = "", first_author_only: bool = True,
    ) -> list[CitationCandidate]:
        """Library entries whose (first) author matches *last_name* in *year*.

        Falls back to any-author matching when nothing matches as first author.
        """
        last_name = (last_name or "").strip().lower()
        if not last_name or not year:
            return []
        rows = self._fetch_where("year = ?", (int(year),))

        def _last(a: Author) -> str:
            return (a.last_name or "").strip().lower()

        def _same(n: str, target: str) -> bool:
            return bool(n) and (n == target or n.split()[-1] == target.split()[-1])

        def _matches(cand: CitationCandidate, first_only: bool) -> bool:
            names = [n for n in (_last(a) for a in cand.authors) if n]
            if not names:
                return False
            if first_only:
                ok = _same(names[0], last_name)
            else:
                ok = any(_same(n, last_name) for n in names)
            if ok and coauthor:
                co = coauthor.strip().lower()
                ok = any(_same(n, co) for n in names)
            return ok

        found = [c for c in rows if _matches(c, True)]
        if not found and first_author_only:
            found = [c for c in rows if _matches(c, False)]
        return found

    def close(self):
        self._conn.close()

    # ------------------------------------------------------------------
    # EndNote import
    # ------------------------------------------------------------------

    def import_from_path(self, input_path: str) -> ImportReport:
        """Import an EndNote export (`.ris` or EndNote XML `.xml`)."""
        path = Path(input_path).expanduser()
        suffix = path.suffix.lower()

        if suffix == ".ris":
            return self.import_ris(str(path))
        if suffix == ".xml":
            return self.import_endnote_xml(str(path))
        if suffix == ".enl":
            report = ImportReport()
            report.errors.append(
                "EndNote .enl files are not directly readable. "
                "Export your EndNote library as RIS or EndNote XML, then import that file."
            )
            return report

        report = ImportReport()
        report.errors.append(
            f"Unsupported file type: {suffix or '(no extension)'} "
            "— expected .ris or .xml."
        )
        return report

    def import_ris(self, ris_path: str) -> ImportReport:
        report = ImportReport()
        path = Path(ris_path).expanduser()
        if not path.exists():
            report.errors.append(f"File not found: {path}")
            return report

        text = path.read_text(encoding="utf-8", errors="ignore")
        records = self._parse_ris_records(text)
        report.total_records = len(records)

        for rec in records:
            citation = self._ris_record_to_candidate(rec)
            if citation is None:
                report.skipped += 1
                continue
            inserted, updated = self.upsert_candidate(
                citation, source="endnote_import",
            )
            if inserted:
                report.imported += 1
            elif updated:
                report.updated += 1
            else:
                report.skipped += 1

        return report

    def import_endnote_xml(self, xml_path: str) -> ImportReport:
        report = ImportReport()
        path = Path(xml_path).expanduser()
        if not path.exists():
            report.errors.append(f"File not found: {path}")
            return report

        try:
            tree = ET.parse(path)
            root = tree.getroot()
        except ET.ParseError as exc:
            report.errors.append(f"Invalid XML: {exc}")
            return report

        records = root.findall(".//record")
        report.total_records = len(records)

        for record in records:
            citation = self._xml_record_to_candidate(record)
            if citation is None:
                report.skipped += 1
                continue
            inserted, updated = self.upsert_candidate(
                citation, source="endnote_import",
            )
            if inserted:
                report.imported += 1
            elif updated:
                report.updated += 1
            else:
                report.skipped += 1

        return report

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_doi(doi: str) -> str:
        if not doi:
            return ""
        doi = doi.strip()
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
        return doi.lower()

    @staticmethod
    def _normalize_pmcid(pmcid: str) -> str:
        pmcid = (pmcid or "").strip().upper()
        if not pmcid:
            return ""
        if pmcid.isdigit():
            pmcid = f"PMC{pmcid}"
        return pmcid if pmcid.startswith("PMC") and pmcid[3:].isdigit() else ""

    @staticmethod
    def _normalize_title(title: str) -> str:
        t = (title or "").lower()
        t = re.sub(r"[^a-z0-9]+", " ", t)
        return re.sub(r"\s+", " ", t).strip()

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        tokens = re.findall(r"[a-z0-9][a-z0-9.\-_/]*", text.lower())
        return [t for t in tokens if len(t) > 1]

    def _key_for_candidate(self, citation: CitationCandidate) -> str:
        if citation.pmid:
            return f"pmid:{citation.pmid.strip()}"
        doi = self._normalize_doi(citation.doi)
        if doi:
            return f"doi:{doi}"
        title_key = self._normalize_title(citation.title)
        if title_key:
            return f"title:{title_key[:240]}"
        return ""

    def _row_to_candidate(self, row) -> CitationCandidate:
        (
            _key,
            pmid,
            doi,
            title,
            authors_json,
            year,
            journal,
            journal_abbrev,
            volume,
            issue,
            pages,
            abstract,
            mesh_terms_json,
            publication_types_json,
            is_retracted,
            is_review,
            source,
            _updated_at,
            pmcid,
        ) = row

        try:
            authors_data = json.loads(authors_json or "[]")
            authors = [Author(**a) for a in authors_data]
        except Exception:
            authors = []

        try:
            mesh_terms = json.loads(mesh_terms_json or "[]")
        except Exception:
            mesh_terms = []
        try:
            publication_types = json.loads(publication_types_json or "[]")
        except Exception:
            publication_types = []

        return CitationCandidate(
            pmid=pmid or "",
            pmcid=pmcid or "",
            doi=doi or "",
            title=title or "",
            authors=authors,
            year=int(year or 0),
            journal=journal or "",
            journal_abbrev=journal_abbrev or "",
            volume=volume or "",
            issue=issue or "",
            pages=pages or "",
            abstract=abstract or "",
            mesh_terms=mesh_terms,
            publication_types=publication_types,
            is_retracted=bool(is_retracted),
            is_review=bool(is_review),
            source=source or "user_library",
        )

    def _score_candidate(
        self,
        candidate: CitationCandidate,
        tokens: list[str],
        pmid_query: str,
        doi_query: str,
        pmcid_query: str = "",
    ) -> float:
        if pmid_query and candidate.pmid == pmid_query:
            return 500.0
        if doi_query and self._normalize_doi(candidate.doi) == doi_query:
            return 500.0
        if pmcid_query and self._normalize_pmcid(candidate.pmcid) == pmcid_query:
            return 500.0

        title = (candidate.title or "").lower()
        abstract = (candidate.abstract or "").lower()
        journal = (candidate.journal or "").lower()
        authors = " ".join(a.display.lower() for a in candidate.authors)

        score = 0.0
        for tok in tokens:
            if tok in title:
                score += 12.0
            if tok in abstract:
                score += 5.0
            if tok in authors:
                score += 3.0
            if tok in journal:
                score += 2.0
            if candidate.pmid and tok == candidate.pmid.lower():
                score += 30.0
            if candidate.doi and tok in self._normalize_doi(candidate.doi):
                score += 20.0

        if candidate.year:
            score += min(max(candidate.year - 1990, 0), 40) * 0.02
        return score

    @staticmethod
    def _parse_year(text: str) -> int:
        if not text:
            return 0
        m = re.search(r"(19|20)\d{2}", text)
        return int(m.group(0)) if m else 0

    @staticmethod
    def _parse_author_name(raw: str) -> Optional[Author]:
        raw = (raw or "").strip().rstrip(".")
        if not raw:
            return None

        if "," in raw:
            last, first = [p.strip() for p in raw.split(",", 1)]
        else:
            parts = raw.split()
            if len(parts) == 1:
                last, first = parts[0], ""
            else:
                last, first = parts[-1], " ".join(parts[:-1])

        initials = "".join(w[0].upper() for w in first.split() if w and w[0].isalpha())
        return Author(last_name=last, first_name=first, initials=initials)

    def _parse_ris_records(self, text: str) -> list[dict[str, list[str]]]:
        records: list[dict[str, list[str]]] = []
        current: dict[str, list[str]] = {}

        for raw_line in text.splitlines():
            line = raw_line.rstrip("\n")
            if len(line) < 6 or line[2:6] != "  - ":
                continue
            tag = line[:2]
            value = line[6:].strip()

            if tag == "TY":
                if current:
                    records.append(current)
                current = {"TY": [value]}
                continue

            if tag == "ER":
                if current:
                    records.append(current)
                    current = {}
                continue

            current.setdefault(tag, []).append(value)

        if current:
            records.append(current)

        return records

    def _ris_record_to_candidate(
        self, rec: dict[str, list[str]]
    ) -> Optional[CitationCandidate]:
        title = self._first_nonempty(rec, ["TI", "T1", "CT"])
        doi = self._normalize_doi(self._first_nonempty(rec, ["DO"]))
        pmid = self._first_nonempty(rec, ["PM", "ID", "AN", "M1"])
        if pmid and not pmid.isdigit():
            pmid_match = re.search(r"\b\d{7,9}\b", pmid)
            pmid = pmid_match.group(0) if pmid_match else ""

        authors = []
        for raw_author in rec.get("AU", []) + rec.get("A1", []):
            parsed = self._parse_author_name(raw_author)
            if parsed:
                authors.append(parsed)

        year = self._parse_year(
            self._first_nonempty(rec, ["PY", "Y1", "Y2", "DA"])
        )
        journal = self._first_nonempty(rec, ["JO", "JF", "JA", "T2"])
        abstract = self._first_nonempty(rec, ["AB", "N2"])

        sp = self._first_nonempty(rec, ["SP"])
        ep = self._first_nonempty(rec, ["EP"])
        if sp and ep:
            pages = f"{sp}-{ep}"
        else:
            pages = sp or ep or self._first_nonempty(rec, ["PG"])

        volume = self._first_nonempty(rec, ["VL"])
        issue = self._first_nonempty(rec, ["IS"])
        pub_type = self._first_nonempty(rec, ["TY"])

        if not (title or pmid or doi):
            return None

        return CitationCandidate(
            pmid=pmid,
            doi=doi,
            title=title or (doi or pmid),
            authors=authors,
            year=year,
            journal=journal,
            journal_abbrev=journal,
            volume=volume,
            issue=issue,
            pages=pages,
            abstract=abstract,
            mesh_terms=[],
            publication_types=[pub_type] if pub_type else [],
            is_retracted=False,
            is_review=False,
            source="user_library",
        )

    def _xml_record_to_candidate(self, rec: ET.Element) -> Optional[CitationCandidate]:
        title = self._xml_first_text(
            rec,
            [
                ".//titles/title",
                ".//title",
            ],
        )
        doi = self._normalize_doi(
            self._xml_first_text(rec, [".//electronic-resource-num", ".//doi"])
        )
        pmid = self._xml_first_text(rec, [".//accession-num", ".//pmid"])
        if pmid and not pmid.isdigit():
            pmid_match = re.search(r"\b\d{7,9}\b", pmid)
            pmid = pmid_match.group(0) if pmid_match else ""

        authors = []
        for author_el in rec.findall(".//contributors/authors/author"):
            raw_author = "".join(author_el.itertext()).strip()
            parsed = self._parse_author_name(raw_author)
            if parsed:
                authors.append(parsed)

        year = self._parse_year(
            self._xml_first_text(
                rec,
                [
                    ".//dates/year",
                    ".//dates/pub-dates/date/year",
                    ".//year",
                ],
            )
        )
        journal = self._xml_first_text(
            rec, [".//periodical/full-title", ".//periodical/abbr-1", ".//secondary-title"]
        )
        abstract = self._xml_first_text(rec, [".//abstract"])
        pages = self._xml_first_text(rec, [".//pages"])
        volume = self._xml_first_text(rec, [".//volume"])
        issue = self._xml_first_text(rec, [".//number"])
        ref_type = ""
        ref_type_el = rec.find(".//ref-type")
        if ref_type_el is not None:
            ref_type = (ref_type_el.attrib.get("name") or "").strip()

        if not (title or pmid or doi):
            return None

        return CitationCandidate(
            pmid=pmid or "",
            doi=doi or "",
            title=title or (doi or pmid),
            authors=authors,
            year=year,
            journal=journal,
            journal_abbrev=journal,
            volume=volume,
            issue=issue,
            pages=pages,
            abstract=abstract,
            mesh_terms=[],
            publication_types=[ref_type] if ref_type else [],
            is_retracted=False,
            is_review=False,
            source="user_library",
        )

    @staticmethod
    def _first_nonempty(rec: dict[str, list[str]], keys: list[str]) -> str:
        for k in keys:
            vals = rec.get(k, [])
            for val in vals:
                if val and val.strip():
                    return val.strip()
        return ""

    @staticmethod
    def _xml_first_text(rec: ET.Element, paths: list[str]) -> str:
        for path in paths:
            el = rec.find(path)
            if el is None:
                continue
            text = "".join(el.itertext()).strip()
            if text:
                return text
        return ""
