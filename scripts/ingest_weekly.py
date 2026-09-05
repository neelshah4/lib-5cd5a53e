#!/usr/bin/env python3
"""Ingest a pubmed-watcher weekly digest markdown file into the catalog.

Usage: ingest_weekly.py <path-to-digest.md> [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys

import catalog_io
import ingest_common as ic
import sections

# Headings that are known non-paper trailers -- never fed to sections.canonical()
# and never ingested. "Borderline" and "Practice-changing this week" are NOT
# trailers: their papers are ingested with an inferred section (see below).
TRAILER_PATTERNS = [
    re.compile(r"^additional candidates noted"),
    re.compile(r"^run metadata"),
    re.compile(r"^self-appraisal"),
]

BORDERLINE_RE = re.compile(r"^borderline")
PRACTICE_CHANGING_RE = re.compile(r"^practice-changing this week")

HEADER_DATE_RE = re.compile(r"[Ww]eek of (\d{4}-\d{2}-\d{2})")
# Fallback for non-standard headers (e.g. a "catch-up run, YYYY-MM-DD" title
# with no "Week of" phrasing at all): take the trailing date off the H1, or
# the end date of a "Date range: X to Y" line.
HEADER_DATE_FALLBACK_RES = [
    re.compile(r"^#.*?(\d{4}-\d{2}-\d{2})\s*$", re.M),
    re.compile(r"[Dd]ate range:\s*\d{4}-\d{2}-\d{2}\s*to\s*(\d{4}-\d{2}-\d{2})"),
]


class ParseFailure(Exception):
    def __init__(self, msg, block=""):
        super().__init__(msg)
        self.block = block


def normalize_heading(raw: str) -> str:
    s = raw.strip()
    s = re.sub(r"^[^\w]+", "", s)  # strip leading emoji/symbols like "⚡ "
    s = re.sub(r"\s*\(\d+\)\s*$", "", s)  # strip trailing "(N)" count annotation
    return s.strip()


def is_trailer(heading_norm: str) -> bool:
    low = re.sub(r"\s+", " ", heading_norm.strip().lower())
    return any(p.match(low) for p in TRAILER_PATTERNS)


def is_borderline(heading_norm: str) -> bool:
    low = re.sub(r"\s+", " ", heading_norm.strip().lower())
    return bool(BORDERLINE_RE.match(low))


def is_practice_changing(heading_norm: str) -> bool:
    low = re.sub(r"\s+", " ", heading_norm.strip().lower())
    return bool(PRACTICE_CHANGING_RE.match(low))


def parse_heading_title(head: str):
    """Returns (title, doi_from_link_or_None)."""
    s = head.strip()
    s = re.sub(r"^\d+\.\s*", "", s)
    s = re.sub(r"^\[[^\]]*\]\s*(?!\()", "", s)
    m = re.match(r"^\[(.*?)\]\((\S+?)\)\s*$", s)
    if m:
        title, url = m.group(1), m.group(2)
        du = re.search(r"doi\.org/(.+)$", url)
        return title, (du.group(1) if du else None)
    return s, None


def extract_doi(blk: str):
    m = re.search(r"doi\.org/([^\s\)\]]+)", blk)
    if m:
        return m.group(1)
    m = re.search(r"DOI:\*{0,2}\s*\[([^\]]+)\]", blk, re.I)
    if m:
        return m.group(1)
    m = re.search(r"DOI:\*{0,2}\s*([^\s\)\]]+)", blk, re.I)
    if m:
        return m.group(1)
    return None


def extract_pmid(blk: str):
    m = re.search(r"PMID:\*{0,2}\s*\[?(\d+)\]?", blk, re.I)
    return m.group(1) if m else None


def extract_journal_year_pt(blk: str):
    m = re.search(r"\*\*([A-Za-z][^*\n]*?,\s*(?:19|20)\d{2}[^*\n]*)\*\*\s*[—-]\s*([^\n]+)", blk)
    if m:
        blob, authors = m.group(1), m.group(2).strip()
        parts = [p.strip() for p in blob.split(",")]
        journal = parts[0] if parts else None
        year = None
        for p in parts:
            if re.match(r"^(19|20)\d{2}$", p):
                year = int(p)
                break
        pts = [p for p in parts[1:] if not re.match(r"^(19|20)\d{2}$", p)]
        return journal, year, pts, authors
    m_j = re.search(r"\*\*Journal/PT:\*\*\s*([^\n]+)", blk)
    m_a = re.search(r"\*\*Authors:\*\*\s*([^\n]+)", blk)
    if m_j:
        parts = [p.strip() for p in m_j.group(1).split(",")]
        journal = parts[0] if parts else None
        year = None
        for p in parts:
            if re.match(r"^(19|20)\d{2}$", p):
                year = int(p)
                break
        pts = [p for p in parts[1:] if not re.match(r"^(19|20)\d{2}$", p)]
        authors = m_a.group(1).strip() if m_a else None
        return journal, year, pts, authors
    m_j2 = re.search(r"\*\*Journal:\*\*\s*([^\|\n]+)\|\s*\*\*Year:\*\*\s*(\d{4})\s*\|\s*\*\*Type:\*\*\s*([^\n]+)", blk)
    if m_j2:
        journal = m_j2.group(1).strip()
        year = int(m_j2.group(2))
        pts = [p.strip() for p in m_j2.group(3).split(",")]
        return journal, year, pts, None
    m_it = re.search(r"\*([A-Za-z][^*\n]*?,\s*(?:19|20)\d{2}[^*\n]*)\.\*\s*([^\n]+)", blk)
    if m_it:
        blob, rest = m_it.group(1), m_it.group(2).strip()
        parts = [p.strip() for p in blob.split(",")]
        journal = parts[0] if parts else None
        year = None
        for p in parts:
            if re.match(r"^(19|20)\d{2}$", p):
                year = int(p)
                break
        pts = [p for p in parts[1:] if not re.match(r"^(19|20)\d{2}$", p)]
        authors = rest if rest and not rest.upper().startswith("PMID") else None
        return journal, year, pts, authors
    return None, None, [], None


def extract_take(blk: str) -> str:
    m = re.search(r"\*\*Summary:\*\*\s*([^\n]+)", blk)
    if m:
        return m.group(1).strip()
    m = re.search(r"\*\*Abstract summary:\*\*\s*([^\n]+)", blk)
    if m:
        return m.group(1).strip()
    m = re.search(
        r"\n\n([^\n].*?)\n\n\*\*(?:Practice impact|Summary \(practice\)):",
        blk, re.S,
    )
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def extract_impact(blk: str):
    m = re.search(r"\*\*(?:Practice impact|Summary \(practice\)):\*\*\s*(.+)", blk)
    if not m:
        return None
    rest = m.group(1)
    rest = rest.split("\n")[0]
    first = re.split(r"\.| —", rest)[0]
    first = first.strip().lower()
    return first or None


def split_sections(text: str):
    """Yields (heading_raw, body) for every top-level '## ' section."""
    parts = re.split(r"(?m)^## ", text)
    for part in parts[1:]:
        lines = part.split("\n", 1)
        heading = lines[0].strip()
        body = lines[1] if len(lines) > 1 else ""
        yield heading, body


def split_papers(body: str):
    """Yields (heading_line, block_text) for every '### ' paper block in body."""
    parts = re.split(r"(?m)^### ", body)
    for part in parts[1:]:
        lines = part.split("\n", 1)
        head = lines[0].strip()
        rest = lines[1] if len(lines) > 1 else ""
        yield head, "### " + head + "\n" + rest


def _build_paper(head, blk, section, section_inferred, tags, path):
    paper_title, doi_from_link = parse_heading_title(head)
    doi = doi_from_link or extract_doi(blk)
    pmid = extract_pmid(blk)
    if not doi and not pmid:
        raise ParseFailure(
            f"{path}: paper block has no resolvable DOI or PMID (title={paper_title!r})",
            blk,
        )
    journal, year, pub_types, authors_str = extract_journal_year_pt(blk)
    authors = None
    if authors_str:
        authors = [a.strip() for a in re.split(r",|\bet al\.?", authors_str) if a.strip()]
    take = extract_take(blk)
    impact = extract_impact(blk)
    rec = {
        "doi": doi,
        "pmid": pmid,
        "title": paper_title,
        "journal": journal,
        "year": year,
        "pub_types": pub_types or None,
        "authors": authors,
        "section": section,
        "take": take,
        "impact": impact,
        "source": "weekly",
        "_section_inferred": section_inferred,
    }
    if tags:
        rec["tags"] = list(tags)
    return rec


def parse_digest(text: str, path: str):
    m = HEADER_DATE_RE.search(text)
    if not m:
        for rx in HEADER_DATE_FALLBACK_RES:
            m = rx.search(text)
            if m:
                break
    if not m:
        raise ParseFailure(f"{path}: could not find 'Week of YYYY-MM-DD' in header", text[:300])
    date = m.group(1)
    digest_id = ic.iso_week_id(date)
    title = f"Weekly digest {date}"

    papers = []
    for heading_raw, body in split_sections(text):
        heading_norm = normalize_heading(heading_raw)
        if is_trailer(heading_norm):
            continue

        borderline = is_borderline(heading_norm)
        practice_changing = is_practice_changing(heading_norm)

        if borderline or practice_changing:
            section = None  # resolved per-paper via infer_section below
        else:
            try:
                section = sections.canonical(heading_norm)
            except ValueError as e:
                raise ParseFailure(f"{path}: unrecognized section heading {heading_raw!r}: {e}",
                                    heading_raw)

        for head, blk in split_papers(body):
            if borderline or practice_changing:
                # need title/journal before we can infer a section
                paper_title, _ = parse_heading_title(head)
                journal, *_rest = extract_journal_year_pt(blk)
                inferred = ic.infer_section(paper_title, journal)
                tags = ["borderline"] if borderline else None
                rec = _build_paper(head, blk, inferred, True, tags, path)
            else:
                rec = _build_paper(head, blk, section, False, None, path)
            papers.append(rec)
    return digest_id, date, title, papers


def run(path: str, dry_run: bool, resolve_fn=None):
    text = open(path, encoding="utf-8").read()
    digest_id, date, title, papers = parse_digest(text, path)

    if resolve_fn is None and papers:
        from resolve import resolve as resolve_fn  # lazy import

    records = catalog_io.load_all()
    digests = catalog_io.load_digests()

    n_added = 0
    n_merged = 0
    n_resolve_failed = 0
    dois = []
    for p in papers:
        identifier = p.get("doi") or p.get("pmid")
        try:
            meta = resolve_fn(identifier)
        except Exception as e:
            # A resolve failure is never fatal (the digest text may already
            # carry the DOI), but it must be visible: silently swallowing it
            # leaves a record permanently missing pmid/year/abstract.
            print(f"WARN: resolve failed for {identifier!r}: {e}", file=sys.stderr)
            n_resolve_failed += 1
            meta = {}
        if not p.get("doi"):
            p["doi"] = meta.get("doi")
        if not p["doi"]:
            if dry_run:
                print(f"WARN: could not resolve a DOI for PMID {p.get('pmid')!r} "
                      f"({p.get('title')!r}) -- skipped in dry-run", file=sys.stderr)
                continue
            raise ParseFailure(
                f"{path}: could not resolve a DOI for PMID {p.get('pmid')!r}",
                str(p),
            )
        for k in ("pmid", "title", "authors", "journal", "year", "pub_date", "pub_types", "url"):
            if not p.get(k) and meta.get(k):
                p[k] = meta[k]
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

    digests = ic.register_digest(digests, digest_id, "weekly", date, title, dois, path)

    if not dry_run:
        ic.strip_transient_fields(records)
        catalog_io.save_all(records)
        catalog_io.save_digests(digests)

    print(f"ingest: {digest_id} +{n_added} added ~{n_merged} merged"
          + (f" !{n_resolve_failed} resolve-failed" if n_resolve_failed else ""))
    return n_added, n_merged, digest_id


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    try:
        run(args.path, args.dry_run)
    except ParseFailure as e:
        print(f"PARSE ERROR: {e}", file=sys.stderr)
        print("---- offending block ----", file=sys.stderr)
        print(e.block, file=sys.stderr)
        sys.exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
