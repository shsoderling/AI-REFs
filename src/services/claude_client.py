"""One place to build the Anthropic client and to call the Messages API.

``make_client`` honours corporate CA bundles.  ``ClaudeCaller`` wraps
``messages.create`` with the request shape every loop in the app uses
(system prompt as a cacheable block, tools, messages) so the agent, the
verifier and the chat worker send identical requests.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

import anthropic
import certifi
import httpx

logger = logging.getLogger(__name__)

LogCallback = Callable[[str, str], None]


def ca_bundle() -> str:
    """CA bundle path: SSL_CERT_FILE, then REQUESTS_CA_BUNDLE, then certifi.

    Lets TLS-inspecting proxies (e.g. Zscaler) work without ever disabling
    verification.
    """
    return (
        os.environ.get("SSL_CERT_FILE")
        or os.environ.get("REQUESTS_CA_BUNDLE")
        or certifi.where()
    )


def make_client(api_key: str, **kwargs: Any) -> anthropic.Anthropic:
    """An ``anthropic.Anthropic`` client using the CA bundle above."""
    return anthropic.Anthropic(
        api_key=api_key,
        http_client=httpx.Client(verify=ca_bundle()),
        **kwargs,
    )


@dataclass
class ModelCaps:
    """What a model has been observed to accept during this process."""
    thinking: Any = None      # None = unprobed; dict = accepted config; False = none accepted


_CAPS: dict[str, ModelCaps] = {}
_CAPS_LOCK = threading.Lock()


def caps_for(model: str) -> ModelCaps:
    with _CAPS_LOCK:
        return _CAPS.setdefault(model, ModelCaps())


def reset_caps() -> None:
    with _CAPS_LOCK:
        _CAPS.clear()


def system_blocks(system: str | list) -> list[dict]:
    """The system prompt as a list of text blocks with a cache breakpoint."""
    if isinstance(system, list):
        return system
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


class ClaudeCaller:
    """Send Messages API requests with the app's standard shape."""

    def __init__(self, client, model: str, log: Optional[LogCallback] = None):
        self.client = client
        self.model = model
        self._log = log or (lambda *a: None)

    def create(self, *, system: str | list, messages: list, tools: list,
               max_tokens: int = 8192, thinking: bool = True):
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_blocks(system),
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        return self.client.messages.create(**kwargs)
