"""CitationCandidate <-> CSL-JSON mapping, record uuids and identity URIs (Task 11)."""
import uuid

from src.models.citation import Author, CitationCandidate
from src.pipeline.csl_mapping import (
    build_uris, ensure_record_uuid, from_csl_item, identity_from_uris,
    normalize_doi, to_csl_item,
)


def full_candidate():
    return CitationCandidate(
        pmid="31978345", doi="10.1016/J.CELL.2020.01.001", pmcid="PMC7000000",
        title="Synaptic proteomes <in> vivo & beyond — ünïcode", source="pubmed",
        authors=[Author(last_name="Smith", first_name="Jane", initials="J"),
                 Author(last_name="Doe", first_name="", initials="AB")],
        year=2020, journal="Cell", journal_abbrev="Cell", volume="180", issue="2", pages="1-10",
        abstract="SHOULD NOT TRAVEL", mesh_terms=["Synapses", "Proteomics"],
        publication_types=["Journal Article", "Review"], is_retracted=True, is_review=True,
        retraction_notice="Retracted 2021", has_erratum=True, record_uuid=str(uuid.uuid4()),
    )


def test_round_trip_is_lossless_except_abstract_and_scores():
    c = full_candidate()
    c.composite_score = 0.9
    back = from_csl_item(to_csl_item(c))
    # DOIs travel normalised (bare, lowercase); everything else is byte-identical
    expect = c.model_copy(update={"abstract": "", "composite_score": 0.0,
                                 "doi": normalize_doi(c.doi)})
    back.source = c.source
    assert back.model_dump() == expect.model_dump()


def test_item_shape_and_no_abstract():
    item = to_csl_item(full_candidate())
    assert item["type"] == "article-journal"
    assert item["DOI"] == "10.1016/j.cell.2020.01.001"
    assert item["issued"] == {"date-parts": [[2020]]}
    assert item["author"][0] == {"family": "Smith", "given": "Jane"}
    assert item["author"][1] == {"family": "Doe", "given": "AB"}
    assert "abstract" not in item
    assert item["custom"]["airefs"]["authorCount"] == 2
    assert item["custom"]["airefs"]["retracted"] is True
    assert item["custom"]["airefs"]["authorsTruncated"] is False


def test_empty_fields_are_omitted():
    item = to_csl_item(CitationCandidate(title="T", record_uuid="u"))
    assert set(item) == {"id", "type", "title", "custom"}
    assert "issued" not in item


def test_unknown_keys_and_missing_fields_are_tolerated():
    c = from_csl_item({"id": "x", "type": "article-journal", "title": "T", "future": 1,
                       "custom": {"airefs": {"newKey": True}}})
    assert c.title == "T" and c.year == 0 and c.authors == [] and c.source == "embedded"
    assert c.record_uuid == "x"


def test_uris_order_and_parse():
    c = full_candidate()
    uris = build_uris(c)
    assert uris[0] == f"airefs:record/{c.record_uuid}"
    assert uris[1] == "https://doi.org/10.1016/j.cell.2020.01.001"
    assert uris[2] == "https://pubmed.ncbi.nlm.nih.gov/31978345/"
    assert uris[3] == "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7000000/"
    assert identity_from_uris(uris) == {
        "record_uuid": c.record_uuid, "doi": "10.1016/j.cell.2020.01.001",
        "pmid": "31978345", "pmcid": "PMC7000000"}


def test_hash_uri_only_without_ids():
    c = CitationCandidate(title="Only a title", year=1999)
    ensure_record_uuid(c)
    uris = build_uris(c)
    assert len(uris) == 2 and uris[1].startswith("airefs:hash/")
    assert build_uris(CitationCandidate(title="x", doi="10.1/y", record_uuid="u"))[1].startswith("https://doi.org/")
    assert identity_from_uris(["airefs:hash/abc"]) == {"record_uuid": "", "doi": "", "pmid": "", "pmcid": ""}


def test_normalize_doi():
    assert normalize_doi("https://doi.org/10.1/ABC") == "10.1/abc"
    assert normalize_doi("http://dx.doi.org/10.1/ABC") == "10.1/abc"
    assert normalize_doi("doi:10.1/ABC") == "10.1/abc"
    assert normalize_doi(" 10.1/x ") == "10.1/x"
    assert normalize_doi("") == ""


def test_ensure_record_uuid_is_stable():
    c = CitationCandidate(title="t")
    u = ensure_record_uuid(c)
    assert ensure_record_uuid(c) == u and len(u) == 36
    assert ensure_record_uuid(CitationCandidate(title="t", record_uuid="keep")) == "keep"
