---
name: ai-refs-citations
description: "Find, verify and insert literature citations into a Word (.docx) manuscript or grant the way the AI REFs desktop app does. Use this whenever a user asks to add references, fill in citations, resolve (REF) or (REFS) markers, check citations they wrote by hand such as (PMID 32879322), (PMC11413553), (doi 10.1101/...) or (Smith et al. 2024), build or renumber a bibliography in NIH, Vancouver, Nature, APA or another style, or 'add the refs to my grant', even if they never say 'AI REFs' or 'marker'. Also use it to reopen a document AI REFs exported earlier (it carries hidden citation fields) and add more citations. Not for reformatting an EndNote or Zotero document, and not for writing prose."
---

# AI REFs citations

You are the citation agent the AI REFs app runs in software: read the document, find
what each marker needs, search the literature, verify every paper against its claim,
let the user review, then write the document. The deterministic parts (marker
grammar, DOCX surgery, tracked Word fields, in-text citation formatting in 19 CSL
styles, renumbering of pre-cited documents) are the app's own code, bundled in
`scripts/`. You supply the judgement the app asks its Claude agent and its independent
verifier for: search, choose, quote, score.

Read `references/search-and-verify.md` before the first search of a session. It carries
the app's search, scoring and verification rules, and following them is what makes your
result match what the user gets from the app.

## What the user gives you

A `.docx` in which citation markers sit in the text:

| Marker | Meaning |
|---|---|
| `(REF)` | find the one best reference for this sentence |
| `(REFS)` | find up to 3 references (the user may ask for another cap) |
| `(PMID: 32879322)`, `(PMC11413553)`, `(doi: 10.1101/2024.01.03.574066)`, `(Battison et al. 2024)`, `(Smith and Jones, 2020)`, `(Smith 2019a)` | the author already knows the paper: look it up, score how well it supports the sentence, never substitute a different paper |
| `(PMC11413553, PMC3159129)`, `(PMID: 32879322; Battison et al. 2024)`, `(REF, PMID: 32879322)` | several suggestions in one marker; `REF` inside means "verify these and find one more" |

Ordinary parentheticals such as `(n = 12)`, `(Fig. 2B)`, `(December 2024)` are never
markers. `references/markers.md` has the exact grammar and edge cases.

## Requirements

`python3` with `python-docx>=1.2`, `pydantic>=2` and `requests`. If an import fails:
`pip install 'python-docx>=1.2,<2' pydantic requests`.

Work in a scratch folder next to the document (for example `<doc>_airefs/`) so the JSON
files survive for a second pass. Run the scripts from the skill's `scripts/` directory,
or by full path; they find their bundled code themselves.

## Workflow

### 0. See what this machine can do

```bash
python3 scripts/check_env.py
```

Two answers shape everything that follows.

`lookups` says where paper lookups have to happen. Many Claude environments give the
shell no internet while the PubMed, bioRxiv and Paperclip connectors work perfectly:

- `scripts`: `resolve_suggestions.py` and `fetch_records.py` can reach PubMed, Europe PMC
  and bioRxiv directly. Use them; they return complete records in one call.
- `connectors`: those two scripts will come back empty. Do every lookup with the
  connectors instead and write `records.json` by hand from what they return (the format
  is six or seven fields, in `references/file-formats.md`). `scan_markers.py`,
  `search_library.py` and `write_docx.py` never touch the network, so the rest of the
  workflow is unchanged.

`library` is the user's AI REFs reference library when they have one. Those are papers
they already collected, so search it first for every claim:

```bash
python3 scripts/search_library.py --query "gephyrin phosphorylation mIPSC" --limit 10
```

The scripts that do reach the network want an NCBI email (E-utilities policy). Ask the
user once, pass `--email you@institution.edu --save-config`, and later calls read it from
`~/.ai_refs/skill_config.json`. An NCBI API key is optional and only raises the rate limit.

### 1. Scan the document

```bash
python3 scripts/scan_markers.py --docx "paper.docx" --out plan.json
```

