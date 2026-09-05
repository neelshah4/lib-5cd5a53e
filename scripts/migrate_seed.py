#!/usr/bin/env python3
"""One-shot, idempotent migration of the seed master-catalog.csv into
data/catalog.json (+ data/digests.json, data/zotero_state.json).

Stdlib only. Never invents a value: unknown -> null.
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sections  # noqa: E402

SEED_CSV = "/Users/neel/Downloads/Claude Sandbox/interesting-articles-library/master-catalog.csv"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
REVIEW_DIR = os.path.join(ROOT, "review")

KEYS_ORDER = [
    "doi", "pmid", "title", "authors", "journal", "year", "pub_date",
    "pub_types", "section", "subtopic", "tags", "take", "impact", "digests",
    "first_seen", "source", "confidence", "verified", "oa", "has_abstract",
    "url",
]


def parse_first_seen(ia_path: str):
    """'2026/03 Mar' -> '2026-03-01'; blank/unparsable -> None."""
    if not ia_path or not ia_path.strip():
        return None
    s = ia_path.strip()
    # expected shape: YYYY/MM Mon
    parts = s.split("/")
    if len(parts) != 2:
        return None
    year_part = parts[0].strip()
    rest = parts[1].strip()
    month_part = rest.split(" ")[0].strip()
    if not (year_part.isdigit() and len(year_part) == 4):
        return None
    if not (month_part.isdigit() and 1 <= int(month_part) <= 12):
        return None
    return f"{year_part}-{month_part.zfill(2)}-01"


def to_bool(s: str) -> bool:
    return str(s).strip() == "True"


def to_str_or_none(s):
    if s is None:
        return None
    s = s.strip()
    return s if s else None


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(REVIEW_DIR, exist_ok=True)

    with open(SEED_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    seen_dois = {}
    duplicates = []
    records = []

    for row in rows:
        doi_raw = (row.get("doi") or "").strip()
        doi = doi_raw.lower()

        if not doi:
            # No DOI is not something we can key on; conservative choice:
            # skip and log as a duplicate-style anomaly for visibility.
            duplicates.append({**row, "_reason": "missing_doi"})
            continue

        if doi in seen_dois:
            duplicates.append({**row, "_reason": "duplicate_doi"})
            continue
        seen_dois[doi] = True

        pmid = to_str_or_none(row.get("pmid"))

        title = (row.get("title") or "").strip()

        authors_raw = (row.get("authors") or "").strip()
        authors = [a.strip() for a in authors_raw.split(";") if a.strip()] if authors_raw else []
        # spec says split on "; " but tolerate bare ";" too; re-split precisely per "; "
        if authors_raw:
            authors = [a for a in (p.strip() for p in authors_raw.split("; ")) if a]

        journal = to_str_or_none(row.get("journal"))

        year_raw = (row.get("year") or "").strip()
        year = int(year_raw) if year_raw.isdigit() else None

        section_raw = (row.get("section") or "").strip()
        section = sections.canonical(section_raw)  # must not raise on seed data

        subtopic = to_str_or_none(row.get("subtopic"))

        first_seen = parse_first_seen(row.get("ia_path") or "")

        conf_raw = (row.get("match_confidence") or "").strip()
        try:
            confidence = float(conf_raw) if conf_raw else None
        except ValueError:
            confidence = None

        needs_review_raw = (row.get("needs_review") or "").strip()
        verified = bool(
            confidence is not None
            and confidence >= 0.90
            and needs_review_raw != "True"
        )

        oa = to_bool(row.get("oa_is") or "")

        url = to_str_or_none(row.get("url"))

        record = {
            "doi": doi,
            "pmid": pmid,
            "title": title,
            "authors": authors,
            "journal": journal,
            "year": year,
            "pub_date": None,
            "pub_types": [],
            "section": section,
            "subtopic": subtopic,
            "tags": [],
            "take": "",
            "impact": None,
            "digests": [],
            "first_seen": first_seen,
            "source": "archive",
            "confidence": confidence,
            "verified": verified,
            "oa": oa,
            "has_abstract": False,
            "url": url,
        }
        # enforce exact key order
        record = {k: record[k] for k in KEYS_ORDER}
        records.append(record)

    records.sort(key=lambda r: r["doi"])

    catalog_path = os.path.join(DATA_DIR, "catalog.json")
    with open(catalog_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=1, ensure_ascii=False)
        f.write("\n")

    dup_path = os.path.join(REVIEW_DIR, "seed_duplicates.csv")
    if duplicates:
        fieldnames = list(rows[0].keys()) + ["_reason"] if rows else ["_reason"]
        with open(dup_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for d in duplicates:
                writer.writerow(d)
    else:
        # no duplicates: write empty file with header only, so downstream
        # `wc -l` / existence checks are well-defined and idempotent.
        if not os.path.exists(dup_path):
            with open(dup_path, "w", encoding="utf-8") as f:
                pass

    digests_path = os.path.join(DATA_DIR, "digests.json")
    if not os.path.exists(digests_path):
        with open(digests_path, "w", encoding="utf-8") as f:
            json.dump([], f)

    zotero_path = os.path.join(DATA_DIR, "zotero_state.json")
    if not os.path.exists(zotero_path):
        with open(zotero_path, "w", encoding="utf-8") as f:
            json.dump({}, f)

    print(f"rows_read={len(rows)} records_written={len(records)} duplicates={len(duplicates)}")


if __name__ == "__main__":
    main()
