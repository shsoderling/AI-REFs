"""LLMCitationAgent.evaluate_suggested driven by a scripted Claude (tool use)."""

from types import SimpleNamespace

from src.models.citation import Author, CitationCandidate
from src.models.evidence import ConfidenceLevel
from src.models.markers import MarkerType, SuggestionKind
from src.models.sentence import SentenceRecord
from src.pipeline.claim_context import ClaimContext
from src.pipeline.llm_citation_agent import LLMCitationAgent, cap_suggested_confidence
from src.services.suggestion_resolver import ResolvedSuggestion
from src.utils.markers import find_markers
from tests.fakes import FakeAnthropic, FakePubMed, message, submit_msg, text_block, tool_use_block


def make_agent(script, articles=None):
    client = FakeAnthropic(script)
    agent = LLMCitationAgent(anthropic_api_key="sk-test", model="m",
                             pubmed_client=FakePubMed(articles=articles), client=client,
                             search_biorxiv=False, search_europepmc=False)
    return agent, client


def cand(pmid="", doi="", title="T", authors=(), year=2024, retracted=False):
    return CitationCandidate(pmid=pmid, doi=doi, title=title, year=year, is_retracted=retracted,
                             authors=[Author(last_name=a) for a in authors], abstract="abs")


def sentence(text):
    s = SentenceRecord(id="S001", raw_text=text, clean_text=text.split(" (")[0] + ".")
    s.markers = find_markers(text)
    s.marker_types = [m.kind for m in s.markers]
    s.marker_type = s.markers[0].kind
    s.marker_count = len(s.markers)
    return s


