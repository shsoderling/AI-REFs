# File formats

All files are JSON. Keep them in a scratch folder next to the document.

## plan.json (from scan_markers.py)

```json
{
  "schema": 1,
  "docx": "/Users/me/paper.docx",
  "marker_config": {"detect_ids": true, "detect_author_year": true},
  "document": {
    "mode": "fresh",                    // fresh | tracked | legacy | foreign | failed | newer-version
    "insert_mode": false,               // true when existing citations must be renumbered
    "existing_references": 0,
    "references_heading_paragraph": -1,
    "existing_style": "superscript",    // superscript | bracket | author-date (legacy documents)
    "pending_tracked_changes": false,
    "problems": [], "reconcile": [],
    "field_count": 0, "foreign_field_count": 0, "paragraphs": 42,
    "unresolved_placeholders": 0,           // [?] left by an earlier export
    "markers_in_tables": []                 // markers the pipeline cannot reach:
                                            // [{location, text, paragraph_text}]
  },
  "counts": {"marked_sentences": 3, "markers": 4, "ref": 2, "refs": 1, "suggested": 1, "suggestions": 2},
  "sentences": [
    {
      "id": "S001",
      "paragraph_index": 3,
      "section": "Results",
      "raw_text": "Rac1 acts on PAK (REF) as shown before (PMID: 32879322; Battison et al. 2024).",
      "clean_text": "Rac1 acts on PAK as shown before.",
      "slot_count": 2,
      "searched_per_marker": true,
      "context": {"preceding": ["..."], "following": ["..."], "paragraph": "... [[claim]] ..."},
      "markers": [
        {"index": 0, "slot": 0, "text": "(REF)", "kind": "REF", "extra_search": 0,
         "sub_claim": "Rac1 acts on PAK", "trailing": "as shown before", "suggestions": []},
        {"index": 1, "slot": 1, "text": "(PMID: 32879322; Battison et al. 2024)", "kind": "SUGGESTED",
         "extra_search": 0, "sub_claim": "as shown before", "trailing": "",
         "suggestions": [
           {"kind": "pmid", "raw": "PMID: 32879322", "value": "32879322", "label": "PMID 32879322",
            "author": "", "coauthor": "", "et_al": false, "year": 0, "year_suffix": ""},
           {"kind": "author_year", "raw": "Battison et al. 2024", "value": "Battison 2024",
            "label": "Battison et al. 2024", "author": "Battison", "coauthor": "", "et_al": true,
            "year": 2024, "year_suffix": ""}
         ]}
      ]
    }
  ]
}
```

Sentence ids are stable for an unchanged document; `write_docx.py` re-scans the
document and refuses to write if the markers no longer match the plan.

## resolved.json (from resolve_suggestions.py)

```json
{
  "sentences": {
    "S001": {
      "1": {"marker": "(PMID: 32879322; Battison et al. 2024)",
            "resolutions": [
              {"suggestion": "PMID 32879322", "kind": "pmid", "source": "pubmed", "total_matches": 1,
               "ambiguous": false, "error": "",
               "candidates": [{"pmid": "32879322", "doi": "...", "pmcid": "", "title": "...",
                               "first_author_year": "Smith et al., 2020", "journal": "J Neurosci",
                               "is_retracted": false, "abstract": "..."}]},
              {"suggestion": "Battison et al. 2024", "kind": "author_year", "source": "pubmed",
               "total_matches": 3, "ambiguous": true, "error": "", "candidates": [ ... up to 10 ... ]}
            ]}
    }
  },
  "records": [ ...full records for every candidate, usable by write_docx.py... ],
  "summary": {"suggestions": 2, "resolved": 2}
}
```

`resolve_suggestions.py` and `fetch_records.py` add `"network_error"` and `"hint"` to
their output when this shell cannot reach PubMed. That is not "the paper does not
exist": redo those lookups with the connectors.

## records.json (from fetch_records.py, search_library.py --records, or written by hand)

```json
{"records": [
  {"pmid": "32879322", "pmcid": "PMC7000000", "doi": "10.1016/j.cell.2020.01.001",
   "title": "...", "authors": [{"last_name": "Smith", "initials": "JA"}, {"last_name": "Lee", "initials": "B"}],
   "year": 2020, "journal": "Cell", "journal_abbrev": "Cell", "volume": "180", "issue": "2", "pages": "1-10",
   "abstract": "...", "publication_types": ["Journal Article"], "is_retracted": false, "source": "pubmed"}
]}
```

