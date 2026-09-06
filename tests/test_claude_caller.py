"""ClaudeCaller: request shape and the per-model thinking fallback ladder."""

import anthropic
import pytest

from src.services.claude_client import ClaudeCaller, caps_for, reset_caps, system_blocks
from tests.fakes import FakeAnthropic, bad_request, message, text_block

REPLY = message(text_block("ok"))


def test_first_call_asks_for_adaptive_thinking_and_a_cacheable_system_block():
    client = FakeAnthropic([REPLY])
    ClaudeCaller(client, "claude-x").create(system="be brief", messages=[], tools=[{"name": "t"}])
    call = client.last_call
    assert call["thinking"] == {"type": "adaptive"}
    assert call["system"] == [{"type": "text", "text": "be brief", "cache_control": {"type": "ephemeral"}}]
    assert call["tools"] == [{"name": "t"}] and call["max_tokens"] == 8192


def test_thinking_false_sends_no_thinking_key():
    client = FakeAnthropic([REPLY])
    ClaudeCaller(client, "claude-x").create(system="s", messages=[], tools=[], thinking=False)
    assert "thinking" not in client.last_call and "tools" not in client.last_call


def test_adaptive_rejection_falls_back_to_a_budget_then_to_none_and_is_memoised():
    client = FakeAnthropic([
        bad_request("thinking: adaptive is not supported on this model"),
        bad_request("thinking.budget_tokens: not supported"),
        REPLY,
        REPLY,
    ])
    caller = ClaudeCaller(client, "claude-old")
    caller.create(system="s", messages=[], tools=[], max_tokens=1024)
    kinds = [c.get("thinking") for c in client.calls]
    assert kinds[0] == {"type": "adaptive"}
    assert kinds[1] == {"type": "enabled", "budget_tokens": 2048}
    assert client.calls[1]["max_tokens"] > 2048          # room for the budget
    assert kinds[2] is None

    # A fresh caller for the same model starts at the memoised rung, no re-probe
    ClaudeCaller(client, "claude-old").create(system="s", messages=[], tools=[])
    assert "thinking" not in client.calls[3]
    assert caps_for("claude-old").thinking is None
    # ...while another model is unaffected
    assert caps_for("claude-new").thinking == {"type": "adaptive"}


def test_non_thinking_400s_propagate_untouched():
    client = FakeAnthropic([bad_request("messages: roles must alternate")])
    with pytest.raises(anthropic.BadRequestError, match="alternate"):
        ClaudeCaller(client, "claude-x").create(system="s", messages=[], tools=[])
    assert caps_for("claude-x").thinking_rung == 0


def test_reset_caps_clears_the_memo():
    client = FakeAnthropic([bad_request("thinking not supported"), REPLY])
    ClaudeCaller(client, "claude-y").create(system="s", messages=[], tools=[])
    assert caps_for("claude-y").thinking_rung == 1
    reset_caps()
    assert caps_for("claude-y").thinking_rung == 0


def test_system_blocks_passes_lists_through():
    blocks = [{"type": "text", "text": "x"}]
    assert system_blocks(blocks) is blocks
