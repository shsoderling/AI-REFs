"""LLMCitationAgent.evaluate_suggested with a fake Anthropic client."""

import json
from types import SimpleNamespace

from src.models.citation import Author, CitationCandidate
from src.models.evidence import ConfidenceLevel
from src.models.markers import MarkerType, SuggestedCitation, SuggestionKind
from src.models.sentence import SentenceRecord
from src.pipeline.llm_citation_agent import LLMCitationAgent
from src.services.suggestion_resolver import ResolvedSuggestion
from src.utils.markers import find_markers


class FakePubMed:
    def __init__(self, articles=None):
        self.articles = articles or {}

    def fetch_articles(self, pmids):
        return [self.articles[p] for p in pmids if p in self.articles]

    def fetch_article(self, pmid):
        return self.articles.get(pmid)

    def search(self, query, max_results=20):
        return [], 0


def text_response(text):
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])


def make_agent(responses, articles=None):
    agent = LLMCitationAgent(anthropic_api_key="sk-test", model="m", pubmed_client=FakePubMed(articles))
    agent.client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: responses.pop(0)))
    return agent


def cand(pmid="", doi="", title="T", authors=(), year=2024, retracted=False):
    return CitationCandidate(pmid=pmid, doi=doi, title=title, year=year, is_retracted=retracted,
                             authors=[Author(last_name=a) for a in authors], abstract="abs")


def sentence(text):
    s = SentenceRecord(id="S001", raw_text=text, clean_text=text.split(" (")[0] + ".")
    s.markers = find_markers(text)
    s.marker_type = s.markers[0].kind
    s.marker_count = len(s.markers)
    return s


def answer(selected, **extra):
    data = {
        "selected": selected,
        "unresolved": extra.pop("unresolved", []),
        "alternatives": extra.pop("alternatives", []),
        "confidence": extra.pop("confidence", "HIGH"),
        "confidence_score": extra.pop("confidence_score", 90),
        "confidence_rationale": "because",
        "verification_status": "verified",
        "supporting_snippets": ["Spines grow."],
        "search_queries_used": [],
        "all_pmids_considered": [],
    }
    data.update(extra)
    return text_response(json.dumps(data))


def test_identifier_suggestions_are_scored_and_pinned():
    s = sentence("Spines grow during LTP (PMID: 1; doi: 10.1000/b).")
    marker = s.markers[0]
    a = cand(pmid="1", title="Paper A", authors=["Battison"])
    b = cand(pmid="2", doi="10.1000/b", title="Paper B", authors=["Smith"])
    other = cand(pmid="99", title="Substitute", authors=["Other"])
    resolved = [
        ResolvedSuggestion(marker.suggestions[0], [a], source="pubmed"),
        ResolvedSuggestion(marker.suggestions[1], [b], source="pubmed"),
    ]
    # The agent scores A, but tries to swap B for a different paper
    agent = make_agent([answer([
        {"suggestion": "PMID: 1", "pmid": "1", "score": 85, "why": "directly shows it"},
        {"suggestion": "doi: 10.1000/b", "pmid": "99", "score": 95, "why": "better"},
    ])], articles={"99": other})

    ev = agent.evaluate_suggested(s, marker, resolved)
    assert [c.pmid for c in ev.selected] == ["1", "2"]     # B kept, substitute rejected
    assert ev.slot_sizes == [2]
    assert ev.selected[0].composite_score == 85
    assert "Author-suggested (PMID 1): directly shows it" == ev.selected[0].score_rationale
    assert ev.selected[1].composite_score == 0.0
    assert any(w.code == "suggested_unscored" for w in ev.warnings)
    assert ev.confidence_level == ConfidenceLevel.MEDIUM   # unscored suggestion caps HIGH
    assert ev.abstract_snippets == ["Spines grow."]


def test_weak_match_warning_and_alternative():
    s = sentence("Claim (PMID: 1).")
    marker = s.markers[0]
    a = cand(pmid="1", title="Paper A")
    alt = cand(pmid="5", title="Alternative")
    resolved = [ResolvedSuggestion(marker.suggestions[0], [a], source="pubmed")]
    agent = make_agent([answer(
        [{"suggestion": "PMID: 1", "pmid": "1", "score": 20, "why": "unrelated"}],
        alternatives=[{"pmid": "5", "why": "this one fits"}],
        confidence="LOW", confidence_score=25,
    )], articles={"5": alt})
    ev = agent.evaluate_suggested(s, marker, resolved)
    assert [c.pmid for c in ev.selected] == ["1"]
    codes = [w.code for w in ev.warnings]
    assert "suggested_weak_match" in codes and "suggested_alternative" in codes
    assert any(c.pmid == "5" for c in ev.candidates)
    assert ev.confidence_level == ConfidenceLevel.LOW


