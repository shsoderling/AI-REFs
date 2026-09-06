"""The citation agent's tool-use loop, driven by a scripted Claude."""

import anthropic
import httpx
import pytest

from src.models.evidence import ConfidenceLevel, VerificationStatus
from src.models.sentence import MarkerType, SentenceRecord
from src.pipeline.llm_citation_agent import MAX_AGENT_ROUNDS, LLMCitationAgent
from tests.fakes import (
    FakeAnthropic, FakeEuropePMC, FakePubMed, article, message, submit_msg, text_block,
    tool_use_msg,
)


def sentence(text="Dendritic spines remodel during learning.", marker=MarkerType.REF):
    return SentenceRecord(id="S001", raw_text=f"{text} ({marker.value})", clean_text=text,
                          marker_type=marker, marker_count=1, marker_types=[marker])


def make_agent(client, pubmed=None, **kw):
    return LLMCitationAgent(
        anthropic_api_key="k", model="claude-test", pubmed_client=pubmed or FakePubMed(),
        biorxiv_client=None, europepmc_client=kw.pop("europepmc", None),
        search_biorxiv=False, search_europepmc=kw.pop("search_europepmc", False),
        client=client, **kw,
    )


def test_happy_path_search_fetch_submit():
    pubmed = FakePubMed(by_search={"spine remodeling learning": (["11", "12"], 2)},
                        articles={"11": article("11", "Spines remodel", abstract="Spines remodel."),
                                  "12": article("12", "Other")})
    client = FakeAnthropic([
        tool_use_msg(("search_pubmed", {"query": "spine remodeling learning"})),
        tool_use_msg(("fetch_articles", {"pmids": ["11", "12"]})),
        submit_msg([{"pmid": "11"}], confidence="HIGH", score=90, status="verified",
                   snippets=("Spines remodel.",), queries=("spine remodeling learning",),
                   considered=("11", "12")),
    ])
    agent = make_agent(client, pubmed)

    ev = agent.find_citations(sentence(), domain_context=["synaptic neuroscience"])

    assert [c.pmid for c in ev.selected] == ["11"]
    assert ev.confidence_level == ConfidenceLevel.HIGH and ev.confidence_score == 90
    assert ev.verification_status == VerificationStatus.VERIFIED
    assert ev.abstract_snippets == ["Spines remodel."]
    assert ev.search_query == "spine remodeling learning"
    assert {c.pmid for c in ev.candidates} == {"11", "12"}
    assert len(client.calls) == 3
    # Request shape: cacheable system block, submit tool last with the cache marker
    first = client.calls[0]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["tools"][-1]["name"] == "submit_citations"
    assert "synaptic neuroscience" in first["messages"][0]["content"]
    # Tool results were fed back with the right ids
    second = client.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert second[2]["content"][0]["tool_use_id"] == "tu_1"
    assert second[2]["content"][0]["content"].startswith('{"pmids"')


def test_prose_answer_is_redirected_to_the_tool():
    client = FakeAnthropic([
        message(text_block("I think PMID 11 fits."), stop_reason="end_turn"),
        submit_msg([{"pmid": "11"}]),
    ])
    pubmed = FakePubMed(articles={"11": article("11", "Spines remodel")})
    ev = make_agent(client, pubmed).find_citations(sentence())

    redirect = client.calls[1]["messages"][-1]
    assert redirect["role"] == "user" and "submit_citations" in redirect["content"]
    assert [c.pmid for c in ev.selected] == ["11"]           # fetched on demand


def test_deadline_call_offers_only_the_submit_tool():
    script = [tool_use_msg(("search_pubmed", {"query": f"q{i}"})) for i in range(MAX_AGENT_ROUNDS)]
    script.append(submit_msg([], confidence="LOW", score=10, status="weak"))
    client = FakeAnthropic(script)

    ev = make_agent(client).find_citations(sentence())

    assert len(client.calls) == MAX_AGENT_ROUNDS + 1
    last = client.calls[-1]
    assert [t["name"] for t in last["tools"]] == ["submit_citations"]
    assert "tool_choice" not in last
    assert "DEADLINE" in last["messages"][-1]["content"]
    assert ev.selected == [] and ev.confidence_level == ConfidenceLevel.LOW
    assert ev.search_query == " | ".join(f"q{i}" for i in range(MAX_AGENT_ROUNDS))


def test_no_submission_even_at_the_deadline_gives_a_low_unresolved_record():
    script = [tool_use_msg(("search_pubmed", {"query": "q"})) for _ in range(MAX_AGENT_ROUNDS)]
    script.append(message(text_block("still thinking"), stop_reason="end_turn"))
    ev = make_agent(FakeAnthropic(script)).find_citations(sentence())
    assert ev.selected == []
    assert ev.confidence_level == ConfidenceLevel.LOW
    assert "without submitting" in ev.confidence_rationale


def test_api_error_marks_the_sentence_unresolved():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.InternalServerError("boom", response=httpx.Response(500, request=request), body=None)
    ev = make_agent(FakeAnthropic([err])).find_citations(sentence())
    assert ev.confidence_level == ConfidenceLevel.UNRESOLVED
    assert "API error" in ev.retrieval_error


def test_auth_error_is_raised_for_the_pipeline_to_stop():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=request), body=None)
    with pytest.raises(RuntimeError, match="authentication"):
        make_agent(FakeAnthropic([err])).find_citations(sentence())


def test_refs_marker_asks_for_max_refs_and_tool_list_reflects_sources():
    client = FakeAnthropic([submit_msg([])])
    agent = make_agent(client, max_refs=4, europepmc=FakeEuropePMC(), search_europepmc=True,
                       use_full_text=True)
    agent.find_citations(sentence(marker=MarkerType.REFS))
    call = client.calls[0]
    assert "4 references" in call["messages"][0]["content"]
    names = [t["name"] for t in call["tools"]]
    assert "search_europepmc" in names and "get_fulltext_passages" in names
    assert "search_biorxiv" not in names


def test_orcid_hint_only_for_self_referencing_claims():
    client = FakeAnthropic([submit_msg([]), submit_msg([])])
    agent = make_agent(client, orcid_id="0000-0001-2345-6789")
    agent.find_citations(sentence("We previously showed that spines remodel."))
    assert "0000-0001-2345-6789[auid]" in client.calls[0]["messages"][0]["content"]
    agent.find_citations(sentence("Spines remodel."))
    assert "[auid]" not in client.calls[1]["messages"][0]["content"]


def test_context_reaches_the_agent_with_the_do_not_cite_instruction():
    from src.pipeline.claim_context import build_claim_context
    doc = [
        SentenceRecord(id="S000", paragraph_index=3, clean_text="Spines are dynamic.", raw_text="Spines are dynamic.", section="Results"),
        sentence("These changes require Rac1."),
    ]
    doc[1].paragraph_index = 3
    doc[1].section = "Results"
    ctx = build_claim_context(doc, doc[1])
    client = FakeAnthropic([submit_msg([])])
    make_agent(client).find_citations(doc[1], context=ctx)
    msg = client.calls[0]["messages"][0]["content"]
    assert msg.startswith('CLAIM — find 1 reference')
    assert "Section: Results" in msg and "Do NOT search for or cite" in msg
    assert 'Preceding: "Spines are dynamic."' in msg
