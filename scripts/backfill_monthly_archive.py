#!/usr/bin/env python3
"""Reconstruct the monthly "Interesting Articles" email digest history from
the Firefox bookmark month folders already recorded in the seed catalog.

Source data (outside the repo, read-only, no network calls):
  - master-catalog.csv : ia_path column records each DOI's Firefox folder
    ("YYYY/MM Mon" for the 62 monthly folders, or one of 6 "Topics/..." /
    "Recent — to file" folders).
  - bookmarks-CLEAN-2026-06-08.json : Firefox JSON export. Each monthly or
    topic folder under "📚 Interesting Articles" has a matching subtree here;
    leaf bookmarks carry dateAdded (microseconds since epoch).

For each YYYY/MM Mon folder this registers one monthly digest issue (id
YYYY-MM) via ingest_common.register_digest, dated to the latest dateAdded
among that folder's bookmarks (falling back to the last day of the month if
no date can be matched), and appends that digest id to every existing catalog
record (published or held) whose DOI appears in the folder.

For the 6 topic folders, no digest issue is created; matching records get a
`topic:<slug>` tag instead.

Never resolves a DOI, never creates a catalog record, never touches
first_seen/verified/section. Idempotent: a second run changes nothing.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import datetime
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import catalog_io  # noqa: E402
import ingest_common as ic  # noqa: E402

CSV_PATH = pathlib.Path(
    "/Users/neel/Downloads/Claude Sandbox/interesting-articles-library/master-catalog.csv"
)
BOOKMARKS_PATH = pathlib.Path(
    "/Users/neel/Downloads/Claude Sandbox/firefox-bookmark-cleanup/bookmarks-CLEAN-2026-06-08.json"
)
BOOKMARKS_ROOT_TITLE = "📚 Interesting Articles"

MONTH_FOLDER_RE = re.compile(r"^(\d{4})/(\d{2}) ([A-Za-z]{3})$")

MONTH_ABBR_TO_NUM = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def load_csv_folders(csv_path: pathlib.Path) -> dict:
    """ia_path -> sorted list of normalized DOIs (deduped)."""
    folders: dict[str, set[str]] = {}
    for row in csv.DictReader(open(csv_path, encoding="utf-8")):
        path = row.get("ia_path") or ""
        doi = catalog_io.norm_doi(row.get("doi") or "")
        if not path:
            continue
        folders.setdefault(path, set())
        if doi:
            folders[path].add(doi)
    return {k: sorted(v) for k, v in folders.items()}


def find_named(node: dict, name: str):
    if node.get("title") == name:
        return node
    for c in node.get("children") or []:
        r = find_named(c, name)
        if r:
            return r
    return None


def load_bookmark_groups(bookmarks_path: pathlib.Path) -> dict:
    """relative folder path (joined by '/') -> list of dateAdded ints."""
    data = json.load(open(bookmarks_path, encoding="utf-8"))
    root = find_named(data, BOOKMARKS_ROOT_TITLE)
    groups: dict[str, list[int]] = {}
    if root is None:
        return groups

    def walk(node, relpath):
        if "children" in node:
            title = node.get("title", "")
            newrel = relpath + [title]
            for c in node["children"]:
                walk(c, newrel)
        else:
            key = "/".join(relpath)
            da = node.get("dateAdded")
            if da:
                groups.setdefault(key, []).append(da)

    for c in root.get("children") or []:
        walk(c, [])
    return groups


def slugify(ia_path: str) -> str:
    p = ia_path
    if p.startswith("Topics/"):
        p = p[len("Topics/"):]
    p = p.rstrip("/")
    p = p.lower()
    p = re.sub(r"[—–]", "-", p)
    p = re.sub(r"[^a-z0-9]+", "-", p)
    p = re.sub(r"-+", "-", p).strip("-")
    return f"topic:{p}"


def month_date(year: int, month: int, dates_us: list[int] | None) -> tuple[str, bool]:
    """Returns (ISO date, used_bookmark_date)."""
    last_day = calendar.monthrange(year, month)[1]
    if dates_us:
        mx = max(dates_us)
        dt = datetime.datetime.fromtimestamp(mx / 1_000_000, tz=datetime.timezone.utc).date()
        # Accept the bookmark date only if it plausibly is the send date: inside the
        # month or within 15 days after it. Bulk bookmark reorganisations stamp dozens
        # of months with one date (22 months read 2022-10-26); those fall back to month end.
        lo = datetime.date(year, month, 1)
        hi = datetime.date(year, month, last_day) + datetime.timedelta(days=15)
        if lo <= dt <= hi:
            return dt.isoformat(), True
    return datetime.date(year, month, last_day).isoformat(), False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    csv_folders = load_csv_folders(CSV_PATH)
    bookmark_groups = load_bookmark_groups(BOOKMARKS_PATH)

    month_folders = sorted(p for p in csv_folders if MONTH_FOLDER_RE.match(p))
    topic_folders = sorted(p for p in csv_folders if not MONTH_FOLDER_RE.match(p))

    records = catalog_io.load_all()
    digests = catalog_io.load_digests()

    csv_dois_seen = set()
    for dois in csv_folders.values():
        csv_dois_seen.update(dois)
    unmatched_dois = sorted(d for d in csv_dois_seen if d not in records)

    plan_rows = []  # (id, date, n_in_folder, n_matched_verified, n_matched_held, n_unmatched)
    bookmark_dated = 0
    fallback_dated = 0
    records_touched = set()
    digest_appends = 0
    topic_tag_appends = 0

    for ia_path in month_folders:
        m = MONTH_FOLDER_RE.match(ia_path)
        year, month_num, mon_abbr = int(m.group(1)), int(m.group(2)), m.group(3)
        digest_id = f"{year:04d}-{month_num:02d}"
        title = f"Interesting Articles — {calendar.month_abbr[month_num]} {year}"

        dates_us = bookmark_groups.get(ia_path)
        date_iso, used_bm = month_date(year, month_num, dates_us)
        if used_bm:
            bookmark_dated += 1
        else:
            fallback_dated += 1

        folder_dois = csv_folders[ia_path]
        matched_verified = []
        matched_held = 0
        unmatched = 0
        for doi in folder_dois:
            rec = records.get(doi)
            if rec is None:
                unmatched += 1
                continue
            existing_digests = rec.get("digests") or []
            if digest_id not in existing_digests:
                if not args.dry_run:
                    rec["digests"] = existing_digests + [digest_id]
                    if not rec.get("source"):
                        rec["source"] = "archive"
                records_touched.add(doi)
                digest_appends += 1
            if rec.get("verified") is True:
                matched_verified.append(doi)
            else:
                matched_held += 1

        if not args.dry_run:
            digests = ic.register_digest(
                digests, digest_id, "monthly", date_iso, title,
                matched_verified, f"firefox:{ia_path}",
            )

        plan_rows.append((digest_id, date_iso, len(folder_dois), len(matched_verified), matched_held, unmatched))

    for ia_path in topic_folders:
        slug = slugify(ia_path)
        for doi in csv_folders[ia_path]:
            rec = records.get(doi)
            if rec is None:
                continue
            tags = rec.get("tags") or []
            if slug not in tags:
                if not args.dry_run:
                    rec["tags"] = tags + [slug]
                records_touched.add(doi)
                topic_tag_appends += 1

    print(f"Month folders: {len(month_folders)}  Topic folders: {len(topic_folders)}")
    print(f"{'id':<10} {'date':<12} {'n_in_folder':>12} {'n_verified':>11} {'n_held':>7} {'n_unmatched':>12}")
    for row in plan_rows:
        print(f"{row[0]:<10} {row[1]:<12} {row[2]:>12} {row[3]:>11} {row[4]:>7} {row[5]:>12}")

    print()
    print(f"Dates from bookmarks: {bookmark_dated}  Fallback (last day of month): {fallback_dated}")
    print(f"Records with a digest id appended (this run): {digest_appends}")
    print(f"Records with a topic tag appended (this run): {topic_tag_appends}")
    print(f"Distinct CSV DOIs not found in catalog at all: {len(unmatched_dois)}")
    if unmatched_dois:
        print("  examples:", unmatched_dois[:5])

    if args.dry_run:
        print("\n--dry-run: no files written.")
        return

    v, u = catalog_io.save_all(records)
    catalog_io.save_digests(digests)
    print(f"\nWrote catalog: {v} verified / {u} unverified. Wrote {len(digests)} digest entries.")


if __name__ == "__main__":
    main()
