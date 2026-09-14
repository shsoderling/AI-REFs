#!/usr/bin/env python3
"""Search the user's AI REFs reference library (offline).

    python search_library.py --query "Rac1 dendritic spine actin" [--limit 10]
    python search_library.py --query "..." --records lib_hits.json

The library holds the papers this user already collected (imported from RIS,
EndNote XML or earlier runs).  Searching it before PubMed matters because a
paper the user curated is usually the one they want cited, and because it
works when the shell has no internet.  Matches are ranked on title, abstract,
journal and author words; nothing is fetched.
"""

import argparse
import json
import sys

from common import die, dump_json, library_path  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--query", required=True, help="claim keywords, e.g. \"gephyrin phosphorylation mIPSC\"")
    ap.add_argument("--limit", type=int, default=10, help="how many hits to return (default 10)")
    ap.add_argument("--library", default="auto", help="'auto' (default), 'none', or a library .db path")
    ap.add_argument("--records", help="also write the hits as a records.json usable by write_docx.py")
    args = ap.parse_args()

    path = library_path(args.library)
    if not path:
        die("no AI REFs library on this machine (run check_env.py); search PubMed instead")

    from airefs.services.ref_library import ReferenceLibrary
    library = ReferenceLibrary(path)
    try:
        hits = library.search(args.query, max_results=args.limit)
        total = library.count()
    finally:
        library.close()

    out = {
        "library": path,
        "library_records": total,
        "query": args.query,
        "hits": [
            {"pmid": c.pmid, "doi": c.doi, "pmcid": c.pmcid, "title": c.title,
             "first_author_year": c.first_author_year, "journal": c.journal_abbrev or c.journal,
             "year": c.year, "is_retracted": c.is_retracted, "abstract": (c.abstract or "")[:1200]}
            for c in hits
        ],
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    if args.records:
        dump_json({"records": [c.model_dump() for c in hits]}, args.records)
    if not hits and total == 0:
        print("note: the library is empty; there is nothing curated to search here", file=sys.stderr)


if __name__ == "__main__":
    main()
