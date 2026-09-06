"""Independent verification: quotes, verdict mapping, deterministic checks, verifier calls."""

import anthropic
import httpx

from src.models.evidence import (
    CitationVerdict, ConfidenceLevel, EvidenceRecord, Verdict, VerificationStatus,
)
from src.pipeline.claim_context import ClaimContext
from src.pipeline.verification import (
    CitationVerifier, apply_verdicts, deterministic_checks, is_preprint, quote_occurs,
)
from src.services.claude_client import ClaudeCaller
from tests.fakes import (
    FakeAnthropic, FakeBioRxiv, FakeEuropePMC, FakePubMed, article, message, text_block,
    verdict_msg,
)

ABSTRACT = ("Rac1 activation increased dendritic spine density in cultured hippocampal "
            "neurons. Spine enlargement required actin remodelling and was blocked by "
            "the Rac1 inhibitor NSC23766 within two hours of treatment.")
CTX = ClaimContext(claim="Rac1 activation increases spine density.", section="Results")


# ── quote matching ───────────────────────────────────────────────────

def test_quote_occurs_normalises_case_whitespace_and_quotes():
    assert quote_occurs("rac1 activation   INCREASED dendritic spine density", ABSTRACT)
    assert quote_occurs("“Spine enlargement required actin remodelling”", ABSTRACT)
    assert not quote_occurs("Rac1 decreased spine density", ABSTRACT)
    assert not quote_occurs("", ABSTRACT) and not quote_occurs("x", "")


def test_quote_occurs_tolerates_a_small_edit_in_a_long_quote():
    edited = ("Rac1 activation increased dendritic spine density in cultured hippocampal "
              "neurons; spine enlargement required actin remodelling and was blocked by "
              "the Rac1 inhibitor NSC23766 within two hours of treatment")
    assert quote_occurs(edited, ABSTRACT)
    assert not quote_occurs("Rac1 made everything better in every single neuron we looked at", ABSTRACT)


# ── verdict application ──────────────────────────────────────────────

def _evidence(*cands, level=ConfidenceLevel.MEDIUM, score=55.0):
    return EvidenceRecord(sentence_id="S001", selected=list(cands), candidates=list(cands),
                          confidence_level=level, confidence_score=score, confidence_rationale="agent")


def _v(kind, key="11", quote="q", found=True, reason="r"):
    return CitationVerdict(key=key, verdict=kind, quote=quote, quote_found=found, reason=reason,
                           source="abstract")


def test_all_supported_becomes_high_and_verified():
    ev = _evidence(article("11", "A"), article("12", "B"))
    ev.verdicts = [_v(Verdict.SUPPORTS, "11"), _v(Verdict.SUPPORTS, "12")]
    apply_verdicts(ev)
    assert (ev.confidence_level, ev.verification_status) == (ConfidenceLevel.HIGH, VerificationStatus.VERIFIED)
    assert ev.confidence_score == 80.0 and "Verifier: A: supports; B: supports" in ev.confidence_rationale


def test_partial_becomes_medium():
    ev = _evidence(article("11", "A"), level=ConfidenceLevel.HIGH, score=95)
    ev.verdicts = [_v(Verdict.PARTIAL, quote="", found=False)]
    apply_verdicts(ev)
    assert (ev.confidence_level, ev.verification_status) == (ConfidenceLevel.MEDIUM, VerificationStatus.PARTIAL)
    assert ev.confidence_score == 69.0 and ev.warnings == []


def test_not_supported_or_missing_quote_becomes_low_with_warnings():
    ev = _evidence(article("11", "A"), article("12", "B"), level=ConfidenceLevel.HIGH, score=90)
    ev.verdicts = [_v(Verdict.NOT_SUPPORTED, "11", quote="", found=False, reason="off topic"),
                   _v(Verdict.PARTIAL, "12", quote="made up", found=False)]
    apply_verdicts(ev)
    assert (ev.confidence_level, ev.verification_status) == (ConfidenceLevel.LOW, VerificationStatus.MISMATCH)
    assert ev.confidence_score == 39.0
    assert [w.code for w in ev.warnings] == ["not_supported", "quote_not_found"]
    assert "A does not support the claim — off topic" in ev.warnings[0].message


