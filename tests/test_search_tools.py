"""Tool schemas live in one module and match the executor's dispatch table."""

from src.services.search_tools import (
    FINAL_TOOL_NAMES, GET_FULLTEXT_PASSAGES, RECORD_VERDICT, SELECT_CITATIONS,
    SUBMIT_CITATIONS, build_tool_list, find_tool_use,
)
from src.services.tool_executor import ToolExecutor
from tests.fakes import message, text_block, tool_use_block


def test_every_search_tool_is_dispatchable_and_well_formed():
    tools = build_tool_list(user_library=True, biorxiv=True, europepmc=True, fulltext=True,
                            final_tool=None, cache=False)
    names = [t["name"] for t in tools]
    assert names == [
        "search_user_library", "search_pubmed", "fetch_articles",
        "search_biorxiv", "fetch_biorxiv_preprint",
        "search_europepmc", "fetch_europepmc_article", "get_fulltext_passages",
    ]
    for t in tools:
        assert t["description"] and t["input_schema"]["type"] == "object"
        if t["name"] != "get_fulltext_passages":      # executor handler lands with full text
            assert t["name"] in ToolExecutor._dispatch, t["name"]


def test_final_tool_goes_last_and_carries_the_cache_breakpoint():
    tools = build_tool_list(europepmc=True, final_tool=SUBMIT_CITATIONS)
    assert tools[-1]["name"] == "submit_citations"
    assert tools[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in SUBMIT_CITATIONS          # module constant untouched
    assert all("cache_control" not in t for t in tools[:-1])
    assert "get_fulltext_passages" not in [t["name"] for t in tools]


def test_fulltext_tool_requires_europepmc():
    assert "get_fulltext_passages" not in [
        t["name"] for t in build_tool_list(europepmc=False, fulltext=True)]
    assert GET_FULLTEXT_PASSAGES["input_schema"]["required"] == ["keywords"]


def test_final_tools_are_not_executor_tools():
    for tool in (SUBMIT_CITATIONS, SELECT_CITATIONS, RECORD_VERDICT):
        assert tool["name"] in FINAL_TOOL_NAMES
        assert tool["name"] not in ToolExecutor._dispatch
    assert set(SUBMIT_CITATIONS["input_schema"]["required"]) >= {"selected", "confidence"}


def test_find_tool_use():
    resp = message(text_block("hi"), tool_use_block("search_pubmed", {"query": "q"}),
                   tool_use_block("submit_citations", {"selected": []}, id="s"),
                   stop_reason="tool_use")
    assert find_tool_use(resp, "submit_citations").id == "s"
    assert find_tool_use(resp, "record_verdict") is None
    assert find_tool_use(message(text_block("x")), "submit_citations") is None
