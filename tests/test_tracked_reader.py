"""Tracked reader: an exported document reopens exactly and offline (Task 16)."""
import copy

import pytest

from src.models.embedded import DocumentTier
from src.pipeline.docx_export import export_fresh
from src.pipeline.existing_citation_parser import ExistingCitationParser
from src.pipeline.renumbering import NewMarkerInfo, compute_renumbering
from src.services.docx_fields import rewrite_code, rewrite_result
from src.services.docx_io import DocxHandler
from tests.test_round_trip import BODY, C2, make_citation, make_project


@pytest.fixture
def exported(tmp_path):
    project = make_project(tmp_path, BODY)
    out = tmp_path / "out.docx"
    export_fresh(project, str(out))
    return project, out


def _forbid_network(monkeypatch):
    import src.services.pubmed_client as pm

    def boom(*a, **k):
        raise AssertionError("network call in the tracked read path")
    for name in ("search", "fetch_article", "fetch_articles", "citation_match"):
        if hasattr(pm.PubMedClient, name):
            monkeypatch.setattr(pm.PubMedClient, name, boom)


def test_reopen_is_tracked_and_exact(exported, monkeypatch):
    _forbid_network(monkeypatch)
    project, out = exported
    m = ExistingCitationParser(DocxHandler(str(out))).analyze()
    assert m.tracking.tier == DocumentTier.TRACKED
    assert m.tracking.field_count == 3 and m.tracking.record_count == 3
    assert m.tracking.bibl_field_count == 1 and m.tracking.problems == []
    assert m.tracking.doc_id == project.doc_id and m.tracking.schema_version == 1
    assert sorted(m.bib_entries) == [1, 2, 3]
    assert [m.bib_entries[n].pmid for n in (1, 2, 3)] == ["30000001", "30000002", "30000003"]
    assert m.bib_entries[2].matched_candidate.title == C2.title
    assert m.bib_entries[2].record_uuid == C2.record_uuid
    assert m.bib_entries[2].matched_candidate.record_uuid == C2.record_uuid
    assert m.bib_entries[2].item["title"] == C2.title and m.bib_entries[2].uris[0].startswith("airefs:record/")
    assert m.bib_entries[1].raw_text.startswith("1. Author1") and m.bib_entries[1].body.startswith("Author1")
    assert m.bib_entries[1].entry_hash
    cites = {p: [(c.number, c.cid != "", c.source, c.cluster_index) for c in cs]
             for p, cs in m.in_text_citations.items()}
    assert cites == {0: [(1, True, "field", 0)], 1: [(2, True, "field", 0), (3, True, "field", 1)],
                     2: [(2, True, "field", 0)]}
    assert m.in_text_citations[1][0].cid == m.in_text_citations[1][1].cid
    assert m.in_text_citations[0][0].record_uuid == m.bib_entries[1].record_uuid
    assert m.in_text_citations[0][0].char_offset == len("First finding ")
    assert m.references_heading_para_idx == 3 and m.bibliography_span == (4, 6)
    assert m.body_end_para_idx == 3
    assert m.detected_style_is_superscript is True and m.detected_style_is_author_date is False
    assert m.has_existing_citations and m.max_existing_number == 3


def test_tracked_map_numbers_agree_with_legacy_regex_map(exported):
    _, out = exported
    h = DocxHandler(str(out))
    tracked = ExistingCitationParser(h).analyze()
    legacy = ExistingCitationParser(h)._legacy_map()
    new = [NewMarkerInfo(para_index=0, char_offset=0, citations=[make_citation(9)])]
    a = compute_renumbering(tracked, new)
    b = compute_renumbering(legacy, new)
    assert a.renumber_map == b.renumber_map
    assert {n: x.original_number for n, x in a.assignments.items()} == \
        {n: x.original_number for n, x in b.assignments.items()}


def test_duplicate_cid_is_reminted_and_hand_edit_detected(exported):
    _, out = exported
    h = DocxHandler(str(out))
    f0, f1 = [f for f in h.fields.fields if f.kind == "airefs_cite"][:2]
    for r in f0.all_runs:                      # paste a copy of field 0 at the end of paragraph 2
        h.get_paragraphs()[2]._p.append(copy.deepcopy(r))
    rewrite_result(f1, "99", superscript=True)  # hand edit
    h.save(str(out))
    m = ExistingCitationParser(DocxHandler(str(out))).analyze()
    kinds = sorted(i.kind for i in m.tracking.reconcile)
    assert kinds == ["duplicate", "hand_edited"]
    cids = [c.cid for cs in m.in_text_citations.values() for c in cs]
    assert len(cids) == 5 and len(set(cids)) == 4       # the cluster of two shares one cid
    assert [c.user_edited for c in m.in_text_citations[1]] == [True, True]
    assert m.in_text_citations[2][1].number == 1        # pasted copy still cites record 1