def test_unverified_keeps_the_agent_values_and_warns():
    ev = _evidence(article("11", "A"), level=ConfidenceLevel.HIGH, score=90)
    ev.verdicts = [CitationVerdict(key="11", reason="no abstract or full text available")]
    apply_verdicts(ev)
    assert ev.confidence_level == ConfidenceLevel.HIGH and ev.confidence_score == 90
    assert ev.verification_status == VerificationStatus.NOT_CHECKED
    assert ev.warnings[0].code == "unverified"


def test_retracted_selection_is_low_even_when_supported():
    ev = _evidence(article("11", "A", is_retracted=True))
    ev.verdicts = [_v(Verdict.SUPPORTS)]
    apply_verdicts(ev)
    assert ev.confidence_level == ConfidenceLevel.LOW


def test_apply_verdicts_is_a_no_op_without_verdicts():
    ev = _evidence(article("11", "A"))
    apply_verdicts(ev)
    assert ev.confidence_level == ConfidenceLevel.MEDIUM and ev.warnings == []


# ── deterministic checks ─────────────────────────────────────────────

def test_retracted_paper_is_flagged_and_demoted():
    ev = _evidence(article("11", "Bad paper", is_retracted=True), level=ConfidenceLevel.HIGH, score=90)
    deterministic_checks(ev)
    deterministic_checks(ev)                                   # idempotent
    assert [w.code for w in ev.warnings] == ["retracted"]
    assert ev.confidence_level == ConfidenceLevel.LOW and ev.confidence_score == 20.0


def test_preprint_is_swapped_for_its_journal_version():
    preprint = article("", "Rac1 preprint", doi="10.1101/2024.01.01.111", source="biorxiv",
                       publication_types=["Preprint"])
    journal = article("77", "Rac1 in J Neurosci", doi="10.1523/jn.2024.1")
    ev = _evidence(article("11", "Other"), preprint)
    biorxiv = FakeBioRxiv(preprints={"10.1101/2024.01.01.111": article(
        "", "Rac1 preprint", doi="10.1101/2024.01.01.111", published_doi="10.1523/jn.2024.1")})
    epmc = FakeEuropePMC(by_doi={"10.1523/jn.2024.1": journal})

    assert is_preprint(preprint)
    assert deterministic_checks(ev, biorxiv=biorxiv, europepmc=epmc) == 1
    assert [c.pmid for c in ev.selected] == ["11", "77"]        # slot preserved
    assert preprint in ev.candidates and journal in ev.candidates
    assert preprint.published_doi == "10.1523/jn.2024.1"
    assert ev.warnings[0].code == "preprint_swapped"


def test_preprint_swap_falls_back_to_pubmed_and_warns_when_nothing_is_found():
    preprint = article("", "P", doi="10.1101/2024.02.02.222", published_doi="10.1/journal")
    ev = _evidence(preprint)
    pubmed = FakePubMed(by_search={"10.1/journal[doi]": (["88"], 1)},
                        articles={"88": article("88", "Journal version")})
    assert deterministic_checks(ev, europepmc=FakeEuropePMC(), pubmed=pubmed) == 1
    assert ev.selected[0].pmid == "88"

    stuck = _evidence(article("", "P2", doi="10.1101/2024.03.03.333", published_doi="10.1/missing"))
    assert deterministic_checks(stuck, europepmc=FakeEuropePMC(), pubmed=FakePubMed()) == 0
    assert stuck.selected[0].title == "P2"
    assert stuck.warnings[0].code == "published_version_available"


# ── the verifier ─────────────────────────────────────────────────────

def _verifier(client, epmc=None, use_full_text=True):
    return CitationVerifier(ClaudeCaller(client, "claude-test"), epmc, use_full_text=use_full_text)


def test_supports_with_a_real_quote():
    client = FakeAnthropic([verdict_msg("supports", "Rac1 activation increased dendritic spine density", "direct")])
    v = _verifier(client).verify_one(CTX, article("11", "A", abstract=ABSTRACT))
    assert v.verdict == Verdict.SUPPORTS and v.quote_found and v.source == "abstract"
    prompt = client.calls[0]["messages"][0]["content"]
    assert prompt.startswith('CLAIM:\n"Rac1 activation increases spine density."')
    assert "Section: Results" in prompt and ABSTRACT in prompt
    assert [t["name"] for t in client.calls[0]["tools"]] == ["record_verdict"]