def test_author_year_pick_is_validated_and_ambiguity_flagged():
    s = sentence("Claim (Battison et al. 2024).")
    marker = s.markers[0]
    good = cand(pmid="1", title="Good", authors=["Battison", "Smith"], year=2024)
    also = cand(pmid="2", title="Also", authors=["Battison"], year=2024)
    wrong = cand(pmid="3", title="Wrong author", authors=["Jones"], year=2024)
    resolved = [ResolvedSuggestion(marker.suggestions[0], [good, also], source="pubmed", total_matches=2)]

    agent = make_agent([answer([{"suggestion": "Battison et al. 2024", "pmid": "2", "score": 80, "why": "topic fits"}])])
    ev = agent.evaluate_suggested(s, marker, resolved)
    assert [c.pmid for c in ev.selected] == ["2"]
    assert any(w.code == "suggested_ambiguous" for w in ev.warnings)
    assert ev.confidence_level == ConfidenceLevel.MEDIUM

    # A pick whose authors do not match is discarded -> unresolved
    agent = make_agent([answer([{"suggestion": "Battison et al. 2024", "pmid": "3", "score": 80, "why": "?"}])],
                       articles={"3": wrong})
    ev = agent.evaluate_suggested(s, marker, resolved)
    assert ev.selected == []
    assert any(w.code == "suggested_unresolved" for w in ev.warnings)
    assert ev.retrieval_error


def test_duplicate_identifiers_cited_once_and_retraction_flagged():
    s = sentence("Claim (PMID: 1, doi: 10.1000/a).")
    marker = s.markers[0]
    a = cand(pmid="1", doi="10.1000/a", title="Same paper", retracted=True)
    resolved = [
        ResolvedSuggestion(marker.suggestions[0], [a], source="pubmed"),
        ResolvedSuggestion(marker.suggestions[1], [a], source="pubmed"),
    ]
    agent = make_agent([answer([
        {"suggestion": "PMID: 1", "pmid": "1", "score": 90, "why": "yes"},
        {"suggestion": "doi: 10.1000/a", "pmid": "1", "score": 90, "why": "yes"},
    ])])
    ev = agent.evaluate_suggested(s, marker, resolved)
    assert len(ev.selected) == 1 and ev.slot_sizes == [1]
    codes = [w.code for w in ev.warnings]
    assert "suggested_duplicate" in codes and "retracted" in codes


def test_extra_search_adds_agent_found_reference():
    s = sentence("Claim (REF, PMID: 1).")
    marker = s.markers[0]
    assert marker.extra_search == 1
    a = cand(pmid="1", title="Suggested")
    found = cand(pmid="7", title="Found by search")
    resolved = [ResolvedSuggestion(marker.suggestions[0], [a], source="pubmed")]
    agent = make_agent([answer([
        {"suggestion": "PMID: 1", "pmid": "1", "score": 70, "why": "ok"},
        {"suggestion": "REF", "pmid": "7", "score": 88, "why": "supports"},
    ])], articles={"7": found})
    ev = agent.evaluate_suggested(s, marker, resolved)
    assert [c.pmid for c in ev.selected] == ["1", "7"]
    assert ev.slot_sizes == [2]


def test_unparseable_answer_falls_back_to_resolved_records():
    s = sentence("Claim (PMID: 1).")
    marker = s.markers[0]
    a = cand(pmid="1", title="Paper A")
    resolved = [ResolvedSuggestion(marker.suggestions[0], [a], source="pubmed")]
    agent = make_agent([text_response("Sorry, I cannot do that.")])
    ev = agent.evaluate_suggested(s, marker, resolved)
    assert [c.pmid for c in ev.selected] == ["1"]
    assert ev.confidence_level == ConfidenceLevel.UNRESOLVED
    assert any(w.code == "suggested_unscored" for w in ev.warnings)

    # Ambiguous + failed agent -> nothing selected, but candidates kept for the chat panel
    s2 = sentence("Claim (Smith 2020).")
    m2 = s2.markers[0]
    two = [cand(pmid="1", authors=["Smith"], year=2020), cand(pmid="2", authors=["Smith"], year=2020)]
    agent = make_agent([text_response("garbage")])
    ev = agent.evaluate_suggested(s2, m2, [ResolvedSuggestion(m2.suggestions[0], two, total_matches=2)])
    assert ev.selected == [] and len(ev.candidates) == 2


def test_prompt_mentions_suggestions_and_context():
    s = sentence("Claim (Battison et al. 2024).")
    marker = s.markers[0]
    resolved = [ResolvedSuggestion(marker.suggestions[0], [], error="nothing found")]
    agent = make_agent([])
    msg = agent._build_suggested_message(s, marker, resolved, ["synaptic neuroscience"], "after the words: X")
    assert "Battison et al. 2024" in msg and "NOT FOUND" in msg and "synaptic neuroscience" in msg
    assert "after the words: X" in msg
    assert "first author 'Battison', year 2024" in msg


def test_find_citations_still_parses_plain_answers():
    s = SentenceRecord(id="S1", raw_text="Claim (REF).", clean_text="Claim.", marker_type=MarkerType.REF, marker_count=1)
    a = cand(pmid="1", title="Paper A")
    agent = make_agent([text_response(json.dumps({
        "selected": [{"pmid": "1", "why": "x"}], "confidence": "HIGH", "confidence_score": 80,
        "confidence_rationale": "r", "verification_status": "partial", "supporting_snippets": [],
        "search_queries_used": ["q"], "all_pmids_considered": ["1"],
    }))], articles={"1": a})
    ev = agent.find_citations(s)
    assert [c.pmid for c in ev.selected] == ["1"]
    assert ev.confidence_level == ConfidenceLevel.HIGH and ev.search_query == "q"