def test_missing_bibliography_field_is_reported_not_fatal(exported):
    _, out = exported
    h = DocxHandler(str(out))
    h.remove_references_section(3, 6)
    h.save(str(out))
    m = ExistingCitationParser(DocxHandler(str(out))).analyze()
    assert m.tracking.tier == DocumentTier.TRACKED
    assert m.references_heading_para_idx == -1 and m.bibliography_span == (-1, -1)
    assert m.body_end_para_idx == -1
    assert sorted(m.bib_entries) == [1, 2, 3]
    assert m.bib_entries[1].raw_text == ""             # no rendered entry to copy
    assert any("regenerated" in p for p in m.tracking.problems)


def test_uncited_record_in_bibl_order_is_reported_or_kept(exported):
    _, out = exported
    h = DocxHandler(str(out))
    body = h.doc.element.body
    body.remove(h.get_paragraphs()[0]._p)              # record 1 loses its only citation
    h.save(str(out))
    m = ExistingCitationParser(DocxHandler(str(out))).analyze()
    assert sorted(m.bib_entries) == [1, 2] and m.bib_entries[1].pmid == "30000002"
    assert [i.kind for i in m.tracking.reconcile] == ["uncited"]
    kept = ExistingCitationParser(DocxHandler(str(out)), keep_uncited=True).analyze()
    assert sorted(kept.bib_entries) == [1, 2, 3]
    assert kept.bib_entries[3].is_uncited and kept.bib_entries[3].pmid == "30000001"
    assert kept.bib_entries[3].raw_text.startswith("1. Author1")


def test_edited_entry_is_flagged(exported):
    _, out = exported
    h = DocxHandler(str(out))
    entry_p = h.get_paragraphs()[5]                     # entry 2
    entry_p.runs[-1].text = entry_p.runs[-1].text + " [corrected volume]"
    h.save(str(out))
    m = ExistingCitationParser(DocxHandler(str(out))).analyze()
    assert [i.kind for i in m.tracking.reconcile] == ["entry_edited"]
    assert m.bib_entries[2].raw_text.endswith("[corrected volume]")


def test_newer_schema_is_read_only(exported):
    _, out = exported
    h = DocxHandler(str(out))
    f = [f for f in h.fields.fields if f.kind == "airefs_cite"][0]
    rewrite_code(f, f.code.replace('"v":1', '"v":99'))
    h.save(str(out))
    m = ExistingCitationParser(DocxHandler(str(out))).analyze()
    assert m.tracking.tier == DocumentTier.NEWER_VERSION and not m.has_existing_citations


def test_damaged_field_is_skipped_and_counted(exported):
    _, out = exported
    h = DocxHandler(str(out))
    f = [f for f in h.fields.fields if f.kind == "airefs_cite"][0]
    rewrite_code(f, " ADDIN AIREFS.CITE {broken ")
    h.save(str(out))
    m = ExistingCitationParser(DocxHandler(str(out))).analyze()
    assert m.tracking.tier == DocumentTier.TRACKED
    assert sorted(m.bib_entries) == [1, 2] and m.bib_entries[1].pmid == "30000002"
    assert any("damaged" in i.kind for i in m.tracking.reconcile)


def test_field_in_table_is_reported_not_numbered(exported):
    _, out = exported
    h = DocxHandler(str(out))
    f0 = [f for f in h.fields.fields if f.kind == "airefs_cite"][0]
    cell = h.doc.add_table(rows=1, cols=1).cell(0, 0).paragraphs[0]
    for r in f0.all_runs:
        cell._p.append(copy.deepcopy(r))
    h.save(str(out))
    m = ExistingCitationParser(DocxHandler(str(out))).analyze()
    assert m.tracking.fields_in_tables == 1 and m.tracking.field_count == 4
    assert sum(len(v) for v in m.in_text_citations.values()) == 4
    assert [i.kind for i in m.tracking.reconcile] == ["table"]