def evaluation_msg(selected, **extra):
    """The agent's answer through the submit_suggested_evaluation tool."""
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
    return message(tool_use_block("submit_suggested_evaluation", data, id="tu_eval"),
                   stop_reason="tool_use")


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
    agent, client = make_agent([evaluation_msg([
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
    # Request shape: the evaluation tool is offered last; the records were pre-loaded
    call = client.calls[0]
    assert call["tools"][-1]["name"] == "submit_suggested_evaluation"
    prompt = call["messages"][0]["content"]
    assert "Paper A" in prompt and "Paper B" in prompt and "(PMID: 1; doi: 10.1000/b)" in prompt


def test_weak_match_warning_and_alternative():
    s = sentence("Claim (PMID: 1).")
    marker = s.markers[0]
    a = cand(pmid="1", title="Paper A")
    alt = cand(pmid="5", title="Alternative")
    resolved = [ResolvedSuggestion(marker.suggestions[0], [a], source="pubmed")]
    agent, _ = make_agent([evaluation_msg(
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

    agent, _ = make_agent([evaluation_msg(
        [{"suggestion": "Battison et al. 2024", "pmid": "2", "score": 80, "why": "topic fits"}])])
    ev = agent.evaluate_suggested(s, marker, resolved)
    assert [c.pmid for c in ev.selected] == ["2"]
    assert any(w.code == "suggested_ambiguous" for w in ev.warnings)
    assert ev.confidence_level == ConfidenceLevel.MEDIUM

    # A pick whose authors do not match is discarded -> unresolved
    agent, _ = make_agent([evaluation_msg(
        [{"suggestion": "Battison et al. 2024", "pmid": "3", "score": 80, "why": "?"}])],
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
    agent, _ = make_agent([evaluation_msg([
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
    agent, client = make_agent([
        message(tool_use_block("search_pubmed", {"query": "spines"}, id="tu_1"), stop_reason="tool_use"),
        evaluation_msg([
            {"suggestion": "PMID: 1", "pmid": "1", "score": 70, "why": "ok"},
            {"suggestion": "REF", "pmid": "7", "score": 88, "why": "supports"},
        ]),
    ], articles={"7": found})
    ev = agent.evaluate_suggested(s, marker, resolved)
    assert [c.pmid for c in ev.selected] == ["1", "7"]
    assert ev.slot_sizes == [2]
    assert len(client.calls) == 2 and ev.search_query == "spines"


def test_prose_answer_then_deadline_falls_back_to_resolved_records():
    s = sentence("Claim (PMID: 1).")
    marker = s.markers[0]
    a = cand(pmid="1", title="Paper A")
    resolved = [ResolvedSuggestion(marker.suggestions[0], [a], source="pubmed")]
    # Prose every round, and still prose at the deadline: the resolved record is used as-is
    from src.pipeline.llm_citation_agent import MAX_AGENT_ROUNDS
    script = [message(text_block("Sorry, I cannot do that."), stop_reason="end_turn")
              for _ in range(MAX_AGENT_ROUNDS + 1)]
    agent, client = make_agent(script)
    ev = agent.evaluate_suggested(s, marker, resolved)
    assert [c.pmid for c in ev.selected] == ["1"]
    assert ev.confidence_level == ConfidenceLevel.UNRESOLVED
    assert any(w.code == "suggested_unscored" for w in ev.warnings)
    redirect = client.calls[1]["messages"][-1]["content"]
    assert "submit_suggested_evaluation" in redirect
    assert [t["name"] for t in client.calls[-1]["tools"]] == ["submit_suggested_evaluation"]

    # Ambiguous + failed agent -> nothing selected, but candidates kept for the chat panel
    s2 = sentence("Claim (Smith 2020).")
    m2 = s2.markers[0]
    two = [cand(pmid="1", authors=["Smith"], year=2020), cand(pmid="2", authors=["Smith"], year=2020)]
    agent, _ = make_agent([message(text_block("garbage"), stop_reason="end_turn")
                           for _ in range(MAX_AGENT_ROUNDS + 1)])
    ev = agent.evaluate_suggested(s2, m2, [ResolvedSuggestion(m2.suggestions[0], two, total_matches=2)])
    assert ev.selected == [] and len(ev.candidates) == 2


def test_api_error_falls_back_to_resolved_records():
    import anthropic
    import httpx
    s = sentence("Claim (PMID: 1).")
    marker = s.markers[0]
    resolved = [ResolvedSuggestion(marker.suggestions[0], [cand(pmid="1")], source="pubmed")]
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.InternalServerError("boom", response=httpx.Response(500, request=request), body=None)
    agent, _ = make_agent([err])
    ev = agent.evaluate_suggested(s, marker, resolved)
    assert [c.pmid for c in ev.selected] == ["1"] and "API error" in ev.confidence_rationale


def test_prompt_mentions_suggestions_and_context():
    s = sentence("Claim (Battison et al. 2024).")
    marker = s.markers[0]
    resolved = [ResolvedSuggestion(marker.suggestions[0], [], error="nothing found")]
    agent, _ = make_agent([])
    ctx = ClaimContext(claim="Claim.", section="Results", preceding=["Before."],
                       sub_claim="as shown before", trailing="and more")
    msg = agent._build_suggested_message(ctx, marker, resolved, ["synaptic neuroscience"])
    assert "Battison et al. 2024" in msg and "NOT FOUND" in msg and "synaptic neuroscience" in msg
    assert 'ending at the marker (Battison et al. 2024): "as shown before"' in msg
    assert "Section: Results" in msg and 'Preceding: "Before."' in msg
    assert "first author 'Battison', year 2024" in msg
    assert msg.rstrip().endswith("call submit_suggested_evaluation.")


def test_find_citations_unchanged_by_the_suggested_path():
    s = SentenceRecord(id="S1", raw_text="Claim (REF).", clean_text="Claim.",
                       marker_type=MarkerType.REF, marker_count=1)
    a = cand(pmid="1", title="Paper A")
    agent, client = make_agent([submit_msg([{"pmid": "1"}], confidence="HIGH", score=80,
                                           status="partial", queries=("q",), considered=("1",))],
                               articles={"1": a})
    ev = agent.find_citations(s)
    assert [c.pmid for c in ev.selected] == ["1"]
    assert ev.confidence_level == ConfidenceLevel.HIGH and ev.search_query == "q"
    assert client.calls[0]["tools"][-1]["name"] == "submit_citations"


def test_cap_helper_only_lowers_high():
    from src.models.evidence import EvidenceRecord, Warning
    ev = EvidenceRecord(confidence_level=ConfidenceLevel.HIGH,
                        warnings=[Warning(code="suggested_ambiguous", message="m")])
    cap_suggested_confidence(ev)
    assert ev.confidence_level == ConfidenceLevel.MEDIUM
    ev2 = EvidenceRecord(confidence_level=ConfidenceLevel.LOW,
                         warnings=[Warning(code="suggested_unresolved", message="m")])
    cap_suggested_confidence(ev2)
    assert ev2.confidence_level == ConfidenceLevel.LOW
