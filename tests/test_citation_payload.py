"""AIREFS.CITE / AIREFS.BIBL payload build and parse (Task 12)."""
import json

import pytest

from src.models.citation import Author, CitationCandidate
from src.pipeline.citation_payload import (
    FIELD_SCHEMA_VERSION, NewerSchemaError, PayloadError, build_bibl_code, build_cite_code,
    entry_hash, extract_json, is_bibl_code, is_cite_code, mint_citation_id, parse_bibl_code,
    parse_cite_code,
)

BASE32 = set("abcdefghijklmnopqrstuvwxyz234567")


def cand(i, **kw):
    return CitationCandidate(pmid=f"100{i}", doi=f"10.1/x{i}", title=f"Title <{i}> & co",
                             authors=[Author(last_name=f"A{i}", initials="B")], year=2000 + i, **kw)


def test_cite_code_shape_and_round_trip():
    code = build_cite_code([cand(1), cand(2)], numbers=[3, 5], render="numeric-superscript",
                           style="nih_grant", plain="3,5")
    assert code.startswith(" ADDIN AIREFS.CITE {") and code.endswith("} ")
    assert is_cite_code(code) and not is_bibl_code(code)
    p = parse_cite_code(code)
    assert len(p.citation_id) == 12 and set(p.citation_id) <= BASE32
    assert p.numbers == [3, 5] and p.render == "numeric-superscript" and p.style == "nih_grant"
    assert p.plain == "3,5" and p.unresolved is False and p.version == FIELD_SCHEMA_VERSION
    assert [it.identity["pmid"] for it in p.items] == ["1001", "1002"]
    assert p.items[0].item["title"] == "Title <1> & co"
    assert p.items[0].record_uuid and p.items[0].uris[0] == f"airefs:record/{p.items[0].record_uuid}"
    assert p.problems == []


def test_build_mints_record_uuids_on_the_candidates():
    c = cand(1)
    build_cite_code([c], numbers=[1], render="numeric-bracket", style="ieee", plain="[1]")
    assert len(c.record_uuid) == 36


def test_citation_id_is_preserved_and_mintable():
    code = build_cite_code([cand(1)], numbers=[1], render="numeric-bracket", style="ieee",
                           plain="[1]", citation_id="abcdefghijkl")
    assert parse_cite_code(code).citation_id == "abcdefghijkl"
    ids = {mint_citation_id() for _ in range(50)}
    assert len(ids) == 50 and all(len(i) == 12 and set(i) <= BASE32 for i in ids)


def test_unresolved_field_has_no_items():
    code = build_cite_code([], numbers=[], render="numeric-bracket", style="ieee", plain="[?]",
                           unresolved=True)
    p = parse_cite_code(code)
    assert p.unresolved and p.items == []


def test_extract_json_is_lenient_and_unknown_keys_ignored():
    code = ' ADDIN AIREFS.CITE   {"citationID":"x","future":{"a":[1]},"citationItems":[]}  junk '
    assert json.loads(extract_json(code))["future"] == {"a": [1]}
    p = parse_cite_code(code)
    assert p.citation_id == "x" and p.items == []


def test_missing_citation_id_is_minted_with_problem():
    p = parse_cite_code(' ADDIN AIREFS.CITE {"citationItems":[]} ')
    assert len(p.citation_id) == 12 and p.problems


def test_missing_record_id_is_minted_with_problem():
    p = parse_cite_code(' ADDIN AIREFS.CITE {"citationID":"x","citationItems":[{"itemData":{"title":"t"}}]} ')
    assert len(p.items[0].record_uuid) == 36 and p.problems


def test_newer_schema_raises():
    with pytest.raises(NewerSchemaError):
        parse_cite_code(' ADDIN AIREFS.CITE {"citationID":"x","airefs":{"v":99}} ')


def test_garbage_raises_payload_error():
    with pytest.raises(PayloadError):
        parse_cite_code(" ADDIN AIREFS.CITE nothing here ")
    with pytest.raises(PayloadError):
        parse_cite_code(" ADDIN AIREFS.CITE {not json} ")


def test_size_safeguard_truncates_authors():
    big = cand(1)
    big.authors = [Author(last_name=f"Consortium member {k}", first_name="Firstname " * 20)
                   for k in range(400)]
    p = parse_cite_code(build_cite_code([big], numbers=[1], render="numeric-bracket",
                                        style="ieee", plain="[1]"))
    item = p.items[0].item
    assert len(item["author"]) == 30
    assert item["custom"]["airefs"]["authorsTruncated"] is True
    assert item["custom"]["airefs"]["authorCount"] == 400


def test_bibl_code_round_trip():
    code = build_bibl_code(doc_id="d1", style="nih_grant", render="numeric-superscript",
                           heading_text="References", order=["u1", "u2"], uncited=["u3"],
                           entry_hashes=[entry_hash("1. a"), entry_hash("2. b")])
    assert is_bibl_code(code) and not is_cite_code(code)
    b = parse_bibl_code(code)
    assert (b.doc_id, b.style, b.render, b.heading_text) == (
        "d1", "nih_grant", "numeric-superscript", "References")
    assert b.order == ["u1", "u2"] and b.uncited == ["u3"] and len(b.entry_hashes) == 2
    assert b.version == FIELD_SCHEMA_VERSION and b.exported_at.endswith("+00:00")
    assert entry_hash(" 1. a ") == entry_hash("1. a") != entry_hash("2. b")


def test_bibl_newer_schema_and_garbage():
    with pytest.raises(NewerSchemaError):
        parse_bibl_code(' ADDIN AIREFS.BIBL {"v":99} ')
    with pytest.raises(PayloadError):
        parse_bibl_code(' ADDIN AIREFS.BIBL ')
