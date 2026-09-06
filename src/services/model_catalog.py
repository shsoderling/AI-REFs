"""Claude model catalog for the Input tab.

The list of models is discovered live from Anthropic's Models API (it
returns exactly the models the user's key can use), cached on disk so the
app starts instantly and works offline, and backed by a short built-in
fallback for a fresh install without a key. Models are ordered newest
first, aliases preferred over dated ids. Nothing here is hard-wired to a
particular model generation except the fallback list.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

CACHE_PATH = Path.home() / ".ai_refs" / "models_cache.json"
CACHE_TTL = timedelta(hours=24)
FETCH_TIMEOUT = 15.0            # seconds; a refresh from the GUI must not hang
NEWEST_MODEL = "newest"         # settings sentinel: "the newest model on the list"


@dataclass(frozen=True)
class ModelInfo:
    id: str                     # what the API expects in `model=`
    display_name: str           # Anthropic's name, e.g. "Claude Opus 5"
    created_at: str             # ISO 8601 release timestamp (sort key)


class ModelSource(str, Enum):
    LIVE = "live"               # fetched from the Models API just now
    CACHE = "cache"             # read from the on-disk cache
    FALLBACK = "fallback"       # built-in list (no key / no network / no cache)


# Built-in fallback: current generation at the time of writing, newest first.
# Dates only order the list; the live catalog replaces it on the first refresh.
FALLBACK_MODELS: list[ModelInfo] = [
    ModelInfo("claude-fable-5-1", "Claude Fable 5.1", "2026-06-24T00:00:00+00:00"),
    ModelInfo("claude-opus-5", "Claude Opus 5", "2026-04-01T00:00:00+00:00"),
    ModelInfo("claude-sonnet-5", "Claude Sonnet 5", "2026-03-01T00:00:00+00:00"),
    ModelInfo("claude-opus-4-8", "Claude Opus 4.8", "2026-01-15T00:00:00+00:00"),
    ModelInfo("claude-sonnet-4-6", "Claude Sonnet 4.6", "2025-12-01T00:00:00+00:00"),
    ModelInfo("claude-haiku-4-5", "Claude Haiku 4.5", "2025-10-01T00:00:00+00:00"),
]


def _sort_key(model: ModelInfo) -> datetime:
    try:
        return datetime.fromisoformat(model.created_at)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def fetch_models(api_key: str, timeout: float = FETCH_TIMEOUT) -> list[ModelInfo]:
    """Models the key can use, newest first, one entry per model (alias preferred)."""
    if not (api_key or "").strip():
        raise ValueError("An Anthropic API key is required to list models")
    client = anthropic.Anthropic(api_key=api_key.strip(), max_retries=1, timeout=timeout)
    best: dict[str, ModelInfo] = {}
    for m in client.models.list():                      # the page auto-paginates
        model_id = getattr(m, "id", "") or ""
        if getattr(m, "type", "model") != "model" or not model_id.startswith("claude-"):
            continue
        created = getattr(m, "created_at", None)
        created_iso = created.isoformat() if hasattr(created, "isoformat") else str(created or "")
        info = ModelInfo(id=model_id, display_name=getattr(m, "display_name", "") or model_id,
                         created_at=created_iso)
        current = best.get(info.display_name)
        if current is None or len(info.id) < len(current.id):   # "claude-opus-5" over a dated id
            best[info.display_name] = info
    models = sorted(best.values(), key=_sort_key, reverse=True)
    logger.info(f"Models API: {len(models)} Claude model(s) available to this key")
    return models


def refresh_models(api_key: str, timeout: float = FETCH_TIMEOUT) -> list[ModelInfo]:
    """Fetch and cache."""
    models = fetch_models(api_key, timeout)
    if models:
        save_cached_models(models)
    return models


# ── cache ─────────────────────────────────────────────────────────────

def save_cached_models(models: list[ModelInfo]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps({
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "models": [asdict(m) for m in models],
        }, indent=2))
    except OSError as exc:
        logger.warning(f"Could not write the model cache: {exc}")


def load_cached_models() -> tuple[list[ModelInfo], Optional[datetime]]:
    """Cached models (newest first) and when they were fetched; ([], None) if none."""
    try:
        data = json.loads(CACHE_PATH.read_text())
        models = [ModelInfo(id=str(m["id"]), display_name=str(m.get("display_name") or m["id"]),
                            created_at=str(m.get("created_at") or ""))
                  for m in data.get("models", []) if m.get("id")]
        fetched_at = datetime.fromisoformat(data["fetched_at"]) if data.get("fetched_at") else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return [], None
    return sorted(models, key=_sort_key, reverse=True), fetched_at


def cache_is_stale(fetched_at: Optional[datetime]) -> bool:
    if fetched_at is None:
        return True
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched_at > CACHE_TTL


def load_models() -> tuple[list[ModelInfo], ModelSource]:
    """Best list available without touching the network: cache, else fallback."""
    cached, _ = load_cached_models()
    if cached:
        return cached, ModelSource.CACHE
    return list(FALLBACK_MODELS), ModelSource.FALLBACK


def newest_model_id() -> str:
    return load_models()[0][0].id


def resolve_model_id(model: Optional[str]) -> str:
    """A concrete model id for a settings value: an explicit id passes
    through; empty or ``NEWEST_MODEL`` means the newest model on the list."""
    model = (model or "").strip()
    if model and model != NEWEST_MODEL:
        return model
    return newest_model_id()
