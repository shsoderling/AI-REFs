# AI REFs — Desktop Reference Assistant for Scientific Grants

AI REFs reads DOCX documents containing `(REF)` and `(REFS)` markers, finds supporting references via PubMed, and lets you review and accept citations through a polished GUI. It then replaces markers with formatted citations and appends a bibliography — all while preserving your document's original formatting.

## Quick Start

### Run from Source

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. (Optional) Install spaCy model for better keyword extraction
python -m spacy download en_core_web_sm

# 3. Run the application
python -m src.app
```

### Build as Desktop App

**macOS:**
```bash
cd packaging
chmod +x build_mac.sh
./build_mac.sh
# Output: packaging/dist/AI REFs.app and packaging/dist/AI REFs <version>.dmg
```

To build and install into /Applications in one step (the previous copy is moved
to the Trash, then the new app is launched):
```bash
./packaging/install_mac.sh
```

**Windows:**
```bat
cd packaging
build_windows.bat
REM Output: dist\AI REFs\AI REFs.exe
```

## How to Use

1. **Input Tab**: Load your DOCX file (drag & drop or browse). Configure citation style, NCBI email, and preferences. The Claude model list is fetched from Anthropic for your API key (newest first) and cached for a day; click ↻ to refresh it. New projects default to the newest model; your own selection is always kept.

2. **REF Library Tab**: Load your AI REFs library and optionally import EndNote exports (`.ris` / `.xml`).
   - The run agent searches your library first (when enabled), then external literature.
   - Accepted literature citations are automatically added to your library for future runs.

3. **Run Tab**: Click "Start Pipeline" to process the document. The pipeline will:
   - Parse the document and find all `(REF)` / `(REFS)` markers
   - Extract keywords from each claim sentence
   - Search your user library + PubMed/Europe PMC/bioRxiv for candidate
     references, reading abstracts and, for open-access papers, the
     relevant full-text passages from Europe PMC
   - Independently verify each selected paper against its claim: a
     separate check quotes the supporting passage, flags retracted papers
     and swaps preprints for their published journal version

4. **Review Tab**: Review each citation assignment:
   - Green = verified (quoted support found), Yellow = partial support,
     Red = not supported, retracted, or the quote was not in the paper
   - Click a sentence to see candidate details, abstracts, the verifier's
     verdict and quote for each reference, and the rationale
   - Every reference, whether the sentence has one (REF) or several (REFS),
     gets its own Keep / View It / Replace buttons (Remove appears when a
     sentence has several); Replace opens the search chat for that slot,
     and choosing several papers there makes the marker cite all of them,
     even when it started as a single (REF)
   - "Leave unchanged" keeps a sentence's marker text exactly as written on
     export (useful when a parenthetical is not really a citation)
   - Export when all sentences are resolved

4. **Export**: Generates:
   - DOCX with citations and bibliography
   - XLSX citation justification report
   - Optional RIS and BibTeX files

## Tracked Documents

Since this version, every citation AI REFs writes is a hidden Word field (the
same mechanism EndNote and Zotero use). The field carries the reference's
identity (PMID, DOI, a stable record id) and its full record, and the
bibliography is wrapped in a second field. When you load an exported document
again — in a new session, on another computer, after editing it in Word — AI
REFs reads those fields instead of parsing the text, so it knows exactly
which reference each citation is, adds your new `(REF)`/`(REFS)` markers,
renumbers everything and rebuilds the bibliography in place. No PubMed
look-ups are needed to reopen a tracked document.

Things to know:

- Word shows the fields' cached text (the numbers). Alt+F9 toggles the raw
  field codes; they look like ` ADDIN AIREFS.CITE {…} `. Alt+F9 again hides them.
- Do not retype citation numbers by hand: numbers are regenerated on export.
  Hand-edited author-date citations are preserved.
- Deleting a sentence removes its citation; the reference is dropped from the
  bibliography on the next export (or kept, with the "keep uncited entries"
  setting).
- If the bibliography is deleted in Word, AI REFs regenerates it.
- Google Docs, Apple Pages and RTF/ODT saves strip Word fields. AI REFs then
  recognises its former export from the visible text and falls back to
  text-based detection, but exact tracking is lost until the next export.
- Documents made with the previous AI REFs (plain text) are adopted into
  tracked documents the first time they are exported again.
- Documents that contain EndNote, Zotero or Mendeley fields are left alone:
  AI REFs shows a banner and disables export for them.

## Markers

- `(REF)` → replaced with 1 best reference
- `(REFS)` → replaced with 2-5 references (configurable)

### Author-suggested citations

If you already know the paper, write it in parentheses. The app looks it up (your
library first, then PubMed, Europe PMC, and bioRxiv/medRxiv), scores how well it
supports the sentence with the same AI confidence scoring used for `(REF)`, and lets
you confirm, replace, or leave it unchanged in the Review tab:

| Written as | Resolved by |
|---|---|
| `(PMID: 32879322)` | PubMed ID |
| `(PMC11413553)` | PubMed Central ID (converted to a PubMed record) |
| `(doi: 10.1101/2024.01.03.574066)` | DOI, including bioRxiv/medRxiv preprints |
| `(Battison et al. 2024)`, `(Smith and Jones, 2020)`, `(Smith 2019a)` | first author + year; the AI picks the matching paper when several exist |

Several citations can share one pair of parentheses, separated by commas or
semicolons, and the kinds can be mixed: `(PMC11413553, PMC3159129)`,
`(PMID: 32879322; Battison et al. 2024)`. `(REF, PMID: 32879322)` verifies the
suggestion and searches for one more reference.

Only parentheticals made entirely of citations are detected, so `(n = 12)`,
`(Fig. 2B)`, or `(December 2024)` are left alone. Detection can be switched off per
kind in the Input tab, and a dialog before each run says how many suggested
citations were found. Suggested citations go through the same independent
verification as searched ones (verdict and quote per paper). Suggested citations
you do not confirm keep their original text on export (they are never replaced
with `[?]`), and a confirmed one becomes a tracked citation field like any other.

## Requirements

- Python 3.11+
- NCBI email address (required for PubMed E-utilities)
- Optional: NCBI API key (increases rate limit from 3 to 10 requests/sec)

## Citation Styles

- **Numbered** (Vancouver): [1], [2], ...
- **Author-Year** (APA-like): (Smith et al., 2023)
- **Nature**: superscript numbers
- **PMID only**: (PMID: 12345678)

## Project Structure

```
src/
├── app.py                 # Entry point
├── gui/                   # PySide6 GUI (3-tab layout)
├── pipeline/              # 7-stage processing pipeline
├── services/              # PubMed client, DOCX I/O, style engine
├── models/                # Pydantic data models
├── storage/               # SQLite cache, project save/load
├── exporters/             # XLSX, RIS, BibTeX exporters
└── utils/                 # Text processing, constants
```
