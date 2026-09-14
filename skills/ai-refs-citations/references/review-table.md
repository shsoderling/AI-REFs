# Review table

Present the proposals before writing anything. One row per sentence, in document
order, with the markers visible so the user can see what each citation replaces.
Keep each cell short; the report written later carries the full quotes.

```
| # | Sentence (markers in **bold**) | Proposed citation(s) | Score / confidence | Verification | Warnings |
|---|---|---|---|---|---|
| S001 | Rac1 acts on PAK **(REF)** as shown before **(PMID: 32879322)** | (REF) → Zhang 2018, J Neurosci, PMID 30012345 · (PMID: 32879322) → Smith 2020, Cell | 88 · 45 / MEDIUM | supports: "Rac1 activation increased PAK1 phosphorylation" · partial: "..." | weak match (PMID 32879322) |
| S002 | Spine growth requires actin remodelling **(REFS)** | Hotulainen 2010 (PMID 20696704); Bosch 2014 (PMID 24853940); Nakahata 2018 (PMID 29769751) | HIGH (92) | all supported, quotes in report | |
| S003 | Method described in **(Battison et al. 2024)** | Battison 2024, bioRxiv 10.1101/2024.01.03.574066 (published: Nat Methods 2025, PMID ...) | 95 / HIGH | supports: "..." | preprint swapped for journal version; ambiguous: chosen among 2 papers by Battison 2024 |
| S004 | Values were normalised **(n = 12)** | not a citation | | | left unchanged |
| S005 | Similar effects in cortex **(REF)** | nothing convincing found (3 searches) | LOW | | will export as [?] unless you give a paper |
```

After the table, ask for decisions in plain words, for example:

> Reply with "accept all", or per sentence: "S001 accept", "S002 drop Nakahata",
> "S003 replace with PMID 31978345", "S005 search again for cortical spines", or
> "S004 leave unchanged". Anything you do not mention is accepted as proposed.

Map the answers to `decisions.json`: accept → `accepted`; any replacement or removal →
`modified` with the new slot contents; leave unchanged → `skipped`; a sentence the
user wants to think about → `pending`. When the user asks for another search, do it,
show the updated row, and only then write.

Suggested-citation rows always show the author's own words in the marker and what
they resolved to; if a suggestion could not be resolved, say "unresolved: <reason>"
and that the text will be kept as written.
