"""The scriptable fakes behave like the real clients for what the code relies on."""

import anthropic
import pytest

from tests.fakes import (
    FakeAnthropic, FakePubMed, article, bad_request, message, submit_msg,
    text_block, tool_use_block, tool_use_msg,
)


def test_fake_anthropic_replays_script_in_order_and_records_calls():
    first = tool_use_msg(("search_pubmed", {"query": "x"}))
    second = submit_msg([{"pmid": "1"}])
    client = FakeAnthropic([first, second])

    r1 = client.messages.create(model="m", messages=[], max_tokens=1)
    r2 = client.messages.create(model="m", messages=[{"role": "user", "content": "hi"}], max_tokens=1)

    assert r1 is first and r2 is second
    assert [c["model"] for c in client.calls] == ["m", "m"]
    assert client.last_call["messages"][0]["content"] == "hi"


def test_fake_anthropic_raises_scripted_exceptions_and_calls_callables():
    err = bad_request("thinking: adaptive is not supported on this model")
    client = FakeAnthropic([err, lambda kw: message(text_block(kw["model"]))])

    with pytest.raises(anthropic.BadRequestError) as excinfo:
        client.messages.create(model="m", messages=[], max_tokens=1)
    assert "adaptive" in str(excinfo.value)
    assert excinfo.value.status_code == 400

    resp = client.messages.create(model="other", messages=[], max_tokens=1)
    assert resp.content[0].text == "other"


def test_fake_anthropic_fails_loudly_when_exhausted():
    client = FakeAnthropic([])
    with pytest.raises(AssertionError):
        client.messages.create(model="m", messages=[], max_tokens=1)


def test_blocks_mirror_sdk_shapes():
    tb = tool_use_block("search_pubmed", {"query": "q"}, id="abc")
    assert (tb.type, tb.name, tb.input, tb.id) == ("tool_use", "search_pubmed", {"query": "q"}, "abc")
    assert not hasattr(tb, "text")          # loops use hasattr(block, "text") for prose
    assert text_block("hi").text == "hi"
    sub = submit_msg([{"pmid": "1"}], confidence="MEDIUM", score=55)
    assert sub.stop_reason == "tool_use"
    assert sub.content[-1].name == "submit_citations"
    assert sub.content[-1].input["confidence"] == "MEDIUM"


def test_fake_pubmed_search_and_fetch():
    pm = FakePubMed(by_search={"rac1": (["1", "2"], 2)},
                    articles={"1": article("1", "Paper one")})
    assert pm.search("rac1") == (["1", "2"], 2)
    assert pm.search("nothing") == ([], 0)
    assert [a.title for a in pm.fetch_articles(["1", "2"])] == ["Paper one"]
    assert pm.search_calls == ["rac1", "nothing"]
