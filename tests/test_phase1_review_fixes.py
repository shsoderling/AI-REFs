"""Regressions from the Phase 1 review."""
import copy

import pytest

from src.models.embedded import DocumentTier, TrackingReport
from src.models.evidence import ReviewDecision
from src.models.existing_refs import ExistingCitationMap
from src.models.project import CitationStyle, ProjectSettings, ProjectState
from src.pipeline.citation_payload import parse_cite_code
from src.pipeline.docx_export import (
    ExportBlocked, ExportDecisions, check_export_guard, export_fresh, export_legacy, export_tracked,
)
from src.pipeline.existing_citation_parser import ExistingCitationParser
from src.services.docx_fields import rewrite_code
from src.services.docx_io import DocxHandler
from tests.fixture_builders import DocBuilder, make_run
from tests.test_legacy_adoption import _legacy_doc, _project as legacy_project
from tests.test_round_trip import BODY, make_citation, make_project
from tests.test_tracked_round_trip import (
    _add_marker, _bib_texts, _cite_fields, _numbers, _paras, _reopen, _results,
)


@pytest.fixture
def session1(tmp_path):
    project = make_project(tmp_path, BODY)
    out = tmp_path / "s1.docx"
    export_fresh(project, str(out))
    return project, out


def _split_every_result_run(path):
    """Simulate Word re-splitting each field result into two runs."""
    h = DocxHandler(str(path))
    for f in _cite_fields(path):
        pass
    for f in [f for f in h.fields.fields if f.kind == "airefs_cite"]:
        r = f.result_runs[0]
        r.addnext(make_run("", superscript=True))
    h.save(str(path))


# C1 ─────────────────────────────────────────────────────────────────
def test_split_result_runs_then_new_marker_exports(session1, tmp_path):
    project, out = session1
    _split_every_result_run(out)
    sent, ev = _add_marker(out, "New first claim (REF).", "S001", [make_citation(9)], 0)
    p2 = _reopen(project, out)
    p2.sentences = [sent]
    p2.evidence_map = {sent.id: ev}
    out2 = tmp_path / "s2.docx"
    for _ in range(3):                       # the stale-index failure was probabilistic
        export_tracked(copy.deepcopy(p2), str(out2))
    assert _numbers(out2) == [[1], [2], [3, 4], [3]]
    assert all(len(f.result_runs) == 1 for f in _cite_fields(out2))


# C2 ─────────────────────────────────────────────────────────────────
def test_unresolved_field_never_consumes_a_number(session1, tmp_path):
    project, out = session1
    sent, ev = _add_marker(out, "Pending (REF).", "S001", [make_citation(9)], 0)
    ev.review_decision = ReviewDecision.REJECTED
    p2 = _reopen(project, out)
    p2.sentences = [sent]
    p2.evidence_map = {sent.id: ev}
    path = tmp_path / "s2.docx"
    export_tracked(p2, str(path))
    for i in range(3):
        nxt = tmp_path / f"s{i + 3}.docx"
        export_tracked(_reopen(project, path), str(nxt))
        path = nxt
    assert _results(path) == ["[?]", "1", "2,3", "2"]
    assert _bib_texts(path)[0].startswith("1. Author1") and len(_bib_texts(path)) == 3


# I2 ─────────────────────────────────────────────────────────────────
def test_unresolved_field_is_filled_when_user_types_a_marker(session1, tmp_path):
    project, out = session1
    sent, ev = _add_marker(out, "Pending (REF).", "S001", [make_citation(9)], 0)
    ev.review_decision = ReviewDecision.REJECTED
    p2 = _reopen(project, out)
    p2.sentences = [sent]
    p2.evidence_map = {sent.id: ev}
    unresolved = tmp_path / "unresolved.docx"
    export_tracked(p2, str(unresolved))
    # The user replaces [?] with (REF) inside the field, as the app tells them to
    h = DocxHandler(str(unresolved))
    f = [f for f in h.fields.fields if f.kind == "airefs_cite"][0]
    from src.services.docx_fields import rewrite_result
    rewrite_result(f, "(REF)", superscript=False)
    h.save(str(unresolved))
    assert len(DocxHandler(str(unresolved)).find_markers()) == 1
    sent2, ev2 = _add_marker(unresolved, "", "S001", [make_citation(9)], 0)   # evidence only
    h = DocxHandler(str(unresolved))
    h.doc.element.body.remove(h.get_paragraphs()[0]._p)                        # drop the helper paragraph
    h.save(str(unresolved))
    p3 = _reopen(project, unresolved)
    p3.sentences = [sent2.model_copy(update={"paragraph_index": 0, "raw_text": "Pending (REF)."})]
    p3.evidence_map = {"S001": ev2}
    filled = tmp_path / "filled.docx"
    stats = export_tracked(p3, str(filled))
    assert stats.fields_written == 1 and stats.unresolved_fields == 0
    assert _results(filled) == ["1", "2", "3,4", "3"]
    assert _paras(filled)[0] == "Pending 1."


