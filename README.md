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
# Output: dist/AI REFs.app
```

**Windows:**
```bat
cd packaging
build_windows.bat
REM Output: dist\AI REFs\AI REFs.exe
```

## How to Use

1. **Input Tab**: Load your DOCX file (drag & drop or browse). Configure citation style, NCBI email, and preferences.

2. **REF Library Tab**: Load your AI REFs library and optionally import EndNote exports (`.ris` / `.xml`).
   - The run agent searches your library first (when enabled), then external literature.
   - Accepted literature citations are automatically added to your library for future runs.

3. **Run Tab**: Click "Start Pipeline" to process the document. The pipeline will:
   - Parse the document and find all `(REF)` / `(REFS)` markers
   - Extract keywords from each claim sentence
   - Search your user library + PubMed/Europe PMC/bioRxiv for candidate references
   - Rank and verify candidates

4. **Review Tab**: Review each citation assignment:
   - Green = high confidence, Yellow = medium, Red = low
   - Click a sentence to see candidate details, abstracts, and rationale
   - Accept, modify (enter your own PMID), or find alternatives
   - Export when all sentences are resolved

4. **Export**: Generates:
   - DOCX with citations and bibliography
   - XLSX citation justification report
   - Optional RIS and BibTeX files

## Markers

- `(REF)` → replaced with 1 best reference
- `(REFS)` → replaced with 2-5 references (configurable)

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
