# Persistent Citation Tracking Design

**Date:** 2026-09-05
**Status:** Approved (Phases 0-1 in scope; Phases 2-5 are follow-ups)
**Source:** research workflow over EndNote / Zotero / Mendeley internals, DOCX/OOXML mechanics and this codebase; three independent designs judged and synthesised, then critiqued and revised.


## Context

AI REFs exports a DOCX in which every citation is plain text: a superscript or bracketed number in the body, and a plain-text "References" list built by `format_bib_entry` (src/pipeline/bib_format.py). The only link between an in-text number and the paper it cites is the number itself; the only link between a bibliography line and a record is the `doi:` / `PMID:` tail printed in the text.

When the scientist comes back later (new session, maybe another machine, after editing the document in Word) and adds new `(REF)` markers, the app has to start over: `ExistingCitationParser.analyze()` regex-parses the References heading, the numbered entries and the body numbers; `ExistingRefEnricher` re-queries PubMed for entries without printed identifiers; `compute_renumbering` re-derives identity from PMID > DOI > title; `_do_insert_export` (src/gui/main_window.py:490) rewrites run text, deletes everything from the References heading to the end of the document and rebuilds it. Anything the regexes miss breaks identity, and every session pays for PubMed re-matching.

The user's goal, confirmed this session: make AI REFs "EndNote-like" for its **own** citations, so it recognises and tracks the citations it inserted previously and can insert new ones without starting over. Integration with EndNote, Zotero or other managers is explicitly **not** wanted. Scope for this plan: **Phases 0 and 1** (hardened DOCX writer, then embedded citation fields with an exact, offline reopen). Later phases are listed at the end as follow-ups.

## How reference managers solve this (research summary)

All three major managers converged on the same carrier: a Word **complex field** (`fldChar begin` / `instrText` / `fldChar separate` / cached result runs / `fldChar end`) whose instruction text is `ADDIN <vendor token> <payload>`. Word treats `ADDIN` as opaque add-in data that it never recomputes, the cached result is what co-authors see, and the payload carries identity plus a full copy of the record (the "traveling library").

| | In-text field | Payload | Bibliography | Traveling library |
|---|---|---|---|---|
| EndNote | ` ADDIN EN.CITE ` + XML | `<Cite><Author/><Year/><RecNum/><DisplayText/><record>…<foreign-keys><key app="EN" db-id="…">N</key>…</record></Cite>` | ` ADDIN EN.REFLIST ` field spanning all entries, no payload | every `<record>` in every citation (no abstracts) |
| Zotero | ` ADDIN ZOTERO_ITEM CSL_CITATION {json} ` | `{citationID, properties:{formattedCitation, plainCitation}, citationItems:[{id, uris:[…], itemData:{CSL-JSON}}]}` | ` ADDIN ZOTERO_BIBL {uncited,omitted,custom} CSL_BIBLIOGRAPHY ` | `itemData` in every citation, always on |
| Mendeley Desktop | ` ADDIN CSL_CITATION {json} ` | same CSL shape plus `mendeley:{previouslyFormattedCitation, manualFormatting}` | ` ADDIN Mendeley Bibliography CSL_BIBLIOGRAPHY ` | `itemData` in every citation |

Lessons that drive this design (all verified against vendor docs and source code by the research agents):

- **Identity lives in the in-text fields, never only in the bibliography.** EndNote's `EN.REFLIST` carries no identity at all; the bibliography is derived output.
- **Numbering is derived from field order on every update**, not from a stored number map (citeproc-js takes an ordered list of citation IDs and returns only the citations whose text changed).
- **Embed the full record in every field** so the document is self-sufficient when the library is unavailable (Zotero removed the option to turn this off).
- **Library-local integer keys are fragile**: EndNote's `rec-number` differs between libraries and even between synced copies. Use opaque UUIDs plus global keys (PMID, DOI).
- **Per-cluster ID plus per-record ID**: one field holds N items (a cluster); copy/paste duplicates the cluster ID, so readers re-mint on collision.
- **Hand-edit detection**: store the rendered text in the payload and compare with the visible text on the next run.
- **What kills fields**: Google Docs, Pages, RTF/ODT saves, and "convert to plain text". Track Changes with markup showing corrupts them. Content controls (`w:sdt`) are user-deletable and, critically, invisible to python-docx.

## Verified facts about the current code

