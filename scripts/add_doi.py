#!/usr/bin/env python3
"""Add a single record to the catalog via resolve().

Usage: add_doi.py <doi|pmid|url> [--section S] [--take T] [--digest-id ID]
"""
from __future__ import annotations

import argparse
import json
import sys

import catalog_io
import ingest_common as ic

MANUAL_MONTH_NAMES = {v: k for k, v in ic.MONTH_NAMES.items()}


def run(identifier: str, section: str | None, take: str, digest_id: str | None,
        resolve_fn=None, dry_run: bool = False):
    if resolve_fn is None:
        from resolve import resolve as resolve_fn  # lazy import

    meta = resolve_fn(identifier)
    doi = meta.get("doi") or identifier

    if not section:
        section = ic.infer_section(meta.get("title") or "", meta.get("journal"))

    if not digest_id:
        digest_id = ic.month_id(ic.today_iso())

    month_num = int(digest_id.split("-")[1])
    year_num = digest_id.split("-")[0]
    month_name = MANUAL_MONTH_NAMES.get(month_num, "").capitalize()
    title = f"Manual additions {month_name} {year_num}"
    date = f"{digest_id}-01"

    rec = {
        "doi": doi,
        "pmid": meta.get("pmid"),
        "title": meta.get("title"),
        "authors": meta.get("authors"),
        "journal": meta.get("journal"),
        "year": meta.get("year"),
        "pub_date": meta.get("pub_date"),
        "pub_types": meta.get("pub_types"),
        "url": meta.get("url"),
        "section": section,
        "take": take or "",
        "source": "manual",
    }

    records = catalog_io.load_all()
    digests = catalog_io.load_digests()

    result_doi, created = ic.upsert(records, rec, digest_id)

    existing_dois = []
    for d in digests:
        if d.get("id") == digest_id:
            existing_dois = d.get("dois", [])
    digests = ic.register_digest(digests, digest_id, "manual", date, title,
                                  existing_dois + [result_doi], "add_doi.py")

    abstract = meta.get("abstract")
    year_for_abstract = rec.get("year") or (rec.get("pub_date") or "")[:4] or "unknown"

    if not dry_run:
        if created and abstract:
            ic.store_abstract(result_doi, year_for_abstract, abstract)
        ic.strip_transient_fields(records)
        catalog_io.save_all(records)
        catalog_io.save_digests(digests)

    final = records[result_doi]
    print(json.dumps(final, indent=1, ensure_ascii=False))
    return final


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("identifier")
    ap.add_argument("--section")
    ap.add_argument("--take", default="")
    ap.add_argument("--digest-id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    run(args.identifier, args.section, args.take, args.digest_id, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
