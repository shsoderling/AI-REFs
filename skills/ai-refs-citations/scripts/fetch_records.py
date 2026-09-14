#!/usr/bin/env python3
"""Fetch canonical bibliographic records for PMIDs, DOIs and PMC ids.

    python fetch_records.py --email you@institution.edu --pmids 32879322,31978345 \
        --dois 10.1101/2024.01.03.574066 --pmcids PMC11413553 --out records.json

Looks in the user's AI REFs library first (when it exists), then PubMed
(E-utilities), then bioRxiv/medRxiv for preprint DOIs, then Europe PMC.  The
records carry everything the bibliography formatter needs (authors, journal,
volume, pages, DOI, PMCID, retraction flag), which connector search results
often lack, so run this on the final identifiers before write_docx.py.
"""

import argparse
import logging
import re

import sys

from common import (  # noqa: E402
    OFFLINE_HINT, die, dump_json, library_path, ncbi_credentials, network_reachable, split_list,
)

from airefs.services.biorxiv_client import BioRxivClient
from airefs.services.europepmc_client import EuropePMCClient
from airefs.services.pubmed_client import PubMedClient
from airefs.services.ref_library import ReferenceLibrary
from airefs.storage.cache_db import CacheDB


def _norm_doi(doi: str) -> str:
    doi = (doi or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:", "DOI:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
    doi = doi.strip().rstrip(".").lower()
    # A bioRxiv URL carries the version (…574066v2); the API knows the DOI without it.
    if doi.startswith("10.1101/"):
        doi = re.sub(r"v\d+$", "", doi)
    return doi


def fetch(pmids, dois, pmcids, email, api_key, library):
    cache = CacheDB()
    pubmed = PubMedClient(email=email, api_key=api_key, cache_db=cache)
    europepmc = EuropePMCClient(cache_db=cache)
    lib = ReferenceLibrary(library) if library else None
    records, sources, missing = [], {}, []

    def add(cand, key, source):
        records.append(cand.model_dump())
        sources[key] = source

    for pmid in pmids:
        pmid = pmid.strip().replace("PMID:", "").strip()
        cand = lib.find_by_pmid(pmid) if lib else None
        source = "library" if cand else ""
        if cand is None:
            cand = pubmed.fetch_article(pmid)
            source = "pubmed"
        if cand is None:
            cand = europepmc.fetch_by_pmid(pmid)
            source = "europepmc"
        if cand is None:
            missing.append(f"PMID {pmid}")
        else:
            add(cand, pmid, source)

    for pmcid in pmcids:
        pmcid = pmcid.strip().upper()
        if not pmcid.startswith("PMC"):
            pmcid = f"PMC{pmcid}"
        cand = lib.find_by_pmcid(pmcid) if lib else None
        source = "library" if cand else ""
        if cand is None:
            pmid = pubmed.pmcid_to_pmid(pmcid)
            if pmid:
                cand = pubmed.fetch_article(pmid)
                source = "pubmed"
        if cand is None:
            cand = europepmc.fetch_by_pmcid(pmcid)
            source = "europepmc"
        if cand is None:
            missing.append(pmcid)
        else:
            if not cand.pmcid:
                cand.pmcid = pmcid
            add(cand, pmcid, source)

    for raw in dois:
        doi = _norm_doi(raw)
        cand = lib.find_by_doi(doi) if lib else None
        source = "library" if cand else ""
        if cand is None:
            for pmid in pubmed.find_pmids_by_doi(doi):
                article = pubmed.fetch_article(pmid)
                if article and _norm_doi(article.doi) == doi:
                    cand, source = article, "pubmed"
                    break
        if cand is None and doi.startswith("10.1101/"):
            for server in ("biorxiv", "medrxiv"):
                try:
                    cand = BioRxivClient(cache_db=cache, server=server).fetch_preprint(doi)
                except Exception as exc:                          # network: try the next source
                    logging.warning(f"{server} lookup failed for {doi}: {exc}")
                    cand = None
                if cand:
                    source = server
                    break
        if cand is None:
            cand = europepmc.fetch_by_doi(doi)
            source = "europepmc"
        if cand is None:
            missing.append(f"doi:{doi}")
        else:
            add(cand, doi, source)

    if lib:
        lib.close()
    return {"records": records, "sources": sources, "missing": missing}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pmids", help="comma-separated PubMed ids")
    ap.add_argument("--dois", help="comma-separated DOIs (journal articles or bioRxiv/medRxiv preprints)")
    ap.add_argument("--pmcids", help="comma-separated PMC ids")
    ap.add_argument("--email", help="NCBI email (or AIREFS_NCBI_EMAIL / ~/.ai_refs/skill_config.json)")
    ap.add_argument("--api-key", help="NCBI API key (optional; 10 requests/s instead of 3)")
    ap.add_argument("--save-config", action="store_true", help="remember the email/API key for next time")
    ap.add_argument("--library", default="auto",
                    help="AI REFs library to consult first: 'auto' (default), 'none', or a .db path")
    ap.add_argument("--out", help="write records.json here (default: stdout)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    pmids, dois, pmcids = split_list(args.pmids), split_list(args.dois), split_list(args.pmcids)
    if not (pmids or dois or pmcids):
        die("give at least one of --pmids, --dois, --pmcids")
    email, api_key = ncbi_credentials(args.email, args.api_key, save=args.save_config)
    library = library_path(args.library)
    # One probe before any lookup: each identifier would otherwise retry through
    # PubMed, Europe PMC and bioRxiv before failing, several seconds each.
    online, detail = network_reachable()
    if not online and not library:
        dump_json({"records": [], "sources": {}, "missing": pmids + pmcids + dois,
                   "network_error": detail, "hint": OFFLINE_HINT}, args.out)
        print(f"warning: {OFFLINE_HINT}", file=sys.stderr)
        return
    result = fetch(pmids, dois, pmcids, email, api_key, library)
    # Distinguish "no such paper" from "this shell has no internet".
    if result["missing"] and not online:
        result["network_error"] = detail
        result["hint"] = OFFLINE_HINT
    dump_json(result, args.out)
    if args.out:
        print(f"{args.out}: {len(result['records'])} record(s)"
              + (f"; not found: {', '.join(result['missing'])}" if result["missing"] else ""))
        if result.get("network_error"):
            print(f"warning: {OFFLINE_HINT}", file=sys.stderr)


if __name__ == "__main__":
    main()
