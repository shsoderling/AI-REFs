"""Independent verification of the agent's selections.

The search agent grades its own work; this module does not trust that.
For every selected paper a separate Claude call reads the claim (with its
context) and the paper's abstract, or its open-access full text when the
abstract is not enough, and records a verdict with a verbatim quote.  The
quote is checked against the source text so a fabricated quote cannot
count as evidence.  Deterministic checks run first: retracted papers are
flagged and preprints are swapped for their published journal version.

``apply_verdicts`` then sets the sentence's confidence and verification
status from the verdicts alone (the verifier decides).
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

import anthropic

from ..models.citation import CitationCandidate
from ..models.evidence import (
    CitationVerdict, ConfidenceLevel, EvidenceRecord, Verdict, VerificationStatus, Warning,
)
from ..services.claude_client import ClaudeCaller
from ..services.search_tools import RECORD_VERDICT, find_tool_use
from ..services.tool_executor import fulltext_passages_for
from .claim_context import ClaimContext

logger = logging.getLogger(__name__)

LogCallback = Callable[[str, str], None]

VERIFIER_SYSTEM = """\
You are a strict scientific fact-checker. You are given a CLAIM from a \
manuscript and the TEXT of a paper (its abstract, or passages of its full \
text). Decide whether the TEXT supports the CLAIM:

- supports: the text directly reports or states what the claim says.
- partial: the text is related (same topic, system or mechanism) but does \
not establish the specific claim, or only supports part of it.
- not_supported: the text does not address the claim, or contradicts it.

