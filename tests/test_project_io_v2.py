"""Project file v2: schema version, tracking mirror, v1 upgrade (Task 19)."""
import json

from src.models.embedded import DocumentTier, TrackingReport
from src.models.project import ProjectState
from src.storage.project_io import load_project, save_project, upgrade_project_data


def test_v1_project_upgrades_and_drops_dead_fields(tmp_path):
    v1 = {"project_name": "old", "bibliography_pmids": ["1"], "pmid_to_bib_number": {"1": 1},
          "settings": {"citation_style": "nih_grant", "anthropic_api_key": "leak"},
          "sentences": [], "evidence_map": {}}
    path = tmp_path / "old.airefsproj"
    path.write_text(json.dumps(v1))
    p = load_project(str(path))
    assert p.schema_version == 2 and p.project_name == "old"
    assert not hasattr(p, "bibliography_pmids") and not hasattr(p, "pmid_to_bib_number")
    assert p.doc_id == "" and p.record_order == [] and p.doc_tracking is None


def test_upgrade_is_idempotent():
    data = {"schema_version": 2, "project_name": "x"}
    assert upgrade_project_data(dict(data)) == data


def test_v2_round_trip_keeps_tracking(tmp_path):
    p = ProjectState(doc_id="d1", record_order=["u1", "u2"], uncited=["u3"],
                     doc_tracking=TrackingReport(tier=DocumentTier.TRACKED, field_count=2))
    p.settings.anthropic_api_key = "secret"
    path = tmp_path / "p.airefsproj"
    save_project(p, str(path))
    data = json.loads(path.read_text())
    assert data["schema_version"] == 2
    assert "secret" not in path.read_text()
    q = load_project(str(path))
    assert q.doc_id == "d1" and q.record_order == ["u1", "u2"] and q.uncited == ["u3"]
    assert q.doc_tracking.tier == DocumentTier.TRACKED and q.doc_tracking.field_count == 2


def test_newer_project_file_is_refused(tmp_path):
    path = tmp_path / "future.airefsproj"
    path.write_text(json.dumps({"schema_version": 99, "project_name": "f"}))
    try:
        load_project(str(path))
    except ValueError as exc:
        assert "newer" in str(exc)
    else:
        raise AssertionError("a project file from a newer AI REFs must be refused")


def test_looks_stripped_from_path_or_entry_hashes(tmp_path):
    from src.pipeline.citation_payload import entry_hash
    from src.pipeline.docx_export import export_fresh, looks_stripped
    from src.pipeline.existing_citation_parser import ExistingCitationParser
    from src.services.docx_io import DocxHandler
    from tests.test_round_trip import BODY, make_project
    project = make_project(tmp_path, BODY)
    out = tmp_path / "out.docx"
    export_fresh(project, str(out))
    assert len(project.entry_hashes) == 3
    # strip every field, as Google Docs would, keeping the visible text
    h = DocxHandler(str(out))
    for f in list(h.fields.fields):
        for r in f.all_runs:
            if r is not None and (r.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}fldChar") is not None
                                  or r.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}instrText") is not None):
                r.getparent().remove(r)
    stripped_path = tmp_path / "from_gdocs.docx"
    h.save(str(stripped_path))
    m = ExistingCitationParser(DocxHandler(str(stripped_path))).analyze()
    assert m.tracking.field_count == 0 and sorted(m.bib_entries) == [1, 2, 3]
    assert looks_stripped(m, str(stripped_path), None, project.entry_hashes)
    assert looks_stripped(m, str(stripped_path), str(stripped_path), [])  # same file as the last export
    assert not looks_stripped(m, str(stripped_path), str(out), [])        # a different file, no hashes
    assert not looks_stripped(m, str(stripped_path), None, [entry_hash("unrelated")] * 3)
    assert not looks_stripped(m, str(stripped_path), None, [])
