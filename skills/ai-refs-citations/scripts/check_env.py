#!/usr/bin/env python3
"""Report what this machine can do before you start, as JSON.

    python check_env.py

Two things decide how the rest of the work goes:

* whether this shell can reach PubMed (many Claude environments cannot, while
  the PubMed / bioRxiv / Paperclip connectors work fine).  When it cannot,
  look papers up with the connectors and write records.json by hand;
  scan_markers.py and write_docx.py never touch the network.
* whether the user has an AI REFs reference library on disk.  If they do,
  search it first: those papers are the ones they curated.
"""

import argparse
import json
import sys
from pathlib import Path

from common import CONFIG_PATH, library_path, network_reachable, read_config  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", default="auto", help="'auto' (default), 'none', or a library .db path")
    ap.add_argument("--no-network", action="store_true", help="skip the PubMed reachability probe")
    args = ap.parse_args()

    cfg = read_config()
    lib = library_path(args.library)
    n_refs = None
    if lib:
        try:
            from airefs.services.ref_library import ReferenceLibrary
            library = ReferenceLibrary(lib)
            n_refs = library.count()
            library.close()
        except Exception as exc:                              # noqa: BLE001
            n_refs = f"unreadable: {exc}"

    probed = not args.no_network
    ok, detail = network_reachable() if probed else (False, "not probed")
    try:
        import docx                                           # noqa: F401
        docx_version = getattr(__import__("docx"), "__version__", "installed")
    except ImportError:
        docx_version = None

    out = {
        "python": sys.version.split()[0],
        "python_docx": docx_version,
        "ncbi_email": cfg.get("ncbi_email", ""),
        "ncbi_api_key": bool(cfg.get("ncbi_api_key")),
        "config_file": str(CONFIG_PATH) if Path(CONFIG_PATH).exists() else "",
        # An existing but empty library file is the same as no library: say so
        # rather than sending you to search nothing.
        "library": (lib or "") if n_refs else "",
        "library_records": n_refs,
        "library_note": (f"{lib} exists but holds no references" if lib and n_refs == 0 else ""),
        "network": {"pubmed_reachable": ok, "detail": detail},
        # Where paper lookups have to happen: the scripts when this shell has
        # internet, otherwise the PubMed / bioRxiv / Paperclip connectors.
        "lookups": ("scripts" if ok else "connectors") if probed else "unknown",
    }
    print(json.dumps(out, indent=2))
    if docx_version is None:
        print("error: python-docx is missing: pip install 'python-docx>=1.2,<2' pydantic requests",
              file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
