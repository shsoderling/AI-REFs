"""One place to build the Anthropic client and to call the Messages API.

``make_client`` honours corporate CA bundles.  ``ClaudeCaller`` wraps
``messages.create`` with the request shape every loop in the app uses
(system prompt as a cacheable block, tools, messages, extended thinking)
so the agent, the verifier and the chat worker send identical requests.

Thinking is requested adaptively.  Models are discovered at run time, so
the caller cannot know in advance which thinking configuration a model
accepts: it walks a ladder (adaptive → fixed budget → none) and memoises,
per model and per process, the first rung the API accepted.
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

# Thinking configurations tried in order; the last rung sends no thinking.
THINKING_LADDER: tuple[Any, ...] = (
    {"type": "adaptive"},
    {"type": "enabled", "budget_tokens": 2048},
    None,
)


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
    """An ``anthropic.Anthropic`` client using the CA bundle above.

    Newer SDK releases are built on the ``httpx2`` package and reject an
    ``httpx.Client``; try the HTTP client classes the SDK may accept and fall
    back to the SDK's own client (with the CA bundle via SSL_CERT_FILE, when
    the user set it) rather than failing to start.
    """
    http_clients = [httpx]
    try:
        import httpx2  # type: ignore
        http_clients.append(httpx2)
    except ImportError:
        pass
    last_error: Optional[Exception] = None
    for mod in http_clients:
        try:
            return anthropic.Anthropic(
                api_key=api_key,
                http_client=mod.Client(verify=ca_bundle()),
                **kwargs,
            )
        except TypeError as exc:                      # wrong httpx flavour for this SDK
            last_error = exc
    logger.warning(f"Could not attach a custom HTTP client to the Anthropic SDK: {last_error}")
    return anthropic.Anthropic(api_key=api_key, **kwargs)


@dataclass
class ModelCaps:
    """What a model has been observed to accept during this process."""
    thinking_rung: int = 0

    @property
    def thinking(self) -> Any:
        return THINKING_LADDER[self.thinking_rung]


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


def _is_thinking_rejection(err: anthropic.BadRequestError) -> bool:
    text = str(err).lower()
    return any(word in text for word in ("thinking", "adaptive", "budget_tokens"))


class ClaudeCaller:
    """Send Messages API requests with the app's standard shape."""

    def __init__(self, client, model: str, log: Optional[LogCallback] = None):
        self.client = client
        self.model = model
        self._log = log or (lambda *a: None)

    def create(self, *, system: str | list, messages: list, tools: list,
               max_tokens: int = 8192, thinking: bool = True):
        """Call the Messages API; with ``thinking`` the model's best accepted
        thinking configuration is used (probed on first use)."""
        caps = caps_for(self.model)
        while True:
            rung = caps.thinking_rung
            config = THINKING_LADDER[rung] if thinking else None
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens,
                "system": system_blocks(system),
                "messages": messages,
            }
            if tools:
                kwargs["tools"] = tools
            if config:
                kwargs["thinking"] = dict(config)
                budget = config.get("budget_tokens")
                if budget and max_tokens <= budget:
                    kwargs["max_tokens"] = budget + 2048
            try:
                return self.client.messages.create(**kwargs)
            except anthropic.BadRequestError as err:
                if not (config and _is_thinking_rejection(err)):
                    raise
                if rung >= len(THINKING_LADDER) - 1:
                    raise
                with _CAPS_LOCK:
                    if caps.thinking_rung == rung:      # another thread may have moved on
                        caps.thinking_rung = rung + 1
                nxt = THINKING_LADDER[caps.thinking_rung]
                self._log("info", f"Model {self.model} rejected thinking {config}; "
                                  f"using {nxt or 'no thinking'} for the rest of this run")
                logger.info("Thinking fallback for %s: %s -> %s", self.model, config, nxt)
