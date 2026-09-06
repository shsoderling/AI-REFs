"""The review/recency preferences from the Input tab reach the prompts."""

import pytest

from src.models.sentence import MarkerType, SentenceRecord
from src.pipeline.llm_citation_agent import LLMCitationAgent, build_preference_text
from tests.fakes import FakeAnthropic, FakePubMed, submit_msg


def _agent(client, **prefs):
    return LLMCitationAgent(anthropic_api_key="k", model="m", pubmed_client=FakePubMed(),
                            biorxiv_client=None, europepmc_client=None,
                            search_biorxiv=False, search_europepmc=False,
                            client=client, **prefs)


def _sentence():
    return SentenceRecord(id="S001", raw_text="Spines remodel (REF).", clean_text="Spines remodel.",
                          marker_type=MarkerType.REF, marker_count=1, marker_types=[MarkerType.REF])


def _system_text(call):
    system = call["system"]
    if isinstance(system, str):
        return system
    return "".join(block["text"] for block in system)


@pytest.mark.parametrize("prefer_reviews,recency_bias", [
    (False, True), (True, True), (False, False), (True, False),
])
def test_preference_sentences_follow_the_settings(prefer_reviews, recency_bias):
    client = FakeAnthropic([submit_msg([])])
    agent = _agent(client, prefer_reviews=prefer_reviews, recency_bias=recency_bias)
    agent.find_citations(_sentence())

    system = _system_text(client.last_call)
    expected = build_preference_text(prefer_reviews, recency_bias)
    assert expected in system
    if prefer_reviews:
        assert "review" in expected.lower() and "primary research over reviews" not in system
    else:
        assert "primary research over reviews" in system
    if recency_bias:
        assert "recent" in expected.lower()
    else:
        assert "Do not weigh publication date" in system


def test_defaults_match_the_previous_hard_coded_prompt():
    text = build_preference_text(prefer_reviews=False, recency_bias=True)
    assert "Prefer peer-reviewed primary research over reviews" in text
    assert "Prefer recent publications when relevance is equal" in text
