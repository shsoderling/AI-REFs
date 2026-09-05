# Word Acceptance Checklist — tracked documents (Phase 1 gate)

Run on Word for Mac (and Word for Windows / Word Online when available) with a
document exported by AI REFs. Record the result of each step; if 3, 4 or 7
fail, stop and reconsider the field carrier (see the design's contingency).

| # | Step | Expected | Result |
|---|------|----------|--------|
| 1 | Open an AI REFs export in Word | Citations render normally; no spelling squiggles under the numbers | |
| 2 | Alt+F9 | Codes ` ADDIN AIREFS.CITE {…} ` and ` ADDIN AIREFS.BIBL {…} ` appear; Alt+F9 again hides them | |
| 3 | Select all, F9 | Nothing changes (fields are locked) | |
| 4 | Save without edits, reopen in AI REFs | Banner "Tracked document", counts unchanged, no PubMed matching in the run log | |
| 5 | Edit body text, add a `(REF)`, save, run AI REFs, export | Numbering and bibliography correct; References replaced in place | |
| 6 | Delete a citation with Track Changes on, save | AI REFs reports pending tracked changes and blocks export; accept the change, save, export drops the record | |
| 7 | Document Inspector: remove document properties and personal information; reopen in AI REFs | Still tracked | |
| 8 | Copy a paragraph containing a citation, paste it twice, save | AI REFs reports the pasted copies; export renumbers correctly | |
| 9 | Upload to Google Docs, download as DOCX, load in AI REFs | Banner "tracking data is gone"; text-based detection still works | |
| 10 | Large document (~200 citations) | Export and reopen each under 10 s | |

Date: ______  Word version: ______  Tester: ______