# I1 ─────────────────────────────────────────────────────────────────
def test_newer_version_document_is_read_only(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out))
    f = _cite_fields(out)[0]
    f = [f for f in h.fields.fields if f.kind == "airefs_cite"][0]
    rewrite_code(f, f.code.replace('"v":1', '"v":99'))
    h.save(str(out))
    m = ExistingCitationParser(DocxHandler(str(out))).analyze()
    assert m.tracking.tier == DocumentTier.NEWER_VERSION
    for mode in ("fresh", "legacy", "tracked"):
        assert check_export_guard(m, mode, ProjectSettings(), allow_fresh_append=True)


# I3 ─────────────────────────────────────────────────────────────────
def test_damaged_field_blocks_export_with_a_clear_reason(session1, tmp_path):
    project, out = session1
    h = DocxHandler(str(out))
    f = [f for f in h.fields.fields if f.kind == "airefs_cite"][0]
    rewrite_code(f, " ADDIN AIREFS.CITE {broken ")
    h.save(str(out))
    p2 = _reopen(project, out)
    with pytest.raises(ExportBlocked) as exc:
        export_tracked(p2, str(tmp_path / "x.docx"))
    assert "damaged" in str(exc.value)


# I4 / I5 ─────────────────────────────────────────────────────────────
def test_adoption_is_all_or_nothing(tmp_path):
    b = DocBuilder()
    p = b.paragraph("Claim [1] and [9]. New (REF).")
    b.paragraph("References")
    b.paragraph("1. Author1 A. Old title. J. 2010. PMID: 2001")
    path = b.save(tmp_path / "partial.docx")
    proj = legacy_project(path, [make_citation(9)])
    proj.settings.min_match_ratio = 0.0
    out = tmp_path / "out.docx"
    stats = export_legacy(proj, str(out), ExportDecisions())
    assert stats.legacy_adopted == -1 and stats.unadoptable_sites == 1
    assert DocxHandler(str(out)).fields.airefs_cite == 0
    assert _paras(out)[0] == "Claim [1] and [9]. New [2]."


def test_split_superscript_run_is_read_and_adopted_as_one_group(tmp_path):
    b = DocBuilder()
    p = b.paragraph("Claim")
    b.add_text(p, "1-", superscript=True)
    b.add_text(p, "3", superscript=True)
    b.add_text(p, ". New (REF).")
    b.paragraph("References")
    for i in (1, 2, 3):
        b.paragraph(f"{i}. Author{i} A. Old title {i}. J. 2010. PMID: 200{i}")
    path = b.save(tmp_path / "split.docx")
    m = ExistingCitationParser(DocxHandler(str(path))).analyze()
    assert sorted(c.number for c in m.in_text_citations[0]) == [1, 2, 3]
    proj = legacy_project(path, [make_citation(9)])
    out = tmp_path / "out.docx"
    stats = export_legacy(proj, str(out), ExportDecisions())
    assert stats.legacy_adopted == 1
    payloads = [parse_cite_code(f.code) for f in _cite_fields(out)]
    assert payloads[0].numbers == [1, 2, 3] and _results(out)[0] == "1-3"
    assert _paras(out)[0] == "Claim1-3. New 4."


# I6 ─────────────────────────────────────────────────────────────────
def test_noop_export_leaves_codes_byte_identical(session1, tmp_path):
    project, out = session1
    before = [f.code for f in _cite_fields(out)]
    out2 = tmp_path / "s2.docx"
    export_tracked(_reopen(project, out), str(out2))
    assert [f.code for f in _cite_fields(out2)] == before
    p = parse_cite_code(before[0])
    assert p.items[0].item["custom"]["airefs"]["source"] == "pubmed"


def test_truncated_author_list_survives_re_export(tmp_path):
    from src.models.citation import Author, CitationCandidate
    big = make_citation(1)
    big.authors = [Author(last_name=f"Consortium member {k}", first_name="Firstname " * 20)
                   for k in range(400)]
    project = make_project(tmp_path, [("Claim (REF).", [[big]])])
    out = tmp_path / "big.docx"
    export_fresh(project, str(out))
    out2 = tmp_path / "big2.docx"
    export_tracked(_reopen(project, out), str(out2))
    item = parse_cite_code(_cite_fields(out2)[0].code).items[0].item
    assert item["custom"]["airefs"]["authorsTruncated"] is True
    assert item["custom"]["airefs"]["authorCount"] == 400 and len(item["author"]) == 30


# I8 ─────────────────────────────────────────────────────────────────
def test_adjacent_fields_are_merged_into_one_cluster(tmp_path):
    body = [("Claim (REF)(REF) and (REF).", [[make_citation(1)], [make_citation(2)], [make_citation(3)]])]
    project = make_project(tmp_path, body)
    out = tmp_path / "adj.docx"
    export_fresh(project, str(out))
    out2 = tmp_path / "adj2.docx"
    stats = export_tracked(_reopen(project, out), str(out2))
    assert stats.duplicates_merged == 1
    assert _numbers(out2) == [[1, 2], [3]] and _results(out2) == ["1,2", "3"]
    m = ExistingCitationParser(DocxHandler(str(out2))).analyze()
    assert m.tracking.reconcile == [] and m.tracking.field_count == 2
