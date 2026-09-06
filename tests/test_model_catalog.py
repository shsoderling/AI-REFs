"""Model catalog: live discovery via the Models API, disk cache, fallback, newest-first."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.services import model_catalog as mc
from src.services.model_catalog import (
    CACHE_TTL, FALLBACK_MODELS, NEWEST_MODEL, ModelInfo, ModelSource, fetch_models,
    load_cached_models, load_models, resolve_model_id, save_cached_models,
)


def _sdk_model(id_, name, days_ago):
    return SimpleNamespace(id=id_, display_name=name, type="model",
                           created_at=datetime.now(timezone.utc) - timedelta(days=days_ago))


class FakeClient:
    """Stands in for anthropic.Anthropic: models.list() is an iterable page."""
    calls = []

    def __init__(self, api_key=None, **kw):
        FakeClient.calls.append((api_key, kw))
        self.models = SimpleNamespace(list=lambda: iter(FakeClient.items))

    items = [
        _sdk_model("claude-haiku-4-5-20251001", "Claude Haiku 4.5", 300),
        _sdk_model("claude-haiku-4-5", "Claude Haiku 4.5", 300),          # alias of the same model
        _sdk_model("claude-opus-5", "Claude Opus 5", 40),
        _sdk_model("claude-sonnet-5", "Claude Sonnet 5", 60),
        _sdk_model("claude-fable-5-1", "Claude Fable 5.1", 5),
        _sdk_model("some-embedding-model", "Not Claude", 1),
    ]


@pytest.fixture
def fake_sdk(monkeypatch):
    FakeClient.calls = []
    monkeypatch.setattr(mc.anthropic, "Anthropic", FakeClient)
    return FakeClient


@pytest.fixture
def cache_path(tmp_path, monkeypatch):
    path = tmp_path / "models_cache.json"
    monkeypatch.setattr(mc, "CACHE_PATH", path)
    return path


def test_fetch_filters_dedupes_and_sorts_newest_first(fake_sdk):
    models = fetch_models("sk-ant-test")
    assert [m.id for m in models] == ["claude-fable-5-1", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]
    assert models[0].display_name == "Claude Fable 5.1"
    assert fake_sdk.calls[0][0] == "sk-ant-test"
    assert fake_sdk.calls[0][1].get("max_retries", 2) <= 1          # a UI refresh must not hang


def test_fetch_requires_a_key(fake_sdk):
    with pytest.raises(ValueError):
        fetch_models("")


def test_cache_round_trip_and_ttl(cache_path):
    models = [ModelInfo(id="claude-opus-5", display_name="Claude Opus 5",
                        created_at="2026-04-01T00:00:00+00:00")]
    save_cached_models(models)
    cached, fetched_at = load_cached_models()
    assert cached == models and fetched_at is not None
    stale = datetime.now(timezone.utc) - CACHE_TTL - timedelta(minutes=1)
    data = json.loads(cache_path.read_text())
    data["fetched_at"] = stale.isoformat()
    cache_path.write_text(json.dumps(data))
    cached, fetched_at = load_cached_models()
    assert cached == models                                            # still returned...
    assert mc.cache_is_stale(fetched_at)                               # ...but flagged stale


def test_corrupt_cache_is_ignored(cache_path):
    cache_path.write_text("{not json")
    assert load_cached_models() == ([], None)


def test_load_models_prefers_cache_then_fallback(cache_path):
    models, source = load_models()
    assert source == ModelSource.FALLBACK and [m.id for m in models] == [m.id for m in FALLBACK_MODELS]
    save_cached_models([ModelInfo(id="claude-opus-5", display_name="Claude Opus 5", created_at="2026-04-01T00:00:00+00:00")])
    models, source = load_models()
    assert source == ModelSource.CACHE and [m.id for m in models] == ["claude-opus-5"]


def test_fallback_list_is_newest_first_and_uses_aliases():
    ids = [m.id for m in FALLBACK_MODELS]
    assert ids[0] == "claude-fable-5-1" and "claude-haiku-4-5" in ids
    assert all(not id_[-8:].isdigit() for id_ in ids)                 # aliases, no dated ids
    dates = [m.created_at for m in FALLBACK_MODELS]
    assert dates == sorted(dates, reverse=True)


def test_resolve_model_id(cache_path):
    assert resolve_model_id("claude-opus-5") == "claude-opus-5"        # explicit id passes through
    assert resolve_model_id(NEWEST_MODEL) == FALLBACK_MODELS[0].id     # no cache: newest fallback
    assert resolve_model_id("") == FALLBACK_MODELS[0].id
    save_cached_models([ModelInfo(id="claude-x-9", display_name="Claude X 9", created_at="2027-01-01T00:00:00+00:00")])
    assert resolve_model_id(NEWEST_MODEL) == "claude-x-9"              # cached newest wins
