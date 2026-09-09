"""PipelineOrchestrator._find_citations_per_marker with a fake agent + resolver."""

from src.models.citation import Author, CitationCandidate
from src.models.evidence import ConfidenceLevel, EvidenceRecord, Warning
from src.models.markers import MarkerType
from src.models.project import ProjectState
from src.models.sentence import SentenceRecord
from src.pipeline.claim_context import bare_context
from src.pipeline.orchestrator import PipelineOrchestrator
from src.services.suggestion_resolver import ResolvedSuggestion
from src.utils.markers import find_markers


def cand(pmid, title=None):
    return CitationCandidate(pmid=pmid, title=title or f"Paper {pmid}", authors=[Author(last_name="A")], year=2020)


def sentence(text):
    s = SentenceRecord(id="S001", raw_text=text, clean_text=text)
    s.markers = find_markers(text)
    s.marker_types = [m.kind for m in s.markers]
    s.marker_type = s.markers[0].kind
    s.marker_count = len(s.markers)
    return s


class FakeAgent:
    """Returns scripted evidence per call; records what it was asked."""
    max_refs = 3

    def __init__(self, search_results, suggested_results=None):
        self.search_results = list(search_results)
        self.suggested_results = list(suggested_results or [])
        self.search_calls = []       # (temp sentence, ClaimContext)
        self.suggested_calls = []    # (temp sentence, marker, resolved, ClaimContext)

    def find_citations(self, temp, domains=None, context=None):
        self.search_calls.append((temp, context))
        return self.search_results.pop(0)

    def evaluate_suggested(self, temp, marker, resolved, domains=None, context=None):
        self.suggested_calls.append((temp, marker, resolved, context))
        return self.suggested_results.pop(0)


class FakeResolver:
    def __init__(self, table):
        self.table = table

    def resolve_all(self, suggestions):
        return [self.table.get(s.value, ResolvedSuggestion(s, error="nf")) for s in suggestions]


def ev(selected, conf=ConfidenceLevel.HIGH, score=80.0, warnings=(), candidates=None):
    e = EvidenceRecord(selected=list(selected), confidence_level=conf, confidence_score=score)
    e.warnings = list(warnings)
    e.candidates = list(candidates if candidates is not None else selected)
    e.confidence_rationale = f"rat{len(selected)}"
    return e


def run(agent, resolver, s):
    orch = PipelineOrchestrator(ProjectState())
    out = orch._find_citations_per_marker(agent, s, None, bare_context(s), resolver)
    return out, orch


def test_mixed_sentence_ref_and_suggested_keeps_slots():
    s = sentence("Rac1 acts on PAK (REF) as shown before (PMID: 5).")
    p5 = cand("5")
    agent = FakeAgent(
        search_results=[ev([cand("1")], score=70)],
        suggested_results=[ev([p5], conf=ConfidenceLevel.MEDIUM, score=60,
                              warnings=[Warning(code="suggested_weak_match", message="m")])],
    )
    resolver = FakeResolver({"5": ResolvedSuggestion(s.markers[1].suggestions[0], [p5], source="pubmed")})
    out, orch = run(agent, resolver, s)

    assert [c.pmid for c in out.selected] == ["1", "5"]
    assert out.slot_sizes == [1, 1]
    assert out.confidence_level == ConfidenceLevel.MEDIUM
    assert out.confidence_score == 65.0
    assert [w.code for w in out.warnings] == ["suggested_weak_match"]
    # the REF search got its sub-claim and was told to avoid the suggested paper
    temp, ctx = agent.search_calls[0]
    assert temp.marker_type == MarkerType.REF and ctx.sub_claim == "Rac1 acts on PAK"
    assert ctx.exclusions == ["5"]
    temp, _marker, _resolved, ctx2 = agent.suggested_calls[0]
    assert temp.clean_text == s.clean_text            # full sentence is the claim
    assert ctx2.sub_claim == "as shown before"
    assert "||" in out.confidence_rationale
    # one verifier context per selected citation, in order
    assert [c.sub_claim for c in orch._contexts["S001"]] == ["Rac1 acts on PAK", "as shown before"]


def test_single_suggested_marker_uses_full_claim():
    s = sentence("Spines grow (Battison et al. 2024).")
    s.clean_text = "Spines grow."
    p = cand("9")
    agent = FakeAgent([], [ev([p])])
    resolver = FakeResolver({"Battison 2024": ResolvedSuggestion(s.markers[0].suggestions[0], [p])})
    out, _ = run(agent, resolver, s)
    assert out.slot_sizes == [1] and [c.pmid for c in out.selected] == ["9"]
    temp, marker, resolved, ctx = agent.suggested_calls[0]
    assert temp.clean_text == "Spines grow." and ctx.sub_claim == ""
    assert out.confidence_rationale == "rat1"


def test_duplicate_across_markers_is_replaced_by_alternative():
    s = sentence("A (REF) and B (REF).")
    dup = cand("1")
    alt = cand("2")
    agent = FakeAgent([
        ev([cand("1")]),
        ev([dup], candidates=[dup, alt]),   # agent repeats paper 1; alt is only a candidate
    ])
    out, _ = run(agent, FakeResolver({}), s)
    # Second slot must not reuse PMID 1; the next unique candidate the agent saw fills it
    assert out.slot_sizes == [1, 1]
    assert [c.pmid for c in out.selected] == ["1", "2"]
    assert agent.search_calls[1][1].exclusions == ["1"]  # exclusion list passed to the agent

    # With no unique candidate at all the slot stays empty
    agent = FakeAgent([ev([cand("1")]), ev([dup], candidates=[dup])])
    out, _ = run(agent, FakeResolver({}), s)
    assert out.slot_sizes == [1, 0] and [c.pmid for c in out.selected] == ["1"]


def test_refs_marker_in_multi_marker_sentence_takes_several():
    s = sentence("Method (REFS) then result (PMID: 3).")
    p3 = cand("3")
    agent = FakeAgent([ev([cand("1"), cand("2"), cand("3"), cand("4")])], [ev([p3])])
    resolver = FakeResolver({"3": ResolvedSuggestion(s.markers[1].suggestions[0], [p3])})
    out, _ = run(agent, resolver, s)
    assert out.slot_sizes == [3, 1]
    # The suggested paper (3) is resolved first and excluded from the REFS search
    assert [c.pmid for c in out.selected] == ["1", "2", "4", "3"]
    temp, ctx = agent.search_calls[0]
    assert temp.marker_type == MarkerType.REFS
    assert "3" in ctx.exclusions


def test_unresolved_suggested_marker_reports_error():
    s = sentence("Claim (PMID: 404).")
    e = ev([], conf=ConfidenceLevel.LOW, score=0)
    e.retrieval_error = "None of the author-suggested citations could be resolved"
    agent = FakeAgent([], [e])
    out, _ = run(agent, FakeResolver({}), s)
    assert out.selected == [] and out.slot_sizes == [0]
    assert out.retrieval_error
    assert out.confidence_level == ConfidenceLevel.LOW


def test_searched_per_marker_predicate_covers_suggested_markers():
    assert sentence("Claim (PMID: 1).").searched_per_marker is True
    assert sentence("Claim (REF).").searched_per_marker is False
    assert sentence("A (REFS) and B (REFS).").searched_per_marker is False
    assert sentence("A (REF) and B (REFS).").searched_per_marker is True
    assert sentence("A (REFS) and B (Smith 2020).").slot_count == 2
    assert sentence("A (REFS) and B (REFS).").slot_count == 1
