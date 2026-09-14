#!/usr/bin/env python3
"""Look up every author-suggested citation in plan.json, exactly as the app does.

    python resolve_suggestions.py --plan plan.json --email you@institution.edu --out resolved.json

Order of sources: the user's AI REFs library, PubMed (PMID / PMC id
converter / DOI search / first-author+year search), Europe PMC, bioRxiv and
medRxiv.  The output gives, per sentence and marker, the record(s) each
suggestion matched, the source, how many papers an author-year citation
matched (ambiguity), and an error text when nothing was found.  All records
found are repeated under "records" so the file can be passed straight to
write_docx.py.
"""

import argparse
import logging

import sys

from common import (  # noqa: E402
    OFFLINE_HINT, die, dump_json, library_path, load_json, ncbi_credentials, network_reachable,
)

from airefs.models.markers import SuggestedCitation
from airefs.services.biorxiv_client import BioRxivClient
from airefs.services.europepmc_client import EuropePMCClient
from airefs.services.pubmed_client import PubMedClient
from airefs.services.ref_library import ReferenceLibrary
from airefs.services.suggestion_resolver import SuggestionResolver
from airefs.storage.cache_db import CacheDB


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", required=True, help="plan.json from scan_markers.py")
    ap.add_argument("--email", help="NCBI email (or AIREFS_NCBI_EMAIL / ~/.ai_refs/skill_config.json)")
    ap.add_argument("--api-key", help="NCBI API key (optional)")
    ap.add_argument("--save-config", action="store_true", help="remember the email/API key for next time")
    ap.add_argument("--library", default="auto", help="'auto' (default), 'none', or a library .db path")
    ap.add_argument("--no-preprints", action="store_true", help="skip bioRxiv/medRxiv lookups")
    ap.add_argument("--out", help="write resolved.json here (default: stdout)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    plan = load_json(args.plan)
    email, api_key = ncbi_credentials(args.email, args.api_key, save=args.save_config)
    cache = CacheDB()
    pubmed = PubMedClient(email=email, api_key=api_key, cache_db=cache)
    lib_path = library_path(args.library)
    library = ReferenceLibrary(lib_path) if lib_path else None
    resolver = SuggestionResolver(
        pubmed=pubmed,
        europepmc=EuropePMCClient(cache_db=cache),
        biorxiv=None if args.no_preprints else BioRxivClient(cache_db=cache, server="biorxiv"),
        medrxiv=None if args.no_preprints else BioRxivClient(cache_db=cache, server="medrxiv"),
        user_library=library,
    )

    out = {"schema": 1, "plan": args.plan, "library": lib_path, "sentences": {}, "records": []}
    seen = set()
    n_suggestions = n_resolved = 0
    for sent in plan["sentences"]:
        for marker in sent["markers"]:
            if marker["kind"] != "SUGGESTED" or not marker["suggestions"]:
                continue
            suggestions = [SuggestedCitation(**{k: v for k, v in s.items() if k != "label"})
                           for s in marker["suggestions"]]
            resolved = resolver.resolve_all(suggestions)
            entries = []
            for r in resolved:
                n_suggestions += 1
                if r.candidates:
                    n_resolved += 1
                entries.append({
                    "suggestion": r.suggestion.label,
                    "raw": r.suggestion.raw,
                    "kind": r.suggestion.kind.value,
                    "source": r.source,
                    "total_matches": r.total_matches,
                    "ambiguous": bool(r.ambiguous),
                    "error": r.error,
                    "candidates": [
                        {"pmid": c.pmid, "doi": c.doi, "pmcid": c.pmcid, "title": c.title,
                         "first_author_year": c.first_author_year, "journal": c.journal_abbrev or c.journal,
                         "is_retracted": c.is_retracted,
                         "abstract": (c.abstract or "")[:1500]}
                        for c in r.candidates
                    ],
                })
                for c in r.candidates:
                    key = c.pmid or c.doi or c.title
                    if key not in seen:
                        seen.add(key)
                        out["records"].append(c.model_dump())
            out["sentences"].setdefault(sent["id"], {})[str(marker["index"])] = {
                "marker": marker["text"], "resolutions": entries,
            }
    if library:
        library.close()
    out["summary"] = {"suggestions": n_suggestions, "resolved": n_resolved}
    # An unresolved suggestion means "no such paper" only if the network worked.
    if n_resolved < n_suggestions:
        ok, detail = network_reachable()
        if not ok:
            out["network_error"] = detail
            out["hint"] = OFFLINE_HINT
    dump_json(out, args.out)
    if args.out:
        print(f"{args.out}: {n_resolved}/{n_suggestions} suggestion(s) resolved, "
              f"{len(out['records'])} record(s)")
        if out.get("network_error"):
            print(f"warning: {OFFLINE_HINT}", file=sys.stderr)


if __name__ == "__main__":
    main()
