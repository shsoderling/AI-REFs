"""Claim context: neighbours, section boundaries, windows, per-marker sub-claims."""

from src.models.sentence import MarkerType, SentenceRecord
from src.pipeline.claim_context import (
    CONTEXT_HEADER, ClaimContext, bare_context, build_claim_context, sub_claims_for,
)


def S(id, para, text, section=None, marker=None, count=0):
    raw = text if marker is None else f"{text[:-1]} ({marker.value})."
    return SentenceRecord(id=id, paragraph_index=para, raw_text=raw, clean_text=text,
                          section=section, marker_type=marker, marker_count=count,
                          marker_types=[marker] * count if marker else [])


DOC = [
    S("S001", 0, "Intro sentence one.", "Introduction"),
    S("S002", 0, "Intro sentence two.", "Introduction"),
    S("S003", 2, "Spines are dynamic.", "Results"),
    S("S004", 2, "They enlarge with LTP.", "Results"),
    S("S005", 2, "These changes require Rac1.", "Results", MarkerType.REF, 1),
    S("S006", 2, "Loss of Rac1 blocks them.", "Results"),
    S("S007", 2, "Unrelated trailing sentence.", "Results"),
    S("S008", 4, "Discussion begins here.", "Discussion"),
]


def test_neighbours_come_from_the_same_section_and_paragraph():
    ctx = build_claim_context(DOC, DOC[4])
    assert ctx.claim == "These changes require Rac1."
    assert ctx.section == "Results"
    assert ctx.preceding == ["Spines are dynamic.", "They enlarge with LTP."]
    assert ctx.following == ["Loss of Rac1 blocks them."]
    assert ctx.paragraph == ("Spines are dynamic. They enlarge with LTP. "
                             "[[These changes require Rac1.]] Loss of Rac1 blocks them. "
                             "Unrelated trailing sentence.")


def test_preceding_stops_at_a_section_boundary_and_following_at_the_paragraph():
    ctx = build_claim_context(DOC, DOC[2])           # first sentence of Results
    assert ctx.preceding == []                       # Intro sentences are another section
    assert ctx.following == ["They enlarge with LTP."]
    last = build_claim_context(DOC, DOC[7])
    assert last.preceding == [] and last.following == [] and last.paragraph == ""


def test_long_paragraph_is_windowed_around_the_claim():
    filler = [S(f"F{i:03d}", 9, f"Filler sentence number {i} with some words in it.", "R")
              for i in range(60)]
    claim = S("C001", 9, "The claim sits in the middle of it all.", "R")
    doc = filler[:30] + [claim] + filler[30:]
    ctx = build_claim_context(doc, claim, max_paragraph_chars=300)
    assert len(ctx.paragraph) <= 302
    assert "[[The claim sits in the middle of it all.]]" in ctx.paragraph
    assert ctx.paragraph.startswith("…") and ctx.paragraph.endswith("…")
    assert len(ctx.preceding) == 2


def test_sub_claims_split_like_the_orchestrator_did():
    s = SentenceRecord(id="M", raw_text="A is true (REF) and B is true (REFS) as shown.",
                       clean_text="A is true and B is true as shown.", marker_count=2)
    assert sub_claims_for(s) == [("A is true", "and B is true"), ("and B is true", "as shown.")]


def test_agent_message_orders_claim_context_extras_and_exclusions():
    ctx = build_claim_context(DOC, DOC[4]).with_marker("These changes", "require Rac1.", ["11", "10.1/x"])
    msg = ctx.to_agent_message(num_refs=2, extras="Document research domains: synapses")
    claim_pos = msg.index("CLAIM — find 2 references")
    sub_pos = msg.index('Specifically the part ending at this (REF) marker: "These changes"')
    ctx_pos = msg.index(CONTEXT_HEADER)
    extras_pos = msg.index("Document research domains")
    excl_pos = msg.index("do NOT select any of these: 10.1/x, 11")
    assert claim_pos < sub_pos < ctx_pos < extras_pos < excl_pos
    assert "Section: Results" in msg and 'Preceding: "Spines are dynamic."' in msg
    assert "(followed by: \"require Rac1.\")" in msg


def test_bare_context_has_no_context_block():
    ctx = bare_context(SentenceRecord(id="X", clean_text="These changes require Rac1."))
    assert not ctx.has_context and ctx.to_context_block() == ""
    assert bare_context(DOC[4]).to_context_block() == f"{CONTEXT_HEADER}\nSection: Results"
    assert ctx.to_agent_message(1) == 'CLAIM — find 1 reference that support this sentence:\n"These changes require Rac1."'


def test_keywords_prefer_the_sub_claim_and_drop_stopwords():
    ctx = ClaimContext(claim="These changes require Rac1 signalling in the hippocampus.",
                       sub_claim="Rac1 signalling")
    assert ctx.keywords(limit=4) == ["rac1", "signalling", "changes", "require"]
