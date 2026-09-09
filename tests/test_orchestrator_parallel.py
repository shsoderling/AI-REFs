"""Concurrent sentence searches: parallelism rules, ordering, cancel and pause."""

import threading
import time

import pytest
from docx import Document

import src.pipeline.orchestrator as orch
from src.models.evidence import EvidenceRecord
from src.models.project import PipelineStage, ProjectState
from src.storage.cache_db import CacheDB
from tests.fakes import FakeBioRxiv, FakeEuropePMC, FakePubMed, article

N_SENTENCES = 6


@pytest.fixture
def docx_path(tmp_path):
    doc = Document()
    for i in range(N_SENTENCES):
        doc.add_paragraph(f"Claim number {i} is supported by the literature (REF).")
    path = tmp_path / "many.docx"
    doc.save(str(path))
    return str(path)


class SlowAgent:
    """Records which thread searched which sentence; 50 ms per sentence."""
    instances = []

    def __init__(self, **kwargs):
        self.calls = []
        self.lock = threading.Lock()
        self.on_call = None
        self.caller = None
        SlowAgent.instances.append(self)

    def find_citations(self, sentence, domain_context=None, context=None):
        with self.lock:
            self.calls.append((sentence.id, threading.current_thread().name))
        if self.on_call:
            self.on_call(sentence)
        time.sleep(0.05)
        ev = EvidenceRecord(sentence_id=sentence.id, selected=[article(f"1{sentence.id[-1]}", sentence.id)])
        return ev


@pytest.fixture
def offline(monkeypatch):
    SlowAgent.instances.clear()
    monkeypatch.setattr(orch, "CacheDB", lambda *a, **k: CacheDB(":memory:"))
    monkeypatch.setattr(orch, "PubMedClient", lambda **k: FakePubMed())
    monkeypatch.setattr(orch, "EuropePMCClient", lambda **k: FakeEuropePMC())
    monkeypatch.setattr(orch, "BioRxivClient", lambda **k: FakeBioRxiv())
    monkeypatch.setattr(orch, "LLMCitationAgent", SlowAgent)


def _project(docx_path, **settings):
    project = ProjectState(input_docx_path=docx_path)
    project.settings.anthropic_api_key = "k"
    project.settings.ncbi_email = "x@y.z"
    project.settings.ncbi_api_key = "ncbi-key"
    project.settings.domain_inference = False
    project.settings.reference_library_enabled = False
    project.settings.verify_citations = False
    project.settings.parallel_searches = 3
    for k, v in settings.items():
        setattr(project.settings, k, v)
    return project


def test_effective_parallelism_rules(docx_path):
    assert orch.PipelineOrchestrator(_project(docx_path, ncbi_api_key=None))._effective_parallelism() == 1
    assert orch.PipelineOrchestrator(_project(docx_path))._effective_parallelism() == 3
    assert orch.PipelineOrchestrator(_project(docx_path, parallel_searches=8))._effective_parallelism() == 8


def test_parallel_run_uses_several_threads_and_keeps_document_order(docx_path, offline):
    project = _project(docx_path)
    start = time.monotonic()
    result = orch.PipelineOrchestrator(project).run()
    elapsed = time.monotonic() - start

    assert result.current_stage == PipelineStage.COMPLETE
    ids = list(result.evidence_map)
    assert ids == sorted(ids) and len(ids) == N_SENTENCES
    assert all(result.evidence_map[i].sentence_id == i for i in ids)
    assert elapsed < N_SENTENCES * 0.05 * 0.9                       # faster than serial
    threads = {name for _, name in SlowAgent.instances[0].calls}
    assert len(threads) >= 2 and all(name.startswith("airefs-search") for name in threads)


def test_serial_run_without_an_ncbi_key_uses_the_calling_thread(docx_path, offline):
    project = _project(docx_path, ncbi_api_key=None)
    result = orch.PipelineOrchestrator(project).run()
    assert len(result.evidence_map) == N_SENTENCES
    names = {name for _, name in SlowAgent.instances[0].calls}
    assert names == {threading.current_thread().name}


def test_cancel_during_a_search_stops_the_remaining_sentences(docx_path, offline):
    project = _project(docx_path)
    orchestrator = orch.PipelineOrchestrator(project)

    def cancel_on_first(sentence):
        orchestrator.cancel()

    # The agent is created inside run(); hook the cancel via the class
    original_init = SlowAgent.__init__

    def init_with_hook(self, **kwargs):
        original_init(self, **kwargs)
        self.on_call = cancel_on_first
    SlowAgent.__init__ = init_with_hook
    try:
        result = orchestrator.run()
    finally:
        SlowAgent.__init__ = original_init

    assert result.current_stage != PipelineStage.COMPLETE
    assert len(SlowAgent.instances[0].calls) < N_SENTENCES


def test_pause_holds_the_workers_until_resume(docx_path, offline):
    project = _project(docx_path)
    orchestrator = orch.PipelineOrchestrator(project)
    orchestrator.pause()
    holder = {}
    thread = threading.Thread(target=lambda: holder.setdefault("result", orchestrator.run()))
    thread.start()
    time.sleep(0.25)
    assert not SlowAgent.instances or SlowAgent.instances[0].calls == []
    orchestrator.resume()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert holder["result"].current_stage == PipelineStage.COMPLETE
    assert len(holder["result"].evidence_map) == N_SENTENCES
