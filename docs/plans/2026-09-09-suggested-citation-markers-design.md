# Author-Suggested Citation Markers

**Date:** 2026-09-09
**Status:** Implemented

## Goal

Besides `(REF)` / `(REFS)`, recognise parentheticals in which the author already
names the reference, look the reference up, score how well it supports the
sentence with the same AI confidence scoring used for searched citations, and
let the author confirm, replace, or leave it in the Review tab:

```
(PMID: 32879322)          (PMC11413553)             (doi: 10.1101/2024.01.03.574066)
(Battison et al. 2024)    (Smith and Jones, 2020)   (Smith 2019a, 2021)
(PMC11413553, PMC3159129) (PMID: 32879322; Battison et al. 2024)   (REF, PMID: 32879322)
```

## Grammar (`src/utils/markers.py`)

* Candidate = innermost parenthetical. Its whole content must parse as a list of
  citation items (separated by `,` `;` or ` and `, optional lead-in such as
  `see`, `e.g.,`, `reviewed in`); anything else is prose. `(n = 12)`,
  `(Fig. 2B)`, `(December 2024)`, `(Duke University, 2024)`, `(NIH 2020)` are
  rejected by the grammar, a first-word stoplist, an organisation-word list, and
  an all-caps-acronym rule.
* Items: `REF`/`REFS` (uppercase only), `PMID[:] n` (a bare number after a PMID
  item is another PMID), `PMC n` / `PMCID: PMC n`, `doi:`/`https://doi.org/`/bare
  `10.xxxx/...` (trailing period stripped), and `Name [et al. | and Name] [,] YEAR[a-z]`
  with lowercase particles, apostrophes, hyphens, accented letters, initials, and
  a bare year continuing the previous author (`Smith 2019, 2021`).
* A `REF` token inside a suggested marker means "verify and also search for one
  more" (`MarkerSpec.extra_search`; `REFS` means up to *max refs* more).
  `(REF, REF)` with no suggestions is simply a `(REFS)` marker.
* One level of nested parentheses is allowed so Lancet-style DOIs such as
  `10.1016/S0140-6736(20)30183-5` parse; when an outer pair is prose, its inner
  parentheticals are still examined.
* `MarkerConfig(detect_ids, detect_author_year)` comes from two new
  `ProjectSettings` flags (Input tab checkboxes, both default on). With both off
  the grammar is exactly the original `\((REFS?)\)` regex. The orchestrator
  records the configuration it ran with in `ProjectState.run_marker_config`;
  export re-scans the DOCX with that (old project files without it use the
  `(REF)/(REFS)`-only grammar) and additionally requires each DOCX marker's text
  to match the slot it is paired with.
* The sentence splitter no longer splits inside a balanced parenthetical, so
  `(Smith, J. A., 2020)` and `(cf. Smith 2020)` stay in one sentence; a stray
  unclosed `(` has no effect.

## Data model

* `SentenceRecord.markers: list[MarkerSpec]` (text, kind, span, suggestions,
  extra_search). `marker_type`/`marker_count` remain as summaries. An after-
  validator rebuilds `markers` from `raw_text` with the `(REF)/(REFS)`-only
  grammar for project files saved before this change.
* `EvidenceRecord.slot_sizes`: how many entries of `selected` belong to each
  marker, in order. `slot_ranges(n)` derives the old layout when absent (one
  marker owns everything; several markers own one citation each).
  `remove_selected` / `insert_into_slot` keep both in sync; the Review tab uses them.
* `ReviewDecision.REJECTED` ("Leave unchanged") now counts as resolved.
* `CitationCandidate.pmcid` (parsed from PubMed `ArticleId IdType="pmc"` and
  Europe PMC `pmcid`; stored in a new library column added by migration; written
  into NIH-grant bibliography entries as `PMCID:`).

## Resolution (`src/services/suggestion_resolver.py`)

Order: user library, PubMed (article cache), Europe PMC, bioRxiv, medRxiv.

| kind | lookups |
|---|---|
| PMID | library → EFetch → Europe PMC `EXT_ID:n AND SRC:MED` |
| PMCID | library → NCBI ID Converter (`/pmc/utils/idconv/v1.0/`) then ELink `dbfrom=pmc db=pubmed` → EFetch → Europe PMC `PMCID:` |
| DOI | library → ESearch `[doi]` then `[aid]` (record kept only if its DOI matches) → Europe PMC `DOI:"..."` → bioRxiv → medRxiv (`v2` suffix stripped for `10.1101/` DOIs) |
| author-year | library (first author, then any author) → ESearch `Last[1au] AND YEAR[dp]`, then `YEAR-1:YEAR+1[dp]`, then `[au]` → Europe PMC `AUTH:"Last" AND PUB_YEAR:` ; top 10 records plus the ESearch total are handed to the agent |

The sandbox used for development had no network access; the query forms follow
the documented APIs and are covered by unit tests with mocked HTTP, but they have
not been exercised live. If a lookup form turns out to be wrong, the resolver
degrades to the next source and the marker shows an "unresolved" warning.