`plan.json` lists every marked sentence with its claim, section, neighbouring sentences,
the sub-claim each marker belongs to, and how the document itself was read
(`references/file-formats.md`). Tell the user in a line or two what was found: how many
`(REF)`/`(REFS)` markers to search, how many author-suggested citations to verify, and
whether the document already carries citations.

- `fresh`: no citations yet; every marker gets a number from 1.
- `tracked`: an earlier AI REFs export. Existing citations are read from hidden fields;
  only the new markers need work and everything is renumbered on write.
- `legacy`: plain-text numbered citations and a References list. Same as tracked, but the
  existing entries are read from the text and adopted into fields on write.
- `foreign`, `failed`, `newer-version`: the document cannot be written (another reference
  manager's fields, unreadable citations, or a newer app version). Say so and stop.

Three fields are worth a sentence to the user when they are not empty.
`document.unresolved_placeholders` counts `[?]` left by an earlier export, which are no
longer markers: to fill one, the user replaces the `[?]` with `(REF)` in Word and you scan
again. `document.markers_in_tables` lists markers inside table cells or text boxes: the
pipeline reads body paragraphs only, exactly as the app does, so those markers are not
searched and would survive into the exported file as literal `(REF)` text. Search those
claims like any other, then after the main write fill them with
`scripts/fill_table_markers.py` (step 6), and tell the user that those numbers are plain
text: a later renumbering pass will not update them, so the script has to be re-run after
any later write. A paper cited only in a table cannot be added to the bibliography this
way, so for one of those, suggest moving the sentence into the body instead.
`document.problems` and `document.reconcile` explain anything else the parser could not
line up.

If the scan finds no markers at all, the document has nothing to work on: say so, name a
sentence or two that look like they need a citation, and offer either to insert `(REF)`
markers into a copy of the document for the user to check, or to take a list of sentences
from them. Do not start citing unmarked prose on your own; which claims need support is
the author's call.

If there are author-suggested markers, ask whether to verify them or to handle only
`(REF)`/`(REFS)` this time; the app asks the same question, because each suggestion costs
a lookup and an evaluation. For the latter, scan again with `--no-ids --no-author-year`.
If the counts look wrong (a whole author-date manuscript detected as suggestions), that is
the moment to switch detection off rather than after the searching.

### 2. Resolve the author's suggestions

With `lookups: scripts`:

```bash
python3 scripts/resolve_suggestions.py --plan plan.json --out resolved.json
```

For each suggestion the file gives the matching record(s), the source, and whether an
author-year citation was ambiguous (several papers by that first author that year) or not
found. With `lookups: connectors`, do the same work through the connectors: PMIDs with
`get_article_metadata`, PMC ids with `convert_article_ids`, DOIs with
`lookup_article_by_citation` or `get_preprint`, author-year with `search_articles`
(`Battison[1au] AND 2024[dp]`, widen to ±1 year, then any author position).

Either way, apply the scoring rules in `references/search-and-verify.md` (section
"Author-suggested citations"): keep every resolved suggestion, score it 0-100 for how
directly it supports the claim, choose among ambiguous matches by topic, never replace a
suggestion with a different paper, and record unresolved ones as unresolved.

### 3. Search for `(REF)` and `(REFS)` markers

For each sentence in `plan.json` with such markers, follow the search workflow in
`references/search-and-verify.md`: read the claim with its context, search the user's
library first when there is one, craft one or two PubMed queries, read abstracts, choose
the best match (`REF`: one paper, `REFS`: up to the cap) and note the supporting snippet.
A sentence with several markers is handled marker by marker using each marker's
`sub_claim`, and a paper already assigned to one marker of that sentence is not reused for
another.

### 4. Verify every selected paper

For each selected paper, searched or suggested, give an independent verdict with a
verbatim quote from the abstract or open-access full text: `supports`, `partial` or
`not_supported`. A supporting verdict without a real quote is only `partial`. Flag
retracted papers; a preprint whose journal version exists is replaced by that version,
with a note. The confidence mapping is in the reference file.

### 5. Present the review table and wait

Show one row per sentence (template in `references/review-table.md`): sentence id, the
sentence with its markers, the proposed paper(s) with PMID or DOI, score or confidence,
the verdict with its quote, and warnings (weak match, ambiguous, unresolved, retracted,
duplicate, alternative). Ask the user to accept, to replace (they give a PMID or DOI, or
ask for another search), or to leave a sentence unchanged. Do not write the document
before they answer: this review is the point of the workflow, and a citation the author
has not seen is a citation they cannot defend. "Accept all" is a valid answer.

When the user has already told you to write the document in the same breath ("find the
refs and give me back the file"), they have pre-approved it: still show the table, then
write, and say which rows are worth a second look (anything scored below 70, ambiguous
or unresolved) and that changing one is a one-line request away.

### 6. Fetch canonical records and write the document

Bibliography entries need full metadata (authors, journal, volume, pages, DOI, PMID),
which search snippets often lack, so fetch it for the final identifiers:

```bash
python3 scripts/fetch_records.py --pmids 32879322,31978345 \
    --dois 10.1101/2024.01.03.574066 --out records.json
```

With `lookups: connectors`, build the same file from `get_article_metadata` and
`get_preprint` results instead. `resolved.json` and `search_library.py --records` are
already in that format, and several `--records` files can be passed at once.

Write `decisions.json` (format in `references/file-formats.md`): per sentence the
decision and, per marker slot, the record keys, plus each citation's score, verdict and
quote for the report. Then:

```bash
python3 scripts/write_docx.py --plan plan.json --records resolved.json --records records.json \
    --decisions decisions.json --style nih_grant --out "paper_with_refs.docx" --report report.md
```

The default style is NIH grant: superscript numbers in the text, NLM-style entries
carrying DOI and PMID. The style decides the in-text form; the reference entries are
NLM-style in every style, so tell a user who asked for "Nature format" that the numbering
matches but the entry layout is NLM. The style ids are listed in
`references/file-formats.md`.
If the scan found markers in tables, fill them once the main write is done:

```bash
python3 scripts/fill_table_markers.py --docx "paper_with_refs.docx" --out "paper_final.docx" \
    --assign table_citations.json
```

`table_citations.json` is `{"markers": [{"location": ..., "text": ..., "keys": [...]}]}`,
with `location` and `text` copied from `document.markers_in_tables` and `keys` naming
papers already cited in the body.

Report the summary lines the script prints (markers resolved, `[?]` placeholders,
references added, markers left unchanged) and hand the user both files. The writer
refuses rather than quietly doing the wrong thing when a slot holds a paper the author
did not name, when a chosen paper is retracted, when an author-date style would strand a
numeric document's existing numbers, or when a legacy reference list is too poorly
matched to rebuild safely; each refusal names the flag that overrides it, and each
override is a decision to put to the user, not to take yourself. Never write onto
the input document; the script refuses.

## Things that matter

- Every citation is written as a hidden Word field carrying the reference's record, the
  same mechanism EndNote and Zotero use, so the document can be reopened later by the app
  or by this skill and renumbered after edits. Word shows only the number; Alt+F9 reveals
  the codes. Google Docs, Pages and RTF saves strip the fields, which costs the tracking
  but not the text. Details in `references/tracked-documents.md`.
- An author-suggested citation you could not confirm keeps its original text on export; it
  is never turned into `[?]`. Only an undecided `(REF)`/`(REFS)` marker becomes `[?]`, and
  the writer lists which sentence it belongs to.
- "Leave unchanged" (decision `skipped`) keeps a marker's text exactly as written, which
  is what the user wants for a parenthetical that was not really a citation.
- Read `slot_count` from the plan rather than counting markers: a sentence usually has
  one slot per marker, in order, but a sentence whose markers are all `(REFS)` has a
  single shared slot, and the whole list is then cited at each marker. Getting the slots
  right is what puts each paper at the marker it belongs to (`references/markers.md`).
- Do not invent identifiers. A PMID or DOI goes into `decisions.json` only after a
  connector, a script or the user's library returned it. A hand-written record whose
  numbers you guessed will be formatted into the bibliography and read as fact.
- The same paper cited twice in the document gets one number and one entry; the writer
  handles that, including across a second pass.
- Second pass on an exported document: scan it again (mode `tracked`), search only the new
  markers, write to a new file. Old citations keep their records and renumber around the
  new ones.
