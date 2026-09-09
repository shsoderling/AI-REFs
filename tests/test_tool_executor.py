"""ToolExecutor: dispatch, candidate tracking, and the full-text passages tool."""

import json

from src.services.tool_executor import ToolExecutor, fulltext_passages_for
from tests.fakes import FakeEuropePMC, FakePubMed, article

PARAGRAPHS = [("Results", "Rac1 activation increased spine density in neurons."),
              ("Methods", "Neurons were cultured for fourteen days before imaging.")]


def _executor(pubmed=None, europepmc=None, candidates=None):
    return ToolExecutor(pubmed=pubmed or FakePubMed(), europepmc=europepmc,
                        all_candidates=candidates if candidates is not None else {})


def test_fetch_articles_tracks_candidates_and_reports_full_text_availability():
    pubmed = FakePubMed(articles={"11": article("11", "Rac1", pmcid="PMC1"),
                                  "12": article("12", "No PMC")})
    seen = {}
    out = json.loads(_executor(pubmed, candidates=seen).execute("fetch_articles", {"pmids": ["11", "12"]}))
    assert [(a["pmid"], a["full_text_available"], a["pmcid"]) for a in out] == [
        ("11", True, "PMC1"), ("12", False, "")]
    assert set(seen) == {"11", "12"}


def test_unknown_tool_and_handler_errors_are_reported_as_json():
    ex = _executor()
    assert json.loads(ex.execute("nope", {}))["error"].startswith("Unknown tool")
    assert "error" in json.loads(ex.execute("fetch_articles", {}))       # KeyError inside


def test_fulltext_passages_via_known_candidate():
    epmc = FakeEuropePMC(fulltext={"PMC1": PARAGRAPHS})
    seen = {"11": article("11", "Rac1", pmcid="PMC1")}
    out = json.loads(_executor(europepmc=epmc, candidates=seen).execute(
        "get_fulltext_passages", {"pmid": "11", "keywords": ["spine density"]}))
    assert out["available"] is True and out["pmcid"] == "PMC1"
    assert out["passages"][0]["section"] == "Results"
    assert epmc.fulltext_calls == ["PMC1"]


def test_fulltext_passages_resolves_the_pmcid_through_europepmc():
    epmc = FakeEuropePMC(by_pmid={"11": article("11", "Rac1", pmcid="PMC9")},
                         fulltext={"PMC9": PARAGRAPHS})
    seen = {"11": article("11", "Rac1")}
    out = json.loads(_executor(europepmc=epmc, candidates=seen).execute(
        "get_fulltext_passages", {"pmid": "11", "keywords": ["neurons"]}))
    assert out["available"] and out["pmcid"] == "PMC9"
    assert seen["11"].pmcid == "PMC9"                    # learned for later


def test_fulltext_unavailable_paths():
    epmc = FakeEuropePMC()
    out = json.loads(_executor(europepmc=epmc).execute(
        "get_fulltext_passages", {"doi": "10.1/none", "keywords": ["x"]}))
    assert out["available"] is False and "abstract" in out["reason"]
    out = json.loads(_executor(europepmc=None).execute("get_fulltext_passages", {"keywords": ["x"]}))
    assert out["available"] is False
    assert fulltext_passages_for(article("1", "T"), ["x"], None) is None