- No code touches Word fields, bookmarks, content controls, customXml or docVars today.
- python-docx 1.2.0 is installed (`requirements.txt` says `>=1.1.0`). It has no field API; fields must be hand-built with `OxmlElement`.
- In-memory experiment (this session): a complex field is transparent to today's code. `paragraph.text` excludes `instrText`; the result run appears in `paragraph.runs` with `font.superscript` intact. So a field-wrapped citation is still seen by `ExistingCitationParser`, `find_superscript_citation_runs` and `apply_renumbering` as a plain run. An inline `w:sdt` is **not** seen at all (missing from `paragraph.text` and `runs`).
- `paragraph.text` is `xpath("w:r | w:hyperlink")` but `paragraph.runs` is direct `w:r` only, so `replace_marker_by_regex` (src/services/docx_io.py:56-176) computes offsets over hyperlink text it cannot index: a marker after a hyperlink is mislocated. Two destructive fallbacks exist: line 81 assigns `paragraph.text = …` (wipes hyperlinks/fields in the paragraph) and lines 104-110 assign `run.text`. Lines 132-141 write the replacement into **every** `w:t` of the cloned run.
- `existing_citation_parser.py:153-163` and `renumber_apply.py:116` extract superscript numbers with `\d+`, so a superscript `3-5` yields `[3, 5]` and loses 4.
- `renumbering.py:136-140` loops `range(references_heading_para_idx)`, so a marker at or after the heading never gets a number and `main_window.py:609-621` writes an empty citation. Assignments are created only from in-text events (`:154-183`), so parsed-but-unmatched entries vanish when the bibliography is rebuilt.
- `remove_references_section` (docx_io.py:262-275) deletes from the heading to the **end of the document** (appendices lost).
- `_on_file_selected` (main_window.py:184-188) swallows analysis exceptions and silently degrades to fresh mode; fresh export only appends, so a misdetected document gets a second bibliography. `_export_document` (main_window.py:258-265) lets the user overwrite the input file. `input_docx_hash` is written but never compared on project open.
- The multi-marker split predicate is duplicated at `orchestrator.py:289`, `main_window.py:394`, `renumber_plan.py:56`.
- `ref_library.py:172-201` `upsert_candidate` overwrites every column on update (abstract, MeSH, authors), so writing a document-derived record back would degrade a rich library row.
- `tests/test_round_trip.py` hand-rolls the writer instead of calling the export code; the export path has no test coverage. 77 tests collect today.
- 13 of the 19 CSL files declare `collapse="citation-number"` (NIH, NLM, Nature, Science, PNAS, Vancouver…); IEEE does not.
- The four fixtures in `fixtures/` already carry Word's own `customXml/item1.xml` (`b:Sources`), so any future custom part must use `next_partname`, never `item1`.

## Recommended design (Phases 0–1)

### Principles

1. **The in-text fields, walked in document order, are the single source of truth** for identity, record data, bibliography membership and order. The bibliography field and the project file are regenerable caches.
2. **Every field is self-sufficient**: full CSL-JSON item with the complete author list, PMID/DOI/PMCID, and the candidate fields the app consumes. Never abstracts.
3. **Private token, CSL-shaped payload**: ` ADDIN AIREFS.CITE {json} `. The token must not contain `CSL_` (Zotero claims any code containing ` CSL_`). No interop is attempted; the CSL shape is used because it is a good, documented schema.
4. **One numbering engine** fed by an ordered event stream; tracked and legacy documents differ only in the event source.
5. **One paragraph index space**: `doc.paragraphs` order everywhere (`SentenceRecord.paragraph_index`, `find_markers`, `renumber_plan`, heading index, section removal). No table or text-box iteration is introduced in this plan.
6. **No network in the read path.** Reopening a tracked document is exact and offline.

### Document format (schema v1)

**Citation field**, one per `(REF)`/`(REFS)` marker; a cluster is one field with N items:

```xml
<w:r><w:fldChar w:fldCharType="begin" w:fldLock="1"/></w:r>
<w:r><w:instrText xml:space="preserve"> ADDIN AIREFS.CITE {"schema":"https://resource.citationstyles.org/schema/latest/input/json/csl-citation.json","citationID":"k7qm2xzp9alc","properties":{"formattedCitation":"3,5","plainCitation":"3,5","noteIndex":0},"citationItems":[{"id":"7f9c2e1a-…","uris":["airefs:record/7f9c2e1a-…","https://doi.org/10.1016/j.cell.2020.01.001","https://pubmed.ncbi.nlm.nih.gov/31978345/"],"itemData":{"id":"7f9c2e1a-…","type":"article-journal","title":"…","author":[{"family":"Smith","given":"J"}],"container-title":"Cell","container-title-short":"Cell","volume":"180","issue":"2","page":"1-10","issued":{"date-parts":[[2020]]},"DOI":"10.1016/…","PMID":"31978345","PMCID":"PMC7000000","custom":{"airefs":{"source":"pubmed","authorCount":2,"authorsTruncated":false,"isReview":false,"retracted":false,"retractionNotice":"","hasErratum":false,"publicationTypes":["Journal Article"],"meshTerms":["Synapses"]}}}}],"airefs":{"v":1,"style":"nih_grant","render":"numeric-superscript","numbers":[3,5],"unresolved":false}} </w:instrText></w:r>
<w:r><w:fldChar w:fldCharType="separate"/></w:r>
<w:r><w:rPr><w:noProof/><w:vertAlign w:val="superscript"/></w:rPr><w:t>3,5</w:t></w:r>
<w:r><w:fldChar w:fldCharType="end"/></w:r>
```