Quote one verbatim sentence or clause from the TEXT that best supports the \
claim. Copy it exactly; never paraphrase, never quote the claim itself. \
Leave the quote empty for not_supported. Surrounding CONTEXT sentences are \
provided only to understand the claim; the paper does not need to support \
them. Call record_verdict exactly once.
"""

_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalise(text: str) -> str:
    """Lower-case words separated by single spaces; punctuation and quotes dropped."""
    return _NON_WORD.sub(" ", (text or "").lower()).strip()


def quote_occurs(quote: str, text: str, shingle: int = 8, threshold: float = 0.8) -> bool:
    """True when the quote occurs in the text (normalised), or when at least
    ``threshold`` of its ``shingle``-word windows do (minor punctuation edits)."""
    q, t = normalise(quote), normalise(text)
    if not q or not t:
        return False
    if q in t:
        return True
    words = q.split()
    if len(words) < shingle:
        return False
    windows = [" ".join(words[i:i + shingle]) for i in range(len(words) - shingle + 1)]
    hits = sum(1 for w in windows if w in t)
    return hits / len(windows) >= threshold


def is_preprint(cand: CitationCandidate) -> bool:
    return (cand.source == "biorxiv"
            or any(pt.lower() == "preprint" for pt in cand.publication_types)
            or cand.doi.lower().startswith("10.1101/"))


def _has_warning(evidence: EvidenceRecord, code: str, key: str) -> bool:
    return any(w.code == code and key in w.message for w in evidence.warnings)


def deterministic_checks(evidence: EvidenceRecord, biorxiv=None, europepmc=None, pubmed=None,
                         log: Optional[LogCallback] = None) -> int:
    """Retraction flags and preprint swaps; runs even when the LLM verifier is off.

    A retracted selection demotes the sentence to LOW. A preprint whose
    journal version can be fetched is replaced in its slot (the preprint
    stays among the candidates). Returns the number of swaps.
    """
    emit = log or (lambda *a: None)
    swapped = 0
    for i, cand in enumerate(list(evidence.selected)):
        if cand.is_retracted and not _has_warning(evidence, "retracted", cand.title[:40]):
            evidence.warnings.append(Warning(
                code="retracted", severity="high",
                message=f"Retracted: {cand.title[:60]} — choose another reference"))
            evidence.confidence_level = ConfidenceLevel.LOW
            evidence.confidence_score = min(evidence.confidence_score, 20.0)
            evidence.verification_status = VerificationStatus.MISMATCH
            emit("warning", f"    Retracted selection: {cand.title[:60]}")

        if not is_preprint(cand):
            continue
        published = cand.published_doi
        if not published and biorxiv is not None and cand.doi:
            try:
                latest = biorxiv.fetch_preprint(cand.doi)
            except Exception as exc:                        # network hiccup: keep going
                logger.warning(f"bioRxiv lookup failed for {cand.doi}: {exc}")
                latest = None
            published = latest.published_doi if latest is not None else ""
        if not published:
            continue
        cand.published_doi = published

        journal = None
        if europepmc is not None:
            try:
                journal = europepmc.fetch_by_doi(published)
            except Exception as exc:
                logger.warning(f"Europe PMC lookup failed for {published}: {exc}")
        if journal is None and pubmed is not None:
            try:
                pmids, _ = pubmed.search(f"{published}[doi]", max_results=1)
                journal = pubmed.fetch_article(pmids[0]) if pmids else None
            except Exception as exc:
                logger.warning(f"PubMed lookup failed for {published}: {exc}")

        if journal is None:
            if not _has_warning(evidence, "published_version_available", published):
                evidence.warnings.append(Warning(
                    code="published_version_available", severity="medium",
                    message=f"Preprint {cand.doi} has a journal version ({published}) "
                            f"that could not be fetched"))
            continue

        if not any(c is cand for c in evidence.candidates):
            evidence.candidates.append(cand)
        if not any(c is journal for c in evidence.candidates):
            evidence.candidates.append(journal)
        evidence.selected[i] = journal
        evidence.warnings.append(Warning(
            code="preprint_swapped", severity="low",
            message=f"Preprint swapped for its journal version: {published}"))
        emit("info", f"    Preprint {cand.doi} swapped for journal version {published}")
        swapped += 1
    return swapped


def passages_text(passages: list[dict]) -> str:
    return "\n\n".join(f"[{p.get('section') or 'Body'}] {p['text']}" for p in passages)


class CitationVerifier:
    """One verdict per selected paper, quoted from its abstract or full text."""

    MAX_TEXT_CHARS = 8000

    def __init__(self, caller: ClaudeCaller, europepmc=None, use_full_text: bool = True,
                 log: Optional[LogCallback] = None):
        self.caller = caller
        self.europepmc = europepmc
        self.use_full_text = use_full_text and europepmc is not None
        self._log = log or (lambda *a: None)

    # ── prompt ───────────────────────────────────────────────────────

    @staticmethod
    def build_prompt(ctx: ClaimContext, cand: CitationCandidate, text: str, source: str) -> str:
        lines = ["CLAIM:", f"\"{ctx.claim}\""]
        if ctx.sub_claim:
            lines.append(f"Specifically the part ending at the citation marker: \"{ctx.sub_claim}\"")
        context = ctx.to_context_block()
        if context:
            lines += ["", context]
        lines += [
            "",
            f"PAPER: {cand.title} ({cand.first_author_year}; {cand.journal_abbrev or cand.journal})",
            f"TEXT ({'abstract' if source == 'abstract' else 'full-text passages'}):",
            text[:CitationVerifier.MAX_TEXT_CHARS],
            "",
            "Does the TEXT support the CLAIM? Call record_verdict.",
        ]
        return "\n".join(lines)

    # ── one paper ────────────────────────────────────────────────────

    def _ask(self, ctx: ClaimContext, cand: CitationCandidate, text: str, source: str) -> CitationVerdict:
        verdict = CitationVerdict(key=cand.pmid or cand.doi or cand.title, source=source)
        prompt = self.build_prompt(ctx, cand, text, source)
        response = self.caller.create(
            system=VERIFIER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[RECORD_VERDICT],
            max_tokens=4096,
        )
        block = find_tool_use(response, RECORD_VERDICT["name"])
        if block is None:
            verdict.reason = "verifier gave no verdict"
            return verdict
        data = block.input or {}
        raw = str(data.get("verdict", "")).lower()
        verdict.verdict = {
            "supports": Verdict.SUPPORTS,
            "partial": Verdict.PARTIAL,
            "not_supported": Verdict.NOT_SUPPORTED,
        }.get(raw, Verdict.UNVERIFIED)
        verdict.quote = str(data.get("quote", "") or "").strip()
        verdict.reason = str(data.get("reason", "") or "").strip()
        verdict.quote_found = bool(verdict.quote) and quote_occurs(verdict.quote, text)
        if verdict.verdict == Verdict.SUPPORTS and not verdict.quote_found:
            # A supporting verdict must be backed by a real quote.
            verdict.verdict = Verdict.PARTIAL
            verdict.reason = (verdict.reason + " (quote not found in the source text)").strip()
        return verdict

    def verify_one(self, ctx: ClaimContext, cand: CitationCandidate) -> CitationVerdict:
        """Abstract first; open-access full text when the abstract is missing
        or only gives a partial verdict. At most two calls per paper."""
        key = cand.pmid or cand.doi or cand.title
        text, source = (cand.abstract or "").strip(), "abstract"
        keywords = ctx.keywords()
        passages = None
        if not text and self.use_full_text:
            passages = fulltext_passages_for(cand, keywords, self.europepmc)
            if passages and passages["passages"]:
                text, source = passages_text(passages["passages"]), "full_text"
        if not text:
            return CitationVerdict(key=key, reason="no abstract or full text available")

        try:
            verdict = self._ask(ctx, cand, text, source)
            if (verdict.verdict == Verdict.PARTIAL and source == "abstract" and self.use_full_text):
                extra = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]{3,}", verdict.reason)][:4]
                passages = fulltext_passages_for(cand, keywords + extra, self.europepmc)
                if passages and passages["passages"]:
                    second = self._ask(ctx, cand, passages_text(passages["passages"]), "full_text")
                    if second.verdict == Verdict.SUPPORTS and second.quote_found:
                        verdict = second
            return verdict
        except anthropic.APIError as exc:
            self._log("warning", f"    Verifier API error for {key}: {exc}")
            return CitationVerdict(key=key, source=source, reason=f"verifier error: {exc}")
        except Exception as exc:                            # never abort the pipeline
            logger.exception("Verifier failed")
            self._log("warning", f"    Verifier error for {key}: {exc}")
            return CitationVerdict(key=key, source=source, reason=f"verifier error: {exc}")

    # ── one sentence ─────────────────────────────────────────────────

    def verify_evidence(self, contexts: list[ClaimContext], evidence: EvidenceRecord) -> None:
        """Fill ``evidence.verdicts`` (aligned with ``selected``) and apply them."""
        if not evidence.selected or not contexts:
            return
        evidence.verdicts = []
        for i, cand in enumerate(evidence.selected):
            ctx = contexts[i] if len(contexts) == len(evidence.selected) else contexts[0]
            verdict = self.verify_one(ctx, cand)
            evidence.verdicts.append(verdict)
            quote = f" “{verdict.quote[:80]}…”" if verdict.quote else ""
            self._log("info", f"    Verify [{evidence.sentence_id}/{i + 1}] {verdict.verdict.value}"
                              f" ({verdict.source or 'no text'}){quote}")
        apply_verdicts(evidence)


def apply_verdicts(evidence: EvidenceRecord) -> None:
    """Confidence and verification status from the verdicts (the verifier decides).

    supports (quote found) → HIGH/verified; partial → MEDIUM/partial;
    not supported, retracted, or a quote that is not in the source → LOW/mismatch;
    nothing verifiable → the agent's values stand, with a warning.
    """
    verdicts = evidence.verdicts
    if not evidence.selected or not verdicts:
        return
    retracted = any(c.is_retracted for c in evidence.selected)
    known = [v for v in verdicts if v.verdict != Verdict.UNVERIFIED]
    unverified = [v for v in verdicts if v.verdict == Verdict.UNVERIFIED]
    fabricated = [v for v in verdicts if v.quote and not v.quote_found]
    failing = [v for v in known if v.verdict == Verdict.NOT_SUPPORTED]

    def title_for(v: CitationVerdict) -> str:
        for c in evidence.selected:
            if v.key in (c.pmid, c.doi, c.title):
                return c.title[:60]
        return v.key

    summary = "; ".join(f"{title_for(v)[:40]}: {v.verdict.value}" for v in verdicts)

    if failing or retracted or fabricated:
        evidence.verification_status = VerificationStatus.MISMATCH
        evidence.confidence_level = ConfidenceLevel.LOW
        evidence.confidence_score = min(evidence.confidence_score, 39.0)
        for v in failing:
            evidence.warnings.append(Warning(
                code="not_supported", severity="high",
                message=f"Verifier: {title_for(v)} does not support the claim — {v.reason}"))
        for v in fabricated:
            if v not in failing:
                evidence.warnings.append(Warning(
                    code="quote_not_found", severity="high",
                    message=f"Verifier: the quoted evidence for {title_for(v)} is not in the paper"))
    elif known and all(v.verdict == Verdict.SUPPORTS for v in known):
        evidence.verification_status = VerificationStatus.VERIFIED
        evidence.confidence_level = ConfidenceLevel.HIGH
        evidence.confidence_score = max(evidence.confidence_score, 80.0)
    elif known:
        evidence.verification_status = VerificationStatus.PARTIAL
        evidence.confidence_level = ConfidenceLevel.MEDIUM
        evidence.confidence_score = min(max(evidence.confidence_score, 40.0), 69.0)

    if unverified:
        evidence.warnings.append(Warning(
            code="unverified", severity="low",
            message="Verifier could not check: " + "; ".join(
                f"{title_for(v)[:40]} ({v.reason})" for v in unverified)))

    if summary:
        evidence.confidence_rationale = (
            f"{evidence.confidence_rationale} | Verifier: {summary}".strip(" |"))