## Agent (`LLMCitationAgent.evaluate_suggested`)

Separate system prompt: confirm each suggestion, choose among author-year matches
using the claim, score 0-100 with a reason, never substitute a different paper,
optional `alternatives`, `unresolved` with reasons, plus the usual
confidence/verification/snippets fields. Post-processing pins identifier
suggestions to the record they resolved to, validates author-year picks (author
present, year within one), cites duplicate identifiers once, and emits warnings:
`suggested_unresolved`, `suggested_weak_match` (<50), `suggested_ambiguous`,
`suggested_unscored`, `suggested_duplicate`, `suggested_alternative`, `retracted`.
HIGH confidence is capped to MEDIUM when any suggestion is unresolved, ambiguous,
or unscored. If the agent fails, unambiguous resolutions are selected with
confidence UNRESOLVED so the marker is never silently dropped.

## Orchestrator

A lone `(REF)`/`(REFS)` marker uses the old single search. Any other sentence goes
marker by marker (`_find_citations_per_marker`): searches for `REF`/`REFS`
(`REFS` now honours *max refs* in multi-marker sentences), resolve + evaluate for
suggested markers, duplicate exclusion across markers, results concatenated with
`slot_sizes`.

## Review tab and export

* Markers are highlighted in the sentence (suggested ones in blue), the list shows
  the marker text, per-reference rows carry the marker they belong to, the detail
  view shows the author's suggestion, the per-citation score/reason, and the AI
  assessment. Filter "Author-Suggested Only".
* "Leave unchanged" (text and per-reference modes) keeps the original text.
* Export (`src/pipeline/export_slots.py` + `main_window`): per marker slot,
  `cite` → formatted citation, `unresolved` `(REF)/(REFS)` → `[?]` (unchanged
  behaviour), suggested markers without a confirmed citation, and any skipped
  marker → original text kept. Replacements are planned in document order
  (bibliography numbering) and applied right-to-left within each paragraph by
  occurrence in the original text, so an inserted citation can never be
  mistaken for a later marker. The completion dialog reports counts.
* Before a run, a dialog reports how many suggested markers were found and offers
  "Verify all" / "Only (REF)/(REFS)" (this run only) / Cancel.

## Merge onto the tracked-document architecture (2026-09-09)

The feature was first built against the February snapshot and then re-applied
on top of the September line (tracked AIREFS fields, headless `docx_export`,
tool-based agent, concurrent orchestrator, independent verifier). What changed
in the port:

* **Agent.** `evaluate_suggested` answers through a `submit_suggested_evaluation`
  tool (`services/search_tools.py`) instead of a JSON text reply; the loop,
  deadline call and error handling are shared with `find_citations` (`_run_loop`).
* **Orchestrator.** `SentenceRecord.searched_per_marker` is true for any sentence
  with a suggested marker (plus the existing mixed (REF) rule); such sentences go
  through `_find_citations_per_marker`, which keeps `slot_sizes` and one
  `ClaimContext` per selected citation for the verifier. All-(REFS) sentences keep
  the single combined search (`slot_count == 1`, whole list at every marker).
  After verification `cap_suggested_confidence` re-applies the HIGH cap.
* **Export.** `renumber_plan.build_renumber_plan` scans the DOCX with
  `export_marker_config`, matches markers to slots by order *and* text
  (`export_slots`), and records an action per marker; `docx_export.write_new_markers`
  writes them right to left within a paragraph at their own offsets, and markers
  left as written take no number and no field (`plan.written_count` feeds
  `validate_before_save`).
* **Review decisions.** "Leave unchanged" is `ReviewDecision.SKIPPED` (resolved;
  export keeps the text). `REJECTED` keeps its earlier meaning (not resolved; a
  (REF)/(REFS) marker exports as `[?]`).
* **Review tab.** The reference cards are the source of truth; `selected` and
  `slot_sizes` are rebuilt from them (grouped by `SingleRefWidget.slot`) once every
  card is decided. Several papers picked for one marker of a multi-marker sentence
  all go into that marker's slot.
* **Clients.** `claude_client.make_client` accepts SDK builds on `httpx2`.

## Assumptions made without user input

1. A poorly matching suggestion stays selected (with a warning and score); the
   author decides. Alternatives are offered, never substituted.
2. Every fully-parsed citation parenthetical is a marker; detection is toggleable
   per kind and confirmed before each run.
3. Unconfirmed or skipped suggested markers keep their text on export.
4. Bare numbers without a `PMID` prefix are not PMIDs; a bare `10.xxxx/...` DOI is.
5. Known limitations: `(Smith et al., 2020a,b)`, `(Smith et al., in press)`,
   trailing qualifiers such as `(Smith 2020, p. 12)`, narrative cites
   `Smith et al. (2020)`, organisation authors (`(World Health Organization, 2020)`
   is deliberately ignored), and bracket/superscript styles are not detected.
6. A `(REF)` that precedes a suggested marker in the same sentence is told to
   avoid the suggested paper (suggestions are resolved first).