Payload rules (`src/pipeline/citation_payload.py`, `FIELD_SCHEMA_VERSION = 1`):

| Key | Rule |
|---|---|
| `citationID` | 12 lowercase base32 chars; re-minted when a duplicate is found on reopen (copy/paste) |
| `citationItems[].id` | record uuid4 (minted at first export, stored on the candidate and in the project) |
| `uris` | ordered identity vector: `airefs:record/<uuid>`, `https://doi.org/<bare lowercase doi>`, `https://pubmed.ncbi.nlm.nih.gov/<pmid>/`, PMC URL when known, `airefs:hash/<sha1(norm title|year)>` only when nothing better |
| `itemData` | CSL-JSON with the **full** author list; `custom.airefs` carries every `CitationCandidate` field CSL lacks (`citation.py:55-67`) so `from_csl_item()` is lossless except `abstract` (never embedded) and scores. If the serialised item exceeds `MAX_ITEM_BYTES = 16384`, authors truncate to 30 with `authorsTruncated: true` and `authorCount` |
| `properties.plainCitation` | rendered result at write time; used for hand-edit detection |
| `airefs.numbers` | expanded final numbers per item (numeric styles); `render` in `numeric-superscript` / `numeric-bracket` / `numeric-paren` / `author-date`; `unresolved: true` with empty items replaces today's bare `[?]` so the site keeps a `cid` |
| Parsing | slice from first `{` to last `}`; unknown keys ignored; `v` greater than supported → document opens read-only with a banner |

**Bibliography field.** The heading paragraph stays **outside** the field (so a user-applied Heading style survives) and gets paragraph style `AIREFS Bibliography Heading` at creation only; entry paragraphs get style `AIREFS Bibliography`. The field `begin`/`instrText`/`separate` live in the first entry paragraph, `end` in the last:

```xml
<w:p><w:pPr><w:pStyle w:val="AIREFSBibliographyHeading"/></w:pPr><w:r><w:t>References</w:t></w:r></w:p>
<w:p><w:pPr><w:pStyle w:val="AIREFSBibliography"/></w:pPr>
  <w:r><w:fldChar w:fldCharType="begin" w:fldLock="1"/></w:r>
  <w:r><w:instrText xml:space="preserve"> ADDIN AIREFS.BIBL {"v":1,"docId":"4c3a…","style":"nih_grant","render":"numeric-superscript","headingText":"References","order":["7f9c2e1a-…"],"uncited":[],"entryHashes":["e3b0…"],"app":"AI REFs","exportedAt":"2026-09-05T15:02:11Z"} </w:instrText></w:r>
  <w:r><w:fldChar w:fldCharType="separate"/></w:r>
  <w:r><w:t>1. Smith J, Doe A. … doi:10.1016/… PMID: 31978345</w:t></w:r>
</w:p>
<w:p><w:pPr><w:pStyle w:val="AIREFSBibliography"/></w:pPr><w:r><w:t>2. …</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>
```

`AIREFS.BIBL` is a **cache**: `order` is record uuids in rendered order (numeric: citation order; author-date: the alphabetical order actually written), `entryHashes` is parallel to `order` (sha1 of each rendered entry, for hand-edit detection), `uncited` is uuids the user chose to keep. If the field is missing, deleted or unparsable while citation fields survive, membership and order are rebuilt from the field walk and the bibliography is regenerated (reported). **Heading rule:** the heading is the paragraph immediately preceding the paragraph holding the `AIREFS.BIBL` begin run, provided it contains no field, is under 80 characters and is not entry-shaped; fallback: nearest preceding paragraph styled `AIREFS Bibliography Heading`, then nearest preceding paragraph whose text equals `headingText`; else "no heading" and a fresh one is inserted (reported). The heading's text and style are never rewritten.

### Identity and matching (offline)

