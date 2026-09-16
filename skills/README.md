# Skills built from this repository

## ai-refs-citations

A Claude skill that reproduces the AI REFs workflow without the desktop app: Claude
reads the document, searches and verifies the literature (PubMed / bioRxiv / Paperclip
connectors, or the bundled clients when the shell has internet), shows the user a review
table, and writes the cited document.

The deterministic half is the app's own code, vendored into
`ai-refs-citations/scripts/airefs/` so the skill has no dependency on this repository:
marker grammar, document parsing, tracked Word fields, renumbering, the CSL styles (both
the in-text form and the reference entries) and the DOCX writer. Only `python-docx`,
`pydantic` and `requests` are needed at runtime.

### Regenerating the vendored code

After changing anything under `src/` that the skill uses, re-run:

```bash
python3 skills/sync_vendored_core.py
```

It copies the module list at the top of the script, rewrites `from src.` imports to
`from airefs.`, points the CSL loader at `assets/csl/`, refreshes the style files and
records the source commit in `scripts/airefs/VENDORED.txt`. Nothing under
`scripts/airefs/` should be edited by hand.

### Layout

```
ai-refs-citations/
  SKILL.md                 workflow Claude follows
  references/              marker grammar, search and verification rules,
                           file formats, tracked documents, review table
  scripts/                 check_env, scan_markers, search_library,
                           resolve_suggestions, fetch_records, write_docx,
                           fill_table_markers
  tests/                   the style-by-style bibliography expectations
  scripts/airefs/          vendored app code (generated)
  assets/csl/              citation style files (generated)
  evals/evals.json         test prompts and assertions
```

### Checking it still works

```bash
python3 skills/ai-refs-citations/scripts/check_env.py
python3 skills/ai-refs-citations/scripts/scan_markers.py \
    --docx fixtures/suggested_markers_test.docx --out /tmp/plan.json
```

A full offline round trip (scan, hand-written records and decisions, write, rescan) is
what the skill's evals exercise; the app's own test suite (`pytest tests`) covers the
vendored code.
