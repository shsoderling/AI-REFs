# Tracked documents and pre-cited documents

## What the writer produces

Every citation is a Word complex field, the mechanism EndNote, Zotero and Mendeley use:

```
{ ADDIN AIREFS.CITE {"citationID": "...", "citationItems": [{"id": "<record uuid>",
  "uris": ["airefs:record/<uuid>", "https://doi.org/...", "https://pubmed.ncbi.nlm.nih.gov/<pmid>/"],
  "itemData": {CSL-JSON: title, author, container-title, volume, page, issued, DOI, PMID, PMCID, ...}}],
  "airefs": {"v": 1, "style": "nih_grant", "render": "numeric-superscript", "numbers": [3]}} }
```

Word shows the cached result (the number, or `(Smith et al., 2020)`); Alt+F9 toggles the
codes. The bibliography is a second field (`AIREFS.BIBL`) holding the record order and a
hash of each entry. Because the fields carry the full record, a later run reads the
document offline, knows exactly which paper each number is, adds new markers and
renumbers, and rebuilds the bibliography in place. Abstracts are never embedded.

`--no-fields` writes plain text instead; use it only when the user asks for a document
without hidden fields (submission systems that strip them do not need it: fields are
plain Word content and survive PDF export).

## Document modes on scan

- `fresh`: no References heading, no readable citations. Markers are numbered from 1 and
  a References list is appended.
- `tracked`: AI REFs fields present. Only new markers need searching; existing ones keep
  their records. On write, numbers are regenerated in document order, hand-edited numeric
  results are overwritten, hand-edited author-date results are preserved, deleted
  citations drop their entry (unless `--keep-uncited`), pasted duplicates get a fresh id,
  a deleted References list is regenerated. Citations inside tables or text boxes are
  read but not renumbered; the write is refused if one of them would have to change.
- `legacy`: a References heading with numbered entries and in-text numbers (superscript or
  bracketed) written by hand or by the old app. Existing entries are matched to in-text
  numbers, new citations get the next numbers in document order, and on write every
  existing citation site is wrapped into a field (adopted) so the next pass is tracked.
  The write is refused when fewer than half of the entries could be matched to in-text
  citations (a misparsed list would otherwise be destroyed); tell the user which entries
  were not matched (`plan.json` → `document.problems`).
- `stripped`: a former export that lost its fields (Google Docs, Pages, RTF). Read as
  legacy; tracking resumes on the next write.
- `foreign`: EndNote, Zotero or Mendeley fields are present. The writer refuses; suggest
  the user finalise or convert that document first.
- `failed` / `newer-version`: unreadable citations, or a document from a newer app
  version. Refuse and report `document.problems`.

## Author-date conversions

A legacy document with numeric citations exported in an author-date style (APA, ...)
can either keep numbers for everything (default) or convert every existing citation to
author-date text (`--convert-author-date yes`). Conversion needs author and year for
every existing entry; entries without them make the writer refuse, and a converted
document is not tracked (plain text). Ask the user before converting.

## Second pass

1. `scan_markers.py` on the exported document (mode `tracked`).
2. Search and verify only the sentences listed (existing citations are not in the plan).
3. `write_docx.py` to a new file. Existing numbers shift as needed; the report lists the
   new entries.

## Leftover [?] placeholders

A marker exported as `[?]` is a citation still waiting for a paper. On the next scan it is
not a marker any more: it is an unresolved field, counted in
`document.unresolved_placeholders`. To fill one, the user replaces the `[?]` in Word with
`(REF)` (or with the identifier they meant, such as `(PMID: 32879322)`) and you scan
again. A `(REF)` typed over a `[?]` is picked up normally and the placeholder field is
replaced by the real citation.

## Caveats to tell the user once

- Do not retype citation numbers by hand; they are regenerated. Author-date text edits
  are kept.
- Track Changes around a citation block the write until accepted or rejected (or
  `--allow-tracked-changes`).
- A citation copied and pasted elsewhere in the document becomes a second citation of
  the same paper (same number), which is usually what was intended.