A record is addressed in decisions.json by its PMID, its DOI (any case) or its PMC id.
A hand-written record needs at least `title`, `authors`, `year` and `journal` to format
a bibliography entry; `authors` may also be given as `"Smith, JA"` strings, and
`journal_abbrev`, `volume`, `issue` and `pages` are used when present. Several
`--records` files may be passed to `write_docx.py`; later files override earlier ones
for the same identifier.

When the shell has no network, this file is what you write by hand from connector
results. `get_article_metadata` gives every field above; copy them rather than
reconstructing a citation from memory.

## decisions.json (written by you after the review)

```json
{
  "style": "nih_grant",
  "sentences": {
    "S001": {
      "decision": "accepted",
      "slots": [["30012345"], ["32879322"]],
      "rationale": "Both papers report PAK activation downstream of Rac1 in spines.",
      "citations": {
        "30012345": {"score": 88, "rationale": "Directly shows Rac1-dependent PAK activation",
                     "verdict": "supports", "quote": "Rac1 activation increased PAK1 phosphorylation ...",
                     "warnings": []},
        "32879322": {"score": 45, "rationale": "Author-suggested (PMID 32879322): related model, indirect",
                     "verdict": "partial", "quote": "...", "warnings": ["weak match"]}
      }
    },
    "S002": {"decision": "skipped"},
    "S003": {"decision": "pending"},
    "S004": {"decision": "modified", "slots": [["10.1101/2024.01.03.574066", "31978345"]]}
  }
}
```

- `decision`: `accepted` (the proposal as reviewed), `modified` (the user replaced or
  removed papers), `skipped` (leave the marker text unchanged; `leave` and `unchanged`
  are accepted synonyms), `pending` (not decided: a `(REF)`/`(REFS)` marker exports as
  `[?]`, a suggested marker keeps its text). `rejected` behaves like `pending`.
- `slots`: one list of record keys per marker slot, in marker order; a single flat list
  is taken as one slot. A slot may hold several keys (a `(REFS)` marker, or a `(REF)` the
  user extended). An empty list leaves that marker unresolved.
- `citations`: optional per-record details for the report (score, rationale, verdict,
  verbatim quote, warnings). They are written into the markdown report only; the
  document's hidden record carries the bibliographic data, not your scoring. Key each
  entry by the same identifier used in `slots`.

## Citation style ids

`nih_grant` (default: superscript numbers, NLM-style entries), `nsf_grant`, `vancouver`,
`ama`, `nlm`, `apa`, `cse_author_date`, `cse_citation_seq`, `nature`, `science`,
`plos_one`, `cell`, `elife`, `pnas`, `acs`, `ieee`, `aps`, `elsevier_harvard`,
`chicago_author_date`. Author-date styles (APA, CSE author-date, Elsevier Harvard,
Chicago, eLife) write `(Smith et al., 2020)` and an alphabetical list; the others write
numbers in order of first appearance. The style file decides the in-text form; every
bibliography entry is formatted NLM-style (authors, title, journal, year, volume, pages)
with the DOI and PMID appended when known, which is also what lets a re-opened document
match its entries back to real records.

## table_citations.json (for fill_table_markers.py)

```json
{"markers": [{"location": "table 1 row 2, column 1", "text": "(REF)", "keys": ["27680697"]}]}
```

`location` and `text` come straight from `document.markers_in_tables`; `keys` name papers
already cited in the body of the written document. The script prints what it filled, what
it could not place, and the reminder that these numbers are plain text.

## check_env.py and search_library.py output

`check_env.py` prints `lookups` (`scripts` when this shell can reach PubMed,
`connectors` when it cannot, `unknown` with `--no-network`), `library` and
`library_records` (empty when the user has no library, or has one holding nothing),
`python_docx`, and the saved `ncbi_email`. `search_library.py` prints `hits`, each with
PMID, DOI, PMCID, title, first-author-year, journal and a trimmed abstract; add
`--records lib_hits.json` to get the same papers as a records file for `write_docx.py`.

## write_docx.py output

The script prints JSON with the output path, the mode used, the summary lines (markers
resolved, `[?]` placeholders, references added, entries left unchanged, fields written),
the sentences still unresolved and those without a decision, and the report path when
`--report` was given. The report is markdown: one section per sentence with the chosen
papers, their numbers, scores, verdicts and quotes.
