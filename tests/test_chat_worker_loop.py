"""The chat worker's loop: search rounds, prose replies, and tool-based selection."""

import sys

import pytest
from PySide6.QtWidgets import QApplication

import src.gui.widgets.chat_worker as cw
from src.storage.cache_db import CacheDB
from tests.fakes import (
    FakeAnthropic, FakePubMed, article, message, text_block, tool_use_block, tool_use_msg,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def offline(monkeypatch):
    """No network clients: PubMed is a fake, the cache is in memory."""
    pubmed = FakePubMed(by_search={"rac1 spines": (["11"], 1)},
                        articles={"11": article("11", "Rac1 in spines", abstract="Rac1 drives spines.")})
    monkeypatch.setattr(cw, "CacheDB", lambda *a, **k: CacheDB(":memory:"))
    monkeypatch.setattr(cw, "PubMedClient", lambda **k: pubmed)
    return pubmed


def run_worker(client, **kw):
    worker = cw.ChatSearchWorker(
        messages=[{"role": "user", "content": "find something on Rac1 and spines"}],
        claim_text="Rac1 drives spine growth.", anthropic_api_key="k", model="claude-test",
        ncbi_email="x@y.z", search_biorxiv=False, search_europepmc=False, client=client, **kw,
    )
    got = {"messages": [], "selections": [], "candidates": [], "errors": [], "status": []}
    worker.assistant_message.connect(got["messages"].append)
    worker.selection_made.connect(got["selections"].append)
    worker.candidates_found.connect(got["candidates"].append)
    worker.error.connect(got["errors"].append)
    worker.status_update.connect(got["status"].append)
    worker.run()
    return worker, got


def test_search_round_then_prose_reply(qapp, offline):
    client = FakeAnthropic([
        tool_use_msg(("search_pubmed", {"query": "rac1 spines"}), ("fetch_articles", {"pmids": ["11"]})),
        message(text_block("**#1** Smith (2020). Rac1 in spines."), stop_reason="end_turn"),
    ])
    worker, got = run_worker(client)

    assert got["messages"] == ["**#1** Smith (2020). Rac1 in spines."]
    assert got["selections"] == [] and got["errors"] == []
    assert [c.pmid for c in got["candidates"][0]] == ["11"]
    assert offline.search_calls == ["rac1 spines"]
    tools = [t["name"] for t in client.calls[0]["tools"]]
    assert tools == ["search_pubmed", "fetch_articles", "select_citations"]
    assert "Rac1 drives spine growth." in client.calls[0]["system"][0]["text"]
    assert got["status"] and got["status"][0].startswith("Calling search_pubmed")


def test_select_citations_tool_emits_the_resolved_candidates(qapp, offline):
    chosen = article("11", "Rac1 in spines")
    client = FakeAnthropic([
        message(text_block("Applying #1."),
                tool_use_block("select_citations", {"selections": [{"pmid": "11"}]}),
                stop_reason="tool_use"),
    ])
    worker, got = run_worker(client, prior_candidates={"11": chosen})

    assert got["selections"] == [[chosen]]
    assert got["messages"] == []                 # the panel announces the selection itself
    assert len(client.calls) == 1


def test_selection_of_an_unseen_paper_emits_an_empty_selection(qapp, offline):
    client = FakeAnthropic([
        message(tool_use_block("select_citations", {"selections": [{"pmid": "999"}]}),
                stop_reason="tool_use"),
    ])
    _, got = run_worker(client)
    assert got["selections"] == [[]]


def test_round_limit_reports_what_was_found(qapp, offline):
    client = FakeAnthropic([tool_use_msg(("fetch_articles", {"pmids": ["11"]}))
                            for _ in range(cw.MAX_CHAT_ROUNDS)])
    _, got = run_worker(client)
    assert len(client.calls) == cw.MAX_CHAT_ROUNDS
    assert got["messages"] and "many steps" in got["messages"][0]
    assert [c.pmid for c in got["candidates"][0]] == ["11"]
