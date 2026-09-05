#!/usr/bin/env python3
"""Merge review/recheck_overlay.json into data/catalog.json IN PLACE.

For each doi in the overlay, sets record["confidence"] and record["verified"]
on the matching catalog record; leaves everything else untouched.

NOT RUN by the worker that wrote it — data/catalog.json did not exist yet at
write time. Run this after catalog.json is built, once, by whoever owns it.
"""
import json
import os
import sys

ROOT = "/Users/neel/critical-care-reading-library"
CATALOG_PATH = os.path.join(ROOT, "data", "catalog.json")
OVERLAY_PATH = os.path.join(ROOT, "review", "recheck_overlay.json")


def main():
    if not os.path.exists(CATALOG_PATH):
        print(f"ERROR: {CATALOG_PATH} does not exist", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(OVERLAY_PATH):
        print(f"ERROR: {OVERLAY_PATH} does not exist", file=sys.stderr)
        sys.exit(1)

    catalog = json.load(open(CATALOG_PATH))
    overlay = json.load(open(OVERLAY_PATH))

    # catalog.json shape is assumed to be either a list of records each with
    # a "doi" field, or a dict keyed by doi. Handle both.
    changed = 0

    if isinstance(catalog, list):
        for rec in catalog:
            doi = (rec.get("doi") or "").strip().lower()
            if doi in overlay:
                ov = overlay[doi]
                rec["confidence"] = ov["confidence"]
                rec["verified"] = ov["verified"]
                changed += 1
    elif isinstance(catalog, dict):
        for doi, rec in catalog.items():
            key = doi.strip().lower()
            if key in overlay:
                ov = overlay[key]
                if isinstance(rec, dict):
                    rec["confidence"] = ov["confidence"]
                    rec["verified"] = ov["verified"]
                    changed += 1
    else:
        print("ERROR: unrecognized catalog.json shape (expected list or dict)", file=sys.stderr)
        sys.exit(1)

    json.dump(catalog, open(CATALOG_PATH, "w"), indent=2)
    print(f"applied overlay: {changed} records changed")


if __name__ == "__main__":
    main()
