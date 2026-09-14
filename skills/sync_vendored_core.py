#!/usr/bin/env python3
"""Copy the app's pure-Python core into the ai-refs-citations skill.

The skill's scripts (scan_markers.py, write_docx.py, ...) import the same
marker grammar, DOCX writer, tracked-field code and citation formatting the
desktop app uses, so a document written by the skill reopens in the app and
vice versa.  Run this after changing anything under ``src/`` that the skill
depends on, then commit the result:

    python skills/sync_vendored_core.py

What is copied: models, utils, storage, the DOCX/field/citation pipeline
modules and the literature clients (requests-based).  What is not: the Qt
GUI, the Claude agent, the verifier and the orchestrator -- in the skill,
Claude itself plays those parts.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SKILL = ROOT / "skills" / "ai-refs-citations"
DEST = SKILL / "scripts" / "airefs"

MODULES = {
    "": ["__init__.py"],
    "models": ["__init__.py", "citation.py", "embedded.py", "evidence.py", "existing_refs.py",
               "markers.py", "project.py", "sentence.py"],
    "utils": ["__init__.py", "constants.py", "markers.py"],
    "storage": ["__init__.py", "cache_db.py", "project_io.py"],
    "services": ["__init__.py", "biorxiv_client.py", "docx_fields.py", "docx_io.py",
                 "europepmc_client.py", "jats.py", "pubmed_client.py", "rate_limiter.py",
                 "ref_library.py", "search_tools.py", "suggestion_resolver.py", "tool_executor.py"],
    "pipeline": ["__init__.py", "author_date_convert.py", "bib_format.py", "citation_numbers.py",
                 "citation_payload.py", "citation_render.py", "claim_context.py", "csl_mapping.py",
                 "document_parser.py", "docx_export.py", "existing_citation_parser.py",
                 "existing_enrichment.py", "export_slots.py", "export_stats.py",
                 "field_citation_reader.py", "marker_locator.py", "renumber_apply.py",
                 "renumber_plan.py", "renumbering.py", "tracked_renumber.py"],
}

CSL_PATH_OLD = 'csl_dir = Path(__file__).parent.parent.parent / "assets" / "csl"'
CSL_PATH_NEW = 'csl_dir = Path(__file__).resolve().parents[3] / "assets" / "csl"   # <skill>/assets/csl'


def main() -> int:
    if DEST.exists():
        shutil.rmtree(DEST)
    for sub, files in MODULES.items():
        target = DEST / sub if sub else DEST
        target.mkdir(parents=True, exist_ok=True)
        for name in files:
            src = SRC / sub / name if sub else SRC / name
            if not src.exists():
                if name == "__init__.py":
                    (target / name).write_text("")
                    continue
                print(f"missing: {src}", file=sys.stderr)
                return 1
            text = src.read_text()
            text = re.sub(r"\bfrom src\.", "from airefs.", text)
            text = re.sub(r"\bimport src\.", "import airefs.", text)
            if sub == "models" and name == "project.py":
                assert CSL_PATH_OLD in text, "get_csl_path changed; update sync_vendored_core.py"
                text = text.replace(CSL_PATH_OLD, CSL_PATH_NEW)
            (target / name).write_text(text)

    assets = SKILL / "assets" / "csl"
    if assets.exists():
        shutil.rmtree(assets)
    shutil.copytree(ROOT / "assets" / "csl", assets)

    try:
        commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        commit = "unknown"
    (DEST / "VENDORED.txt").write_text(
        f"Copied from src/ at commit {commit} by skills/sync_vendored_core.py. Do not edit here; "
        "edit src/ in the AI-REFs repository and re-run the sync script.\n")
    n = sum(len(v) for v in MODULES.values())
    print(f"vendored {n} modules and {len(list(assets.glob('*.csl')))} CSL styles into {DEST.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
