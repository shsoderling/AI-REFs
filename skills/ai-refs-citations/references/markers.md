# Marker grammar

The scanner (`scripts/scan_markers.py`, the app's own code) treats a parenthetical as a
marker only when its whole content parses as a list of citation items separated by
commas, semicolons or "and", with an optional lead-in such as "see", "e.g.,",
"reviewed in". Anything else is prose.

## Items

| Item | Examples | Notes |
|---|---|---|
| search markers | `REF`, `REFS` | uppercase only; `(REF, REF)` is simply `(REFS)` |
| PubMed id | `PMID: 32879322`, `PMID 32879322`, `PMIDs: 1, 2` | a bare number after a PMID item is another PMID; a bare number alone is never a PMID |
| PMC id | `PMC11413553`, `PMCID: PMC11413553` | converted to the PubMed record |
| DOI | `doi: 10.1101/2024.01.03.574066`, `https://doi.org/10.1038/...`, bare `10.1016/j.cell.2020.01.001` | a trailing period is stripped; one level of nested parentheses is allowed for DOIs such as `10.1016/S0140-6736(20)30183-5` |
| author-year | `Battison et al. 2024`, `Smith and Jones, 2020`, `Smith 2019a`, `Smith 2019, 2021`, `van der Berg et al., 2018`, `O'Neil 2020` | first author (particles, apostrophes, hyphens, accents, initials allowed) + year with optional letter; a bare year continues the previous author |

Mixed lists are fine: `(PMID: 32879322; Battison et al. 2024)`, `(PMC11413553, PMC3159129)`.
`(REF, PMID: 32879322)` means "verify the suggestion and find one more"; `REFS` inside
means up to the cap more.

## Never markers

`(n = 12)`, `(Fig. 2B)`, `(December 2024)`, `(Duke University, 2024)`, `(NIH 2020)`,
`(P < 0.05)`, `(see Methods)`, `(ET AL 2020)`, `(R2023b)`. Organisation names, all-caps
acronyms, a first-word stoplist ("see" alone, "data", "table" ...) and the requirement
that every item parse keep these out. Narrative citations like `Smith et al. (2020)`,
`(Smith et al., 2020a,b)`, `(Smith et al., in press)` and `(Smith 2020, p. 12)` are not
detected either; tell the user if they rely on those.

## Detection switches

`scan_markers.py --no-ids` ignores PMID/PMC/DOI items; `--no-author-year` ignores
author-year items. With both, only `(REF)`/`(REFS)` count, which is what the app did
before author-suggested markers existed. A whole parenthetical has to parse, so the
switches also drop mixed markers: `(REF, PMID: 32879322)` stops being a marker at all
and that sentence disappears from the plan. Say so when a document has any.

A document that already cites in author-year style will show every citation as a
suggestion; that is usually not what the user wants unless they asked to verify their
citations, so ask.

## Where markers are read

Body paragraphs only. Table cells, text boxes, headers and footers are not parsed, which
is also true of the app, so a `(REF)` in a table is never searched and would be exported
as literal text. `scan_markers.py` lists any it finds in tables under
`document.markers_in_tables` so you can raise it with the user before writing, and
`fill_table_markers.py` writes plain numbers into those cells after the main write (they
do not follow later renumbering, so re-run it after any later write).

## Sentences and slots

Each marked sentence has `slot_count` citation slots:
- a lone `(REF)`, `(REFS)` or suggestion marker: one slot;
- an all-`(REFS)` sentence with several markers: one slot shared by every marker (the
  whole list is cited at each, as the app has always done);
- any other multi-marker sentence, and any sentence with a suggestion: one slot per
  marker, in order. `decisions.json` gives one list of record keys per slot.
