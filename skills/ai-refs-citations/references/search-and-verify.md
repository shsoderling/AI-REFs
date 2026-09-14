# Search, scoring and verification rules

These are the rules the AI REFs app gives its Claude agent and its independent
verifier, rewritten for you to apply directly. They decide what a user sees as
"the same result as the app".

## Reading a claim

`plan.json` gives, per sentence: `clean_text` (the claim without markers), `section`,
`context.preceding` (up to two earlier sentences of the same section),
`context.following` (the next sentence of the paragraph) and `context.paragraph` (the
paragraph with the claim inside `[[ ]]`). Cite only the claim; the context exists so
that "These mice also showed the deficit" can be searched at all. For a sentence with
several markers, each marker's `sub_claim` is the text that ends at that marker
("Rac1 acts on PAK" for `Rac1 acts on PAK (REF) as shown before (PMID: 5)`) and
`trailing` is what follows it; search for the sub-claim, not the whole sentence.

## Search workflow for (REF) and (REFS)

Budget: about three searches and two fetches per marker. Do not keep searching; pick the
best available match even if it is not perfect, and say so in the rationale.

1. If the user has an AI REFs library (`check_env.py` reports it), search it first with
   the claim's key terms and prefer its papers when relevance is comparable; the user
   curated it. `search_library.py --query "..."` works offline and returns full records.
2. PubMed: one or two well-formed queries. Combine the two or three most specific
   concepts with AND (`Rac1 AND dendritic spine AND hippocampus`); use `[tiab]` for
   phrases, `[au]` for names, `[MeSH Terms]` for canonical concepts, `[dp]` for years.
   Europe PMC (via Paperclip) is a useful second source for full-text hits and
   preprints; bioRxiv for very recent work.
3. Fetch the promising PMIDs and read the abstracts. Ask for open-access full text
   (`get_full_text_article` with the PMCID, or Paperclip) only when an abstract is
   ambiguous about whether the paper supports the claim.
4. Choose. `(REF)`: exactly one paper. `(REFS)`: up to the cap (default 3), best first.
   For a sentence handled marker by marker, never reuse a paper already assigned to an
   earlier marker of the same sentence; take the next-best unique paper instead.

Preferences (the app's defaults; the user can change them):
- Peer-reviewed primary research over reviews, unless the user asked to prefer reviews
  or the claim is a broad statement of the field that a review covers directly.
- Recent publications when relevance is equal.
- Never select a retracted article.
- Self-citations: when the sentence says "we previously showed", "our lab", "our
  earlier work", the author's own paper is wanted. If the user gave an ORCID, search
  `0000-0001-2345-6789[auid] AND <topic>`; otherwise ask for the author's name.

Record for each chosen paper: PMID (or DOI for a preprint), a one-sentence "why", a
supporting snippet from the abstract, confidence HIGH / MEDIUM / LOW with a 0-100
score, and the queries used. If nothing supports the claim, say so (confidence LOW,
no selection) rather than forcing a weak match; the marker will export as `[?]`
unless the user provides a paper.

## Author-suggested citations

The author named the paper. Your job is to confirm it and judge the fit, not to find
a better one.

1. Use `resolve_suggestions.py` first when this shell has network (`check_env.py` says
   `lookups: scripts`). It resolves PMIDs and PMC ids exactly, DOIs through PubMed then
   bioRxiv/medRxiv, and author-year citations through the user's library then PubMed
   (`Last[1au] AND YEAR[dp]`, widening to ±1 year, then any-author position) and Europe
   PMC. Each resolution reports its `source`, `total_matches` and `ambiguous`. When the
   shell has no network, do the same lookups with the connectors: `get_article_metadata`
   for a PMID, `convert_article_ids` for a PMC id, `lookup_article_by_citation` or
   `get_preprint` for a DOI, `search_articles` with `[1au]` and `[dp]` for author-year.
   A lookup that fails because the shell is offline is not evidence that the paper does
   not exist, and the scripts say so in `network_error` rather than letting you
   misreport it.
2. Keep every suggestion that resolves to a real paper, one entry per suggestion, even
   when it supports the claim poorly: give it a low score (below 50 triggers a
   "weak match" warning) and explain why. Never replace an author's suggestion with a
   different paper. If you know or find a better one, offer it as an alternative in
   the review table.
3. Ambiguous author-year matches (several papers by that first author that year): read
   the abstracts and choose the one whose topic matches the claim; search PubMed with
   the author, year and topic words if none of the listed ones fits. Say in the table
   that the pick was among N candidates so the user can confirm it.
4. A pick for an author-year suggestion must actually carry that surname among its
   authors (and the co-author's when `Smith and Jones, 2020` was written) and a year
   within one of the one written; otherwise treat the suggestion as unresolved.
5. Unresolved suggestions (nothing found, or a PMID that returns no record) stay in the
   document as written; report them as unresolved with the reason. Use the tools for at
   most one lookup of your own before giving up (a mistyped PMID is common; a DOI with
   a trailing period is not the error, the scripts strip it).
6. Two suggestions in one marker that turn out to be the same paper (its PMID and its
   DOI) are cited once.
7. `REF` inside a suggestion marker, e.g. `(REF, PMID: 32879322)`: verify the suggestion
   and also search for one more paper (the `extra_search` count in the plan; `REFS`
   inside means up to the cap) exactly as for a `(REF)` marker, excluding the
   suggested paper.

Scores: 100 means the paper's own findings state the claim; 50 related but indirect;
10 unrelated. Sentence confidence: HIGH only if every suggestion scored 70 or more
and none is unresolved, ambiguous or unscored; MEDIUM if all scored 40 or more;
otherwise LOW.

## Verification (every selected paper)

The search agent grades its own work; the verifier does not trust that. For each
selected paper, searched or suggested:

1. Take the abstract (or open-access full-text passages when there is no abstract or
   the abstract only gives a partial answer). Decide:
   - `supports`: the text directly reports or states what the claim says;
   - `partial`: related (same topic, system or mechanism) but does not establish the
     specific claim, or only part of it;
   - `not_supported`: the text does not address the claim or contradicts it.
2. Quote one verbatim sentence or clause from the paper's text that best supports the
   claim. Copy it exactly; never paraphrase and never quote the claim itself. Leave it
   empty for `not_supported`. A `supports` verdict whose quote is not actually in the
   text counts as `partial`.
3. Deterministic checks: a retracted paper is flagged and the sentence drops to LOW.
   A preprint (bioRxiv/medRxiv DOI or publication type "Preprint") whose journal version
   exists is replaced by the journal version (`get_preprint` reports `published_doi`;
   look the DOI up); note the swap for the user.

Sentence-level outcome from the verdicts: all `supports` with real quotes → HIGH,
verified; any `not_supported`, retraction, or fabricated quote → LOW, mismatch (warn);
otherwise MEDIUM, partial. For sentences with author-suggested citations, an
unresolved, ambiguous or unscored suggestion caps the sentence at MEDIUM even when
the verified papers support the claim.

## Warnings to surface in the review table

| Warning | When |
|---|---|
| weak match | a suggested paper scored below 50 |
| ambiguous | an author-year citation matched several papers; say which was chosen |
| unresolved | a suggestion could not be matched to any record |
| unscored | a suggestion resolved but could not be scored (evaluation failed) |
| duplicate | two suggestions in one marker are the same paper; cited once |
| alternative | you propose a better paper than the author's suggestion (offered, not substituted) |
| retracted | the paper is retracted |
| not supported / quote not found | the verifier's verdict |
| preprint swapped | a preprint was replaced by its journal version |
| published version available | a preprint has a journal version that could not be fetched |
