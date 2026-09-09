"""JATS body flattening and keyword-ranked passage selection."""

from src.services.jats import PARAGRAPH_CAP, parse_jats_body, select_passages

JATS = """<?xml version="1.0"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:mml="http://www.w3.org/1998/Math/MathML">
  <front><article-meta><title-group><article-title>Rac1 in spines</article-title></title-group></article-meta></front>
  <body>
    <sec><title>Introduction</title>
      <p>Dendritic spines are small protrusions that carry most excitatory synapses in the brain.</p>
    </sec>
    <sec><title>Results</title>
      <p>Rac1 activation increased spine density by forty percent in cultured hippocampal neurons.</p>
      <table-wrap><table><tr><td>Rac1 spine density table that must not appear anywhere</td></tr></table></table-wrap>
      <fig><caption><p>Figure caption about Rac1 and spine density that should be skipped entirely.</p></caption></fig>
      <sec><title>Time course</title>
        <p>The increase in spine density was detectable within two hours of Rac1 activation.</p>
      </sec>
    </sec>
    <sec><title>Discussion</title>
      <p>These findings place Rac1 upstream of actin remodelling during spine formation.</p>
      <p>Short.</p>
    </sec>
  </body>
  <back><ref-list><ref><mixed-citation>Smith J. Rac1 and spines. 2020.</mixed-citation></ref></ref-list></back>
</article>
"""


def test_parse_jats_body_flattens_sections_and_skips_tables_figures_refs():
    paragraphs = parse_jats_body(JATS)
    titles = [t for t, _ in paragraphs]
    assert titles == ["Introduction", "Results", "Results > Time course", "Discussion"]
    text = " ".join(p for _, p in paragraphs)
    assert "must not appear" not in text and "Figure caption" not in text
    assert "Smith J." not in text
    assert "Short." not in text                       # below the minimum length


def test_parse_jats_body_tolerates_garbage():
    assert parse_jats_body("<not xml") == []
    assert parse_jats_body("<article><front/></article>") == []


def test_select_passages_ranks_by_distinct_keyword_hits_with_results_bonus():
    paragraphs = parse_jats_body(JATS)
    chosen = select_passages(paragraphs, ["Rac1", "spine density", "activation"], max_passages=2)
    assert [p["section"] for p in chosen] == ["Results", "Results > Time course"]
    assert chosen[0]["score"] >= chosen[1]["score"] >= 1
    assert all(set(p) == {"section", "text", "score"} for p in chosen)


def test_select_passages_respects_caps_and_falls_back_to_document_order():
    long_para = ("word " * 400).strip()
    paragraphs = [("Methods", long_para), ("Results", "Nothing relevant here at all, honestly.")]
    chosen = select_passages(paragraphs, ["zebrafish"], max_passages=5, max_chars=5000)
    assert [p["section"] for p in chosen] == ["Methods", "Results"]     # no hits: reading order
    assert len(chosen[0]["text"]) <= PARAGRAPH_CAP + 1 and chosen[0]["text"].endswith("…")

    tight = select_passages(paragraphs, ["zebrafish"], max_passages=5, max_chars=100)
    assert [p["section"] for p in tight] == ["Results"]                 # the long one did not fit
    assert select_passages([], ["x"]) == []
