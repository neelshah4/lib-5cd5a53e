#!/usr/bin/env python3
"""Ingest a monthly critical-care digest markdown file (or a bare -DOIs.txt
list) into the catalog.

Usage: ingest_monthly.py <path-to-digest.md> [--dry-run] [--dois-file X]
"""
from __future__ import annotations

import argparse
import re
import sys

import catalog_io
import ingest_common as ic
import sections

HEADER_RE = re.compile(
    r"#.*?—\s*([A-Za-z]+)\s+(\d{4})\s*$", re.M
)

class ParseFailure(Exception):
    def __init__(self, msg, block=""):
        super().__init__(msg)
        self.block = block


def parse_header(text: str, path: str):
    m = HEADER_RE.search(text)
    if not m:
        raise ParseFailure(f"{path}: could not find '— <Month> <Year>' in header", text[:300])
    month_name, year = m.group(1).lower(), int(m.group(2))
    if month_name not in ic.MONTH_NAMES:
        raise ParseFailure(f"{path}: unrecognized month name {month_name!r}", m.group(0))
    return f"{year:04d}-{ic.MONTH_NAMES[month_name]:02d}"


def split_sections(text: str):
    parts = re.split(r"(?m)^## ", text)
    for part in parts[1:]:
        lines = part.split("\n", 1)
        heading = lines[0].strip()
        body = lines[1] if len(lines) > 1 else ""
        yield heading, body


def split_entries(body: str):
    """Yields (title, url, journal, take) for every '[Title](url) — Journal'
    entry in a monthly section body."""
    lines = body.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m = re.match(r"^\[(.+?)\]\((\S+?)\)\s*—\s*(.+)$", line)
        if m:
            title, url, journal = m.group(1), m.group(2), m.group(3).strip()
            take = ""
            j = i + 1
            if j < n:
                nxt = lines[j].strip()
                if nxt and not nxt.startswith("[") and not nxt.startswith("#") and not nxt.startswith("-"):
                    take = nxt
            yield title, url, journal, take
        i += 1


def run(path: str, dry_run: bool, dois_file: str | None, resolve_fn=None):
    records = catalog_io.load_all()
    digests = catalog_io.load_digests()

    papers = []

    if dois_file:
        digest_id = None
        title = None
        for line in open(dois_file, encoding="utf-8"):
            doi = line.strip()
            if not doi:
                continue
            papers.append({"doi": doi, "take": "", "source": "monthly", "_from_dois_file": True})
        if not papers:
            raise ParseFailure(f"{dois_file}: no DOIs found", "")
        # digest id/title/date come from the accompanying .md path's name if possible,
        # else fall back to today's month.
        digest_id = ic.month_id(ic.today_iso())
        date = ic.today_iso()
        title = f"Manual DOI-list additions {digest_id}"
    else:
        text = open(path, encoding="utf-8").read()
        digest_id = parse_header(text, path)
        year, month = digest_id.split("-")
        date = f"{year}-{month}-01"
        title = f"Monthly digest {digest_id}"

        for heading_raw, body in split_sections(text):
            heading = heading_raw.strip()
            try:
                section = sections.canonical(heading)
            except ValueError as e:
                raise ParseFailure(f"{path}: unrecognized section heading {heading_raw!r}: {e}",
                                    heading_raw)
            for entry_title, url, journal, take in split_entries(body):
                du = re.search(r"doi\.org/(.+)$", url)
                doi = du.group(1) if du else None
                papers.append({
                    "doi": doi,
                    "url": None if doi else url,
                    "title": entry_title,
                    "journal": journal,
                    "section": section,
                    "take": take,
                    "source": "monthly",
                    "_raw_url": url,
                })

    if resolve_fn is None and papers:
        from resolve import resolve as resolve_fn  # lazy import

    n_added = 0
    n_merged = 0
    dois = []
    for p in papers:
        identifier = p.get("doi") or p.get("_raw_url") or p.get("url")
        try:
            meta = resolve_fn(identifier)
        except Exception:
            meta = {}
        if not p.get("doi"):
            p["doi"] = meta.get("doi")
        if not p["doi"]:
            raise ParseFailure(f"{path or dois_file}: could not resolve a DOI for {identifier!r}", str(p))
        for k in ("pmid", "title", "authors", "journal", "year", "pub_date", "pub_types", "url"):
            if not p.get(k) and meta.get(k):
                p[k] = meta[k]
        if not p.get("section"):
            p["section"] = ic.infer_section(meta.get("title") or p.get("title") or "", meta.get("journal") or p.get("journal"))
        p.pop("_raw_url", None)
        p.pop("_from_dois_file", None)

        abstract = meta.get("abstract")
        year_for_abstract = p.get("year") or (p.get("pub_date") or "")[:4] or "unknown"

        doi, created = ic.upsert(records, p, digest_id)
        dois.append(doi)
        if created:
            n_added += 1
            if abstract and not dry_run:
                ic.store_abstract(doi, year_for_abstract, abstract)
        else:
            n_merged += 1

    digests = ic.register_digest(digests, digest_id, "monthly", date, title, dois, path or dois_file)

    if not dry_run:
        ic.strip_transient_fields(records)
        catalog_io.save_all(records)
        catalog_io.save_digests(digests)

    print(f"ingest: {digest_id} +{n_added} added ~{n_merged} merged")
    return n_added, n_merged, digest_id


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dois-file")
    args = ap.parse_args(argv)

    if not args.path and not args.dois_file:
        print("PARSE ERROR: must supply a digest path or --dois-file", file=sys.stderr)
        sys.exit(1)

    try:
        run(args.path, args.dry_run, args.dois_file)
    except ParseFailure as e:
        print(f"PARSE ERROR: {e}", file=sys.stderr)
        print("---- offending block ----", file=sys.stderr)
        print(e.block, file=sys.stderr)
        sys.exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
