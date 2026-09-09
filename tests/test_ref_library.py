"""Reference library: PMCID column migration and identifier lookups."""

import sqlite3

from src.models.citation import Author, CitationCandidate
from src.services.ref_library import ReferenceLibrary


OLD_SCHEMA = """
CREATE TABLE references_library (
 key TEXT PRIMARY KEY, pmid TEXT, doi TEXT, title TEXT NOT NULL, authors_json TEXT NOT NULL,
 year INTEGER NOT NULL DEFAULT 0, journal TEXT NOT NULL DEFAULT '', journal_abbrev TEXT NOT NULL DEFAULT '',
 volume TEXT NOT NULL DEFAULT '', issue TEXT NOT NULL DEFAULT '', pages TEXT NOT NULL DEFAULT '',
 abstract TEXT NOT NULL DEFAULT '', mesh_terms_json TEXT NOT NULL DEFAULT '[]',
 publication_types_json TEXT NOT NULL DEFAULT '[]', is_retracted INTEGER NOT NULL DEFAULT 0,
 is_review INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'literature',
 added_at REAL NOT NULL, updated_at REAL NOT NULL);
INSERT INTO references_library VALUES ('pmid:111','111','10.1000/ABC','Old paper',
 '[{"last_name":"Battison","first_name":"A","initials":"A"}]',2024,'J','J','','','','',
 '[]','[]',0,0,'endnote_import',1,1);
"""


def test_old_library_file_is_migrated_and_searchable(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(OLD_SCHEMA)
    conn.commit()
    conn.close()

    lib = ReferenceLibrary(str(db))
    try:
        cols = {r[1] for r in lib._conn.execute("PRAGMA table_info(references_library)")}
        assert "pmcid" in cols
        assert lib.find_by_pmid("111").title == "Old paper"
        assert lib.find_by_doi("https://doi.org/10.1000/abc").title == "Old paper"
        assert lib.find_by_pmcid("PMC1") is None
        assert [c.title for c in lib.find_by_author_year("Battison", 2024)] == ["Old paper"]
        assert [c.title for c in lib.find_by_author_year("battison", 2024)] == ["Old paper"]
        assert lib.find_by_author_year("Smith", 2024) == []
        assert lib.find_by_author_year("Battison", 2023) == []
        # search() still works on the migrated table
        assert lib.search("Old paper")[0].title == "Old paper"
    finally:
        lib.close()


def test_pmcid_roundtrip_and_author_matching(tmp_path):
    lib = ReferenceLibrary(str(tmp_path / "lib.db"))
    try:
        c = CitationCandidate(
            pmid="222", pmcid="PMC999", doi="10.1/X", title="New",
            authors=[Author(last_name="van der Berg", initials="J"), Author(last_name="Smith")],
            year=2020,
        )
        assert lib.upsert_candidate(c) == (True, False)
        assert lib.find_by_pmcid("pmc999").title == "New"
        assert lib.find_by_pmcid("999").title == "New"
        assert lib.find_by_pmid("222").pmcid == "PMC999"
        assert lib.search("PMC999")[0].title == "New"

        # first-author matching tolerates particles and surname-only queries
        assert [x.title for x in lib.find_by_author_year("Berg", 2020)] == ["New"]
        assert [x.title for x in lib.find_by_author_year("van der Berg", 2020)] == ["New"]
        # co-author filter
        assert [x.title for x in lib.find_by_author_year("van der Berg", 2020, coauthor="Smith")] == ["New"]
        assert lib.find_by_author_year("van der Berg", 2020, coauthor="Jones") == []
        # non-first author still found as a fallback
        assert [x.title for x in lib.find_by_author_year("Smith", 2020)] == ["New"]

        # Updating without a PMCID keeps the stored PMCID
        c2 = CitationCandidate(pmid="222", doi="10.1/x", title="New v2", year=2020)
        assert lib.upsert_candidate(c2) == (False, True)
        got = lib.find_by_pmid("222")
        assert got.pmcid == "PMC999" and got.title == "New v2"
    finally:
        lib.close()