Resolution order on reopen: `airefs:record/<uuid>` in the project's record registry → PMID → DOI (via `ReferenceLibrary._normalize_doi`) → embedded `itemData` as a read-only record (`source="embedded"`, always succeeds for our own fields). Never bibliography numbers. Document copy wins over library; `upsert_candidate(merge=True)` becomes non-destructive (fill empty columns only, never shorten authors or clear abstract/MeSH) before any embedded record can reach the library through `review_tab.py:568/825`. Library UUID columns and tombstones are a follow-up (Phase 3).

### Write path

All export logic moves to headless `src/pipeline/docx_export.py` (`export_fresh`, `export_tracked`, `export_legacy`, each returning `ExportStats`); `MainWindow._do_export` keeps only dialogs.

1. `docx_io.iter_text_runs(paragraph)` = `xpath("w:r | w:hyperlink/w:r")`, which reproduces `CT_P.text` exactly; it is the **only** walker used for offset arithmetic, with a test asserting `"".join(texts) == paragraph.text` on every fixture. `iter_all_runs(paragraph)` = `.//w:r` with an ancestor filter (skip `mc:Fallback`; mark `w:del` ancestors; track field depth) is used only for field awareness.
2. `replace_marker_by_regex` becomes `_locate_span` (over `iter_text_runs`) + `_split_and_emit` (clone `w:rPr` only, one `w:t`), returns the new cite run, raises `FieldBoundaryError` when the span touches an in-field run, and never assigns `paragraph.text` or `run.text`. `_collapse_and_replace_superscript` never runs in a paragraph containing `w:fldChar`.
3. `insert_citation_field(paragraph, marker_text, code, result_text, superscript)` = `_locate_span` + `docx_fields.build_field_runs(code, result_run)`; unresolved markers get an empty-item field rendering `[?]`.
4. Cluster text: numbers ascending, collapsed with `format_bracket_numbers` when the CSL declares `collapse="citation-number"`; `_parse_csl_citation_layout` (main_window.py:311-343) moves to `citation_render.py` and gains `collapse` detection.
5. Bibliography: `write_bibliography_field` (creates the two styles via `doc.styles.add_style` if absent) or `replace_bibliography_field` (replaces only the field paragraphs; content after the bibliography survives). `remove_references_section` becomes bounded (heading through the last entry-shaped or blank paragraph) and is used only for legacy documents.
6. Output safety: if the chosen output path equals the input (`Path.resolve()`), write `{input}.bak` first and warn; `save` writes `output.tmp` beside the target then `os.replace`. `validate_before_save` checks: balanced begin/separate/end, no `w:t` contains `ADDIN AIREFS`, every payload re-parses, field count equals sites written, every resolved marker received a number. Failure aborts before any write.
7. Hard guard, ratio-based (Phase 0): export stops with a message when (a) a References heading or at least one parsed entry exists and fresh mode would append a second bibliography; (b) parsed entries > 0 and distinct matched in-text numbers / parsed entries < `min_match_ratio` (0.5) or matched = 0; (c) any `ADDIN` citation field that is not ours exists in the body (safety only: we do not read or rewrite other managers' fields); (d) analysis raised. `build_events_legacy` seeds an assignment for **every** parsed entry (cited ones in citation order, uncited ones appended in original order and reported) so a partial parse can no longer delete entries.
8. Multi-marker split predicate consolidated into `SentenceRecord.searched_per_marker` (beside `effective_marker_types`); all three call sites use it.

### Read path (`ExistingCitationParser.analyze()` becomes a two-tier ladder)

`src/services/docx_fields.py::iter_complex_fields(doc)` is a stack-based state machine over body paragraphs in document order: concatenates `w:instrText` across runs and paragraphs, honours `w:fldSimple`, `w:delInstrText`, and final-view Track Changes semantics (`w:del` ancestors = removed, `w:ins` = present, sets `pending_tracked_changes`).

| Tier | Signal | Behaviour |
|---|---|---|
| Tracked | `AIREFS.CITE` fields present | Exact map, offline, no PubMed. `bib_entries` from the field walk (order derived from first appearance; `AIREFS.BIBL` used only as a cache for `uncited` and `entryHashes`); `in_text_citations` from field order with `cid` / `record_uuid` / `cluster_index`; `matched_candidate` populated from `from_csl_item()` so `build_author_date_labels` and the review UI work unchanged; heading index from the heading rule; style from `airefs.render` |
| Legacy | no AI REFs fields | Today's regex + enrichment, unchanged, plus adoption on export (see Phase 1 step 9). If the open project has a non-empty `record_order` for this document but the file has zero fields, the banner says "tracking data was removed (Google Docs/Pages round trip?)" and the legacy path runs |

`analyze()` never raises: exceptions become `TrackingReport.problems`, the banner shows an "analysis failed" state, and `_on_file_selected` no longer degrades silently to fresh mode (fresh export needs an explicit "treat as uncited document" confirmation). `orchestrator.py:152-154` stops overwriting a tracked map, and `:163-183` skips enrichment for entries that already carry a `record_uuid`, PMID or DOI. `_open_project` compares `input_docx_hash` and re-analyses on mismatch.

### Reconciliation on reopen (tracked documents)

| Observation | Action | Reported as |
|---|---|---|
| Field with unknown `citationID` | adopt; record from `itemData` | pasted |
| Duplicate `citationID` | keep first, re-mint later ones | duplicate |
| Result text ≠ `plainCitation`, numbered style | overwrite (numbers are derived) | hand-edited, overwritten |
| Result text ≠ `plainCitation`, author-date | preserve, flag | hand-edited, preserved |
| Result split into several runs by Word | collapse to first result run, delete the rest | — |
| `AIREFS.BIBL` missing / deleted / unparsable | rebuild from the walk; regenerate at the heading if found, else at the end | bibliography regenerated |
| Two fields adjacent with no text between | merge into one cluster on regenerate | merged |
| Record with zero fields (only in `order`) | drop from a numbered bibliography unless in `uncited` or `keep_uncited_entries` | uncited |
| Entry hash ≠ current entry text | keep verbatim until the user regenerates | entry edited |
| Unresolved field | list as a pending site linked to its paragraph; fill from review | unresolved |
| Pending tracked changes | final-view analysis; export blocked with an explicit "Export anyway" override | tracked changes |
| Citation fields inside tables/text boxes | export proceeds only if none would change number or text, else blocked with a message | tables |

### Renumbering (single engine)

`compute_renumbering` is refactored into `compute_renumbering_from_events(events, key_index)` plus two event sources: `build_events_legacy(existing, new_markers)` (today's paragraph walk with the `range(max_para)` defect fixed: all paragraphs visited for new markers, existing in-text events still collected only before the heading, plus per-entry seeding) and `build_events_tracked(handler, plan)` (field walk in document order interleaved with new markers by paragraph and char offset). The preview dialog, tracked export and legacy export call the same function. Agreement tests between the two sources are paired with oracle tests (a marker after the heading gets a number; a parsed-but-uncited entry survives). `tracked_renumber.apply` rewrites only result runs and codes whose text or numbers changed, then `replace_bibliography_field`. `apply_renumbering` and `author_date_convert` remain the legacy path, made field-aware: run passes skip in-field runs, regex passes exclude `field_result_spans(paragraph)`, superscript runs go through `expand_bracket_numbers` (unified with `_expand_citation_range`). For tracked documents a style switch (numbered ↔ author-date) is a re-render of every field and the bibliography from `itemData`, and the `main_window.py:566` shape override is removed for them.

### python-docx notes

Build fields with `OxmlElement("w:fldChar")` + `set(qn("w:fldCharType"), …)` and `w:instrText` with `xml:space="preserve"` (lxml escapes once; write the literal JSON). Never assign `run.text` on a field run (clears the `fldChar`) or `paragraph.text` (clears the paragraph); rewrite result runs by replacing `w:t` text. Styles via `doc.styles.add_style` with an existence check. Pin `python-docx>=1.2.0,<2`. Word behaviour that is not in ISO 29500 (`ADDIN` opacity, `fldLock`, Document Inspector leaving fields alone, `noProof`) is a manual acceptance gate on Word for Mac at the end of Phase 1; the contingency if Word misbehaves is a `w:sdt` carrier read as a second tracked form.

## Data model and project-file changes

| Model | Change |
|---|---|
| `CitationCandidate` (src/models/citation.py) | `record_uuid: str = ""`, `pmcid: str = ""`, `raw_entry: str = ""`; `to_csl_item()` / `from_csl_item()` live in `csl_mapping.py` |
| `SentenceRecord` (src/models/sentence.py) | `searched_per_marker` property |
| `InTextCitation` (src/models/existing_refs.py) | `cid`, `record_uuid`, `cluster_index`, `source` (`field` / `text`), `user_edited`, `unresolved` |
| `ExistingBibEntry` | `record_uuid`, `item: dict` (raw CSL), `uris`, `is_uncited`, `entry_hash`; `matched_candidate` kept; delete the dead `bib_key` property |
| `ExistingCitationMap` | `tracking: Optional[TrackingReport]`, `bibliography_span: tuple[int,int]`, `heading_para_idx_found: bool`, `pending_tracked_changes: bool` |
| New `src/models/embedded.py` | `DocumentTier` enum (`tracked`, `legacy`, `stripped`, `failed`, `newer_version`), `EmbeddedCitationItem`, `EmbeddedCitation`, `TrackingReport(tier, doc_id, field_count, record_count, problems, reconcile)`, `ReconcileIssue(kind, cid, message)`; pydantic, `extra="ignore"` |
| `ProjectState` (src/models/project.py) | `schema_version: int = 2`, `doc_id: str`, `doc_tracking: Optional[TrackingReport]`, `record_order: list[str]`, `uncited: list[str]`; **remove** the dead `bibliography_pmids` / `pmid_to_bib_number`; `input_docx_hash` compared on open |
| `ProjectSettings` | `embed_citation_fields: bool = True`, `keep_uncited_entries: bool = False`, `allow_export_with_tracked_changes: bool = False`, `min_match_ratio: float = 0.5` |
| `ExportStats` (src/pipeline/export_stats.py) | `fields_written`, `unresolved_fields`, `legacy_adopted`, `entries_seeded_uncited`, `uncited_dropped`, `hand_edits_overwritten`, `hand_edits_preserved`, `damaged_fields`, `bibliography_regenerated` |
| `.airefsproj` (src/storage/project_io.py) | top-level `schema_version`; `load_project` upgrades v1 before `model_validate`; never stores paths or email inside embedded payloads |

## File-by-file changes

**Modify**

| File | Change |
|---|---|
| `src/services/docx_io.py` | `iter_text_runs` / `iter_all_runs`; `_locate_span` / `_split_and_emit` refactor (fix offsets, remove the `:81` and `:104-110` fallbacks, fix `:132-141` multi-`w:t`, return the cite run, `FieldBoundaryError`); `insert_citation_field`, `write_bibliography_field`, `replace_bibliography_field` + heading locator, `field_result_spans`, `validate_before_save`, temp-then-rename `save`; `find_superscript_citation_runs(skip_fields=True)` using `expand_bracket_numbers`; bounded `remove_references_section` |
| `src/pipeline/document_parser.py` | **no indexing change**; add a regression test pinning `paragraph_index` equality with `find_markers` and the field walker |
| `src/pipeline/existing_citation_parser.py` | `analyze()` tier ladder that never raises; `_scan_in_text_citations` excludes `field_result_spans`; unify `_expand_citation_range` with `expand_bracket_numbers`; populate new fields and `tracking` |
| `src/pipeline/renumbering.py` | `uuid:` alias first in `CitationKeyIndex._aliases`; shared DOI normalisation; `compute_renumbering_from_events` + `build_events_legacy` (all paragraphs, entry seeding); `key_for_existing` prefers `record_uuid` |
| `src/pipeline/renumber_plan.py` | carry tier; tracked plans from `build_events_tracked`; use `searched_per_marker` |
| `src/pipeline/renumber_apply.py` | legacy-only; skip in-field runs; exclude `field_result_spans`; superscript ranges via `expand_bracket_numbers` |
| `src/pipeline/author_date_convert.py` | field-aware skips; tracked documents re-render from `itemData`; `order` written in rendered sort order |
| `src/pipeline/bib_format.py` | `format_bib_entry_from_item(item, number, style)`; `format_bib_entry` becomes a wrapper; verbatim `raw_entry` branch |
| `src/pipeline/orchestrator.py` | no-clobber for a tracked map; enrichment only for entries lacking ids; `:289` uses `searched_per_marker`; log tier |
| `src/pipeline/export_stats.py` | new counters and summary lines |
| `src/pipeline/existing_enrichment.py` | skip entries with `record_uuid` / PMID / DOI |
| `src/models/{citation,sentence,existing_refs,project}.py`, `src/storage/project_io.py` | as in the data model table |
| `src/services/ref_library.py` | `upsert_candidate(merge=…)` non-destructive path (`:172-201`) |
| `src/gui/main_window.py` | `_on_file_selected` (`:140-192`) → `set_document_mode(report)`, no silent degrade, `[?]` leftover check counts unresolved fields; `_export_document` (`:258-265`) input-path guard; `_open_project` (`:700-724`) hash compare + re-analyse; `_do_fresh_export` / `_do_insert_export` (`:354-657`) delegate to `docx_export`; `_parse_csl_citation_layout` moves out; `:394` uses `searched_per_marker` |
| `src/gui/inputs_tab.py` | `set_insert_mode` (`:658-668`) → `set_document_mode(mode, report)` with states fresh / tracked / legacy / stripped / analysis-failed / newer-version |
| `src/gui/review_tab.py` | export gating (`:616`) allows a tracked document with zero new markers (renumber/regenerate after user edits); `:568` / `:825` pass `merge=True` for embedded candidates; reconcile summary lines |
| `tests/test_round_trip.py` | drive `docx_export` instead of `export_like_fresh`; parametrise over all 19 styles; assert a tracked reopen |
| `requirements.txt` | `python-docx>=1.2.0,<2` |
| `docs/plans/2026-09-05-citation-tracking-design.md` | new design doc (research summary + this design), per the brainstorming workflow |

**New**

| File | Purpose |
|---|---|
| `src/services/docx_fields.py` | `ComplexField` dataclass; `iter_complex_fields`; `build_field_runs`; `rewrite_field_code` / `rewrite_result`; `in_field`; prefix classification (ours vs other `ADDIN`) |
| `src/pipeline/citation_payload.py` | build / parse / validate `AIREFS.CITE` and `AIREFS.BIBL`; version check; `citationID` minting; size safeguard |
| `src/pipeline/csl_mapping.py` | `CitationCandidate` ↔ CSL-JSON (incl. `custom.airefs`); `uris` builder; DOI/PMID/PMCID normalisation |
| `src/pipeline/citation_render.py` | CSL layout + `collapse` parsing (from `main_window.py:311-343`); `render_cluster_text`; author-date labels |
| `src/pipeline/field_citation_reader.py` | classify fields, build `EmbeddedCitation` list, derive membership/order from the walk, build the tracked `ExistingCitationMap` and `TrackingReport`, apply the reconciliation table |
| `src/pipeline/tracked_renumber.py` | `build_events_tracked`; diff-based rewrite of result runs and codes; in-place bibliography rebuild; table-field change detection |
| `src/pipeline/docx_export.py` | headless `export_fresh` / `export_tracked` / `export_legacy`; hard guard; legacy adoption |
| `src/models/embedded.py` | models from the data model table |
| `tests/fixture_builders.py` | OOXML edge-case fixture assembly in `tmp_path` (split `instrText`, field spanning paragraphs, `fldSimple`, `w:ins` / `w:del`, citation inside `w:hyperlink`, multi-`w:t` runs, text box in `mc:Choice` + `mc:Fallback`, unmatched `fldChar`) |
| `tests/test_docx_fields.py`, `tests/test_citation_payload.py`, `tests/test_tracked_round_trip.py`, `tests/test_paragraph_index.py` | see Verification |

## Implementation sequence

Work test-first: each step lands with its tests before the next starts. Commit the current untracked work-in-progress on a branch before Phase 0 (six pipeline modules and six test files are untracked today).

**Phase 0 — hygiene (about 4–5 days, ships alone, fixes five live bugs)**

1. Write `docs/plans/2026-09-05-citation-tracking-design.md`; pin python-docx; commit WIP.
2. `docx_io.iter_text_runs` / `iter_all_runs` + the offset-equality test over `fixtures/*.docx` and `fixture_builders` cases.
3. `docx_fields.iter_complex_fields` / `in_field` / `field_result_spans` + `test_docx_fields.py` (split `instrText`, cross-paragraph field, `fldSimple`, `w:ins`/`w:del`, nested depth, unmatched `fldChar` tolerated; text box counted once).
4. `replace_marker_by_regex` refactor (`_locate_span` + `_split_and_emit`, `FieldBoundaryError`, remove both destructive fallbacks, multi-`w:t` fix, guard `_collapse_and_replace_superscript`) + tests: marker after a hyperlink is placed correctly; refuses inside a field; works adjacent to one; never calls `clear_content`.
5. Field-aware scanners: `find_superscript_citation_runs(skip_fields=True)`, `_scan_in_text_citations`, `apply_renumbering`, `author_date_convert`; unify range expansion (`3-5` → `[3,4,5]` in superscript runs) + tests.
6. `compute_renumbering_from_events` + `build_events_legacy` with the `range(max_para)` fix and per-entry seeding + oracle tests; preview dialog and export share the function.
7. Bounded `remove_references_section`; the ratio-based hard guard (all four cases) with a guard-matrix test; `analyze()` never raises; `_on_file_selected` no silent degrade; input-path guard with `.bak`; temp-then-rename save; `input_docx_hash` compare on project open.
8. `searched_per_marker` consolidation; `test_paragraph_index.py`.

**Phase 1 — fields (about 6–8 days, delivers the multi-session capability)**

1. `csl_mapping.py` + `citation_payload.py` + `models/embedded.py` with unit tests (round trip `from_csl_item(to_csl_item(c)) == c` minus abstract/scores; titles with `<`, `&`, unicode; unknown keys; `v=99` read-only; `citationID` collision re-mint; size safeguard).
2. `citation_render.py` (CSL layout + `collapse`; cluster rendering matrix: superscript / bracket / paren / IEEE no-collapse / author-date).
3. `docx_io.insert_citation_field`, `build_field_runs`, unresolved fields, `write_bibliography_field` / `replace_bibliography_field` + heading locator + styles, `validate_before_save`.
4. `docx_export.export_fresh` writing fields; `main_window._do_fresh_export` delegates; `test_round_trip.py` drives the real writer for all 19 styles and asserts a tracked reopen with zero PubMed calls (mocked client).
5. `field_citation_reader.py`: tracked `ExistingCitationMap` + `TrackingReport`; `ExistingCitationParser.analyze()` ladder; orchestrator no-clobber + enrichment skip; GUI banner states.
6. `tracked_renumber.py` + `docx_export.export_tracked`: add-marker round trip, marker after the heading gets a number, delete a sentence → record dropped and reported, delete the whole References section → regenerated from fields with identical order, paragraph reorder → renumbered, paste duplicate → re-minted, hand-edited numbered result overwritten, author-date preserved, edited entry kept verbatim, content after References preserved, numbered → APA → numbered idempotent, three cycles leave uuids stable, tracked changes block with override, table-cell field that would change number blocks export.
7. `ref_library.upsert_candidate(merge=True)` + test (never shortens authors or clears abstract/MeSH/pubtypes); review-tab wiring.
8. Project-file v2 (`schema_version`, `doc_id`, `record_order`, upgrade of v1 files) + "tracking data removed" banner when a known document reopens with zero fields.
9. Legacy adoption on export (`export_legacy` wraps every recognised superscript run / bracket group into a field whose items are the enriched entry or a minimal item with `raw_entry` + `airefs:hash` URI, so the next session is tracked) + test: legacy document adopted then reopened as tracked; partial legacy parse (50 entries, 3 matched) blocked by the guard and, with the guard lowered, all 50 survive via seeding.
10. Manual Word for Mac acceptance gate (below), then release.

## Verification

- **Automated:** `python -m pytest -q` (77 tests today plus the new suites). Every step above names its tests; the Phase 0 acceptance is one test per live bug (hyperlink offset, `3-5` range loss, delete-to-EOF, multi-`w:t` duplication, post-heading marker → empty citation) plus the guard matrix. Phase 1 acceptance is `test_tracked_round_trip.py` through the real `docx_export` for all 19 styles.
- **End-to-end in the app:** run `python -m src.app`, process `fixtures/simple_input.docx` with a real NCBI email and API key, export; close; reopen the exported file in the app and confirm the banner says "tracked" with the right counts and that the pipeline runs with no "Matching entry" enrichment lines; add a `(REF)` in Word, save, reopen, run, export; confirm numbering and bibliography are correct and the References section was replaced in place.
- **Manual Word for Mac gate (end of Phase 1):** open an export in Word; citations render normally; Alt+F9 shows the `AIREFS.CITE` codes; F9 changes nothing; spell-check does not flag numbers (`noProof`); save and reopen in AI REFs → still tracked; run Document Inspector (remove hidden properties) → still tracked; Track Changes deletion of a citation, saved, is reported as pending tracked changes. Also confirm one Google Docs round trip degrades to the legacy path with the "tracking data removed" banner rather than corrupting anything.

## Decisions taken (change if you disagree)

- Uncited records (fields deleted by the user) are dropped from a numbered bibliography and reported; `keep_uncited_entries` keeps them.
- Track Changes pending → export blocked with an explicit override.
- Document copy wins over the library; no "refresh from library" action yet.
- Abstracts are never embedded in the document.
- Range punctuation stays `3-5` as today.
- Other managers' `ADDIN` fields are detected only to block export (never read, never rewritten). No EndNote/Zotero/Mendeley import, per the user.
- Google Docs / Pages / RTF round trips strip fields; the app reports it and falls back to the legacy path. Re-anchoring from a project mirror is a follow-up.
- Footnotes, headers and citations inside tables/text boxes are read for safety but not renumbered; full support is a follow-up.

## Follow-ups (not in this plan)

- Phase 2: custom XML traveling-library part (full author lists, `next_partname`), `w:docVar` beacon, Regenerate and "Finalize for submission" (flatten to plain text on a copy), "Continue with exported document".
- Phase 3: sentence-hint re-anchoring after fields are stripped, SQLite `uuid` / alias / tombstone migration (additive, transactional, with `.bak`), "Refresh embedded records" from PubMed (retractions).
- Phase 5: table/text-box citations across all consumers, 2020a/b author-date disambiguation.
- Dropped entirely per the user: reading or converting EndNote/Zotero/Mendeley fields, Export for Zotero, Google Docs transfer copy.
