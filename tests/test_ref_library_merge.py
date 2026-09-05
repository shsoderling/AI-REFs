"""Non-destructive library merge for records read back from documents (Task 18)."""
import sqlite3

from src.models.citation import Author, CitationCandidate
from src.services.ref_library import ReferenceLibrary


def _lib(tmp_path):
    return ReferenceLibrary(str(tmp_path / "lib.db"))


def _row(lib, pmid):
    conn = sqlite3.connect(lib.db_path)
    try:
        return conn.execute(
            "SELECT title, authors_json, abstract, mesh_terms_json, publication_types_json, "
            "is_retracted, is_review, source, journal_abbrev FROM references_library WHERE pmid = ?",
            (pmid,)).fetchone()
    finally:
        conn.close()


def test_merge_never_degrades_a_rich_row(tmp_path):
    lib = _lib(tmp_path)
    rich = CitationCandidate(pmid="1", title="T", abstract="ABS", mesh_terms=["M1", "M2"],
                             publication_types=["Journal Article"], journal="J",
                             authors=[Author(last_name=f"A{i}") for i in range(12)], source="pubmed")
    assert lib.upsert_candidate(rich, source="accepted_pubmed") == (True, False)
    thin = CitationCandidate(pmid="1", title="T", authors=[Author(last_name="A0")], source="embedded",
                             is_retracted=True, journal_abbrev="J Abbr")
    assert lib.upsert_candidate(thin, source="embedded", merge=True) == (False, True)
    title, authors_json, abstract, mesh, pubtypes, retracted, review, source, abbr = _row(lib, "1")
    assert abstract == "ABS" and '"M1"' in mesh and "Journal Article" in pubtypes
    assert authors_json.count("last_name") == 12
    assert retracted == 1                       # retraction propagates
    assert abbr == "J Abbr"                     # empty column filled
    assert source == "accepted_pubmed"          # a richer origin is kept


def test_merge_into_embedded_row_upgrades_it(tmp_path):
    lib = _lib(tmp_path)
    thin = CitationCandidate(pmid="2", title="T", source="embedded")
    lib.upsert_candidate(thin, source="embedded")
    rich = CitationCandidate(pmid="2", title="T full", abstract="ABS", source="pubmed",
                             authors=[Author(last_name="A"), Author(last_name="B")])
    assert lib.upsert_candidate(rich, source="accepted_pubmed", merge=True) == (False, True)
    title, authors_json, abstract, *_rest, source, _abbr = _row(lib, "2")
    assert abstract == "ABS" and authors_json.count("last_name") == 2
    assert title == "T"                         # existing non-empty text columns are kept
    assert source == "accepted_pubmed"          # 'embedded' is the weakest origin


def test_non_merge_update_still_overwrites(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert_candidate(CitationCandidate(pmid="3", title="T", abstract="old"), source="x")
    lib.upsert_candidate(CitationCandidate(pmid="3", title="T", abstract="new"), source="x")
    assert _row(lib, "3")[2] == "new"


def test_merge_insert_behaves_like_insert(tmp_path):
    lib = _lib(tmp_path)
    assert lib.upsert_candidate(CitationCandidate(pmid="4", title="T"), source="embedded", merge=True) == (True, False)
    assert lib.count() == 1
