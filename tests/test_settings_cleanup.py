"""Dead settings and ranker leftovers are gone; old files still load."""

from src.models.citation import CitationCandidate
from src.models.project import ProjectSettings, ProjectState
from src.storage.project_io import load_project, save_project

REMOVED_SETTINGS = ("recency_weight", "max_candidates_per_sentence", "custom_csl_path",
                    "include_abstracts_in_report", "generate_ris", "generate_bibtex")
REMOVED_SCORES = ("relevance_score", "recency_score", "journal_score")


def test_removed_settings_are_not_in_the_model():
    dumped = ProjectSettings().model_dump()
    for key in REMOVED_SETTINGS:
        assert key not in dumped
    assert dumped["parallel_searches"] == 3
    assert dumped["verify_citations"] is True
    assert dumped["use_full_text"] is True


def test_old_settings_json_with_removed_keys_still_loads():
    s = ProjectSettings.model_validate({
        "recency_weight": 0.2, "max_candidates_per_sentence": 15, "generate_ris": True,
        "custom_csl_path": "/x.csl", "prefer_reviews": True,
    })
    assert s.prefer_reviews is True
    assert not hasattr(s, "recency_weight")


def test_candidate_drops_legacy_score_fields_but_loads_them():
    c = CitationCandidate.model_validate({"pmid": "1", "title": "T", "relevance_score": 1.0,
                                          "recency_score": 0.5, "journal_score": 2.0})
    dumped = c.model_dump()
    for key in REMOVED_SCORES:
        assert key not in dumped
    assert dumped["is_open_access"] is False
    assert dumped["published_doi"] == ""
    assert c.composite_score == 0.0


def test_project_round_trip_keeps_new_settings(tmp_path):
    project = ProjectState()
    project.settings.parallel_searches = 5
    project.settings.verify_citations = False
    project.settings.use_full_text = False
    path = tmp_path / "p.airefsproj"
    save_project(project, str(path))
    loaded = load_project(str(path))
    assert loaded.settings.parallel_searches == 5
    assert loaded.settings.verify_citations is False
    assert loaded.settings.use_full_text is False
