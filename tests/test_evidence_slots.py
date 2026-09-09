"""EvidenceRecord slot bookkeeping and legacy project loading."""

import json

from src.models.citation import CitationCandidate
from src.models.evidence import EvidenceRecord, ReviewDecision
from src.models.markers import MarkerType
from src.models.project import ProjectState
from src.models.sentence import SentenceRecord


def cand(pmid):
    return CitationCandidate(pmid=pmid, title=f"Paper {pmid}")


def test_slot_ranges_single_marker_owns_everything():
    ev = EvidenceRecord(selected=[cand("1"), cand("2"), cand("3")])
    assert ev.slot_ranges(1) == [(0, 3)]
    assert ev.citations_for_slot(0, 1) == ev.selected


def test_slot_ranges_legacy_multi_ref_one_each():
    ev = EvidenceRecord(selected=[cand("1"), cand("2")])
    assert ev.slot_ranges(2) == [(0, 1), (1, 2)]
    ev = EvidenceRecord(selected=[cand("1")])
    assert ev.slot_ranges(2) == [(0, 1), (1, 1)]
    assert ev.citations_for_slot(1, 2) == []
    ev = EvidenceRecord(selected=[cand("1"), cand("2"), cand("3")])
    # surplus goes to the last occupied slot
    assert ev.slot_ranges(2) == [(0, 1), (1, 3)]


def test_explicit_slot_sizes_are_respected():
    ev = EvidenceRecord(selected=[cand("1"), cand("2"), cand("3")], slot_sizes=[1, 2])
    assert ev.slot_ranges(2) == [(0, 1), (1, 3)]
    ev.slot_sizes = [0, 3]
    assert ev.slot_ranges(2) == [(0, 0), (0, 3)]


def test_remove_and_insert_keep_slots_consistent():
    ev = EvidenceRecord(selected=[cand("1"), cand("2"), cand("3")], slot_sizes=[1, 2])
    removed = ev.remove_selected(0, 2)
    assert removed.pmid == "1"
    assert ev.slot_sizes == [0, 2]
    assert ev.citations_for_slot(0, 2) == [] and [c.pmid for c in ev.citations_for_slot(1, 2)] == ["2", "3"]

    idx = ev.insert_into_slot(0, cand("9"), 2)
    assert idx == 0 and ev.slot_sizes == [1, 2]
    assert [c.pmid for c in ev.selected] == ["9", "2", "3"]
    assert ev.slot_of_index(2, 2) == 1
    assert ev.remove_selected(99, 2) is None


def test_is_resolved_includes_skipped():
    ev = EvidenceRecord(review_decision=ReviewDecision.SKIPPED)
    assert ev.is_resolved and ev.is_skipped
    assert not EvidenceRecord(review_decision=ReviewDecision.REJECTED).is_resolved
    assert not EvidenceRecord().is_resolved


def test_legacy_sentence_backfills_ref_markers():
    s = SentenceRecord(id="S001", raw_text="Claim (REF) and more (REF).", clean_text="Claim and more.",
                       marker_type=MarkerType.REF, marker_count=2)
    assert [m.text for m in s.markers] == ["(REF)", "(REF)"]
    assert s.marker_label() == "(2 markers)"
    # Suggested-looking text in an old record is NOT reinterpreted
    s = SentenceRecord(id="S002", raw_text="Claim (PMID: 1) (REFS).", marker_type=MarkerType.REFS, marker_count=1)
    assert [m.text for m in s.markers] == ["(REFS)"]
    assert s.marker_label() == "(REFS)"


def test_legacy_project_json_loads(tmp_path):
    """A project file written before this feature loads and exports the same slots."""
    data = {
        "project_name": "old",
        "sentences": [
            {"id": "S001", "paragraph_index": 0, "sentence_index": 0,
             "raw_text": "A (REF) and B (REF).", "clean_text": "A and B.",
             "marker_type": "REF", "marker_count": 2},
        ],
        "evidence_map": {
            "S001": {"sentence_id": "S001",
                     "selected": [{"pmid": "1", "title": "One"}],
                     "review_decision": "accepted"},
        },
    }
    path = tmp_path / "old.airefsproj"
    path.write_text(json.dumps(data))
    from src.storage.project_io import load_project
    project = load_project(str(path))
    s = project.sentences[0]
    assert len(s.markers) == 2
    ev = project.evidence_map["S001"]
    assert ev.slot_sizes == []
    assert ev.slot_ranges(2) == [(0, 1), (1, 1)]
    assert project.settings.detect_suggested_ids is True

    from src.pipeline.export_slots import collect_marker_slots, ACTION_CITE, ACTION_UNRESOLVED
    slots = collect_marker_slots(project)[0]
    assert [x.action for x in slots] == [ACTION_CITE, ACTION_UNRESOLVED]
    assert [c.pmid for c in slots[0].citations] == ["1"]


def test_project_marker_config_follows_settings():
    p = ProjectState()
    assert p.marker_config.detect_ids and p.marker_config.detect_author_year
    p.settings.detect_author_year = False
    assert not p.marker_config.detect_author_year


def test_legacy_project_exports_with_legacy_grammar(tmp_path):
    """An old project file parsed only (REF)/(REFS); export must not touch other parentheticals."""
    from docx import Document
    from src.models.markers import MarkerType
    from src.pipeline.export_slots import collect_marker_slots, slot_for_docx_marker, ACTION_CITE
    from src.services.docx_io import DocxHandler

    docx_path = tmp_path / "in.docx"
    doc = Document()
    doc.add_paragraph("Spines grow after LTP (Smith 2020). Rac1 drives this (REF).")
    doc.save(str(docx_path))

    data = {
        "input_docx_path": str(docx_path),
        "sentences": [
            {"id": "S001", "paragraph_index": 0, "sentence_index": 0,
             "raw_text": "Spines grow after LTP (Smith 2020).", "clean_text": "Spines grow after LTP (Smith 2020)."},
            {"id": "S002", "paragraph_index": 0, "sentence_index": 1,
             "raw_text": "Rac1 drives this (REF).", "clean_text": "Rac1 drives this.",
             "marker_type": "REF", "marker_count": 1},
        ],
        "evidence_map": {"S002": {"sentence_id": "S002", "selected": [{"pmid": "1", "title": "One"}],
                                  "review_decision": "accepted"}},
    }
    path = tmp_path / "old.airefsproj"
    path.write_text(json.dumps(data))
    from src.storage.project_io import load_project
    project = load_project(str(path))

    assert project.run_marker_config is None
    cfg = project.export_marker_config
    assert not cfg.detect_ids and not cfg.detect_author_year
    markers = DocxHandler(str(docx_path)).find_markers(cfg)
    assert [m["text"] for m in markers] == ["(REF)"]
    slots = collect_marker_slots(project)
    slot = slot_for_docx_marker(slots, 0, 0, "(REF)")
    assert slot is not None and slot.action == ACTION_CITE
    # A DOCX marker whose text disagrees with the slot is never matched
    assert slot_for_docx_marker(slots, 0, 0, "(Smith 2020)") is None