def test_supports_with_a_fabricated_quote_is_downgraded_to_partial():
    client = FakeAnthropic([verdict_msg("supports", "Rac1 cures everything", "sure")])
    v = _verifier(client, use_full_text=False).verify_one(CTX, article("11", "A", abstract=ABSTRACT))
    assert v.verdict == Verdict.PARTIAL and not v.quote_found
    assert "quote not found" in v.reason and len(client.calls) == 1


def test_partial_abstract_verdict_triggers_a_full_text_second_pass():
    epmc = FakeEuropePMC(fulltext={"PMC1": [("Results", "Rac1 activation increased spine density by forty percent.")]})
    client = FakeAnthropic([
        verdict_msg("partial", "", "abstract does not give numbers"),
        verdict_msg("supports", "increased spine density by forty percent", "explicit"),
    ])
    v = _verifier(client, epmc).verify_one(CTX, article("11", "A", abstract=ABSTRACT, pmcid="PMC1"))
    assert v.verdict == Verdict.SUPPORTS and v.source == "full_text" and v.quote_found
    assert len(client.calls) == 2 and epmc.fulltext_calls == ["PMC1"]
    assert "full-text passages" in client.calls[1]["messages"][0]["content"]


def test_second_pass_is_kept_only_when_it_supports():
    epmc = FakeEuropePMC(fulltext={"PMC1": [("Results", "Something else entirely about kinases.")]})
    client = FakeAnthropic([verdict_msg("partial", "", "vague"), verdict_msg("not_supported", "", "no")])
    v = _verifier(client, epmc).verify_one(CTX, article("11", "A", abstract=ABSTRACT, pmcid="PMC1"))
    assert v.verdict == Verdict.PARTIAL and v.source == "abstract"


def test_no_text_means_unverified_without_a_call():
    client = FakeAnthropic([])
    v = _verifier(client, FakeEuropePMC()).verify_one(CTX, article("11", "A"))
    assert v.verdict == Verdict.UNVERIFIED and "no abstract" in v.reason and client.calls == []


def test_missing_abstract_uses_full_text_when_available():
    epmc = FakeEuropePMC(fulltext={"PMC1": [("Results", "Rac1 activation increased spine density.")]})
    client = FakeAnthropic([verdict_msg("supports", "Rac1 activation increased spine density", "ok")])
    v = _verifier(client, epmc).verify_one(CTX, article("11", "A", pmcid="PMC1"))
    assert v.verdict == Verdict.SUPPORTS and v.source == "full_text"


def test_api_errors_and_missing_tool_calls_leave_the_paper_unverified():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.InternalServerError("boom", response=httpx.Response(500, request=request), body=None)
    v = _verifier(FakeAnthropic([err])).verify_one(CTX, article("11", "A", abstract=ABSTRACT))
    assert v.verdict == Verdict.UNVERIFIED and "verifier error" in v.reason

    v2 = _verifier(FakeAnthropic([message(text_block("I think so"))])).verify_one(
        CTX, article("11", "A", abstract=ABSTRACT))
    assert v2.verdict == Verdict.UNVERIFIED and "no verdict" in v2.reason


def test_verify_evidence_aligns_contexts_and_applies_verdicts():
    ev = _evidence(article("11", "A", abstract=ABSTRACT), article("12", "B", abstract=ABSTRACT))
    ctx_a = ClaimContext(claim="Rac1 activation increases spine density.", sub_claim="Rac1 activation")
    ctx_b = ClaimContext(claim="Rac1 activation increases spine density.", sub_claim="spine density")
    client = FakeAnthropic([
        verdict_msg("supports", "Rac1 activation increased dendritic spine density", "a"),
        verdict_msg("partial", "", "b"),
    ])
    _verifier(client, use_full_text=False).verify_evidence([ctx_a, ctx_b], ev)
    assert [v.verdict for v in ev.verdicts] == [Verdict.SUPPORTS, Verdict.PARTIAL]
    assert ev.confidence_level == ConfidenceLevel.MEDIUM
    assert 'marker: "Rac1 activation"' in client.calls[0]["messages"][0]["content"]
    assert 'marker: "spine density"' in client.calls[1]["messages"][0]["content"]
