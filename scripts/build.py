#!/usr/bin/env python3
"""Build exports from the published (verified) catalog. No network.

Reads catalog_io.load_all() (verified + unverified merged) but EXPORTS ONLY
the verified subset. Writes:
  data/catalog.csv
  exports/library.ris / .bib / .csl.json
  exports/by-section/<RIS_SAFE name>.ris
  exports/digests/<digest-id>.ris
Also flips has_abstract True where a doi appears in data/abstracts/<year>.json
(the one write to the catalog this script performs, via save_all()) and
recomputes each digest's per-section counts (via save_digests()).

Idempotent: running twice produces byte-identical exports.
"""
import csv
import glob
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import catalog_io
from sections import SECTIONS, RIS_SAFE

EXPORTS = ROOT / "exports"
ABSTRACTS_DIR = ROOT / "data" / "abstracts"


def _join(v):
    if isinstance(v, list):
        return "; ".join(str(x) for x in v)
    if v is None:
        return ""
    return v


def ris_authors(rec):
    return [a.strip() for a in (rec.get("authors") or []) if a and a.strip()]


def kw_list(rec):
    kws = []
    if rec.get("section"):
        kws.append(rec["section"])
    if rec.get("subtopic"):
        kws.append(rec["subtopic"])
    for d in rec.get("digests") or []:
        kws.append(d)
    for t in rec.get("tags") or []:
        kws.append(t)
    return kws


def write_ris(records, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write("TY  - JOUR\n")
            f.write(f"TI  - {r.get('title') or ''}\n")
            for au in ris_authors(r):
                f.write(f"AU  - {au}\n")
            if r.get("journal"):
                f.write(f"JO  - {r['journal']}\n")
            if r.get("year"):
                f.write(f"PY  - {r['year']}\n")
            if r.get("doi"):
                f.write(f"DO  - {r['doi']}\n")
            if r.get("pmid"):
                f.write(f"AN  - {r['pmid']}\n")
            if r.get("url"):
                f.write(f"UR  - {r['url']}\n")
            for kw in kw_list(r):
                f.write(f"KW  - {kw}\n")
            f.write("ER  - \n\n")


def _bibkey(r, seen):
    au = ris_authors(r)
    fa = (au[0].split(",")[0] if au else "anon")
    base = re.sub(r"[^A-Za-z0-9]", "", f"{fa}{r.get('year') or ''}")
    base = base or "ref"
    k = base
    i = 0
    while k in seen:
        i += 1
        k = f"{base}{i}"
    seen.add(k)
    return k


def write_bib(records, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            k = _bibkey(r, seen)
            au = " and ".join(ris_authors(r))
            f.write(f"@article{{{k},\n")
            f.write(f"  title={{{r.get('title') or ''}}},\n")
            if au:
                f.write(f"  author={{{au}}},\n")
            if r.get("journal"):
                f.write(f"  journal={{{r['journal']}}},\n")
            if r.get("year"):
                f.write(f"  year={{{r['year']}}},\n")
            if r.get("doi"):
                f.write(f"  doi={{{r['doi']}}},\n")
            if r.get("url"):
                f.write(f"  url={{{r['url']}}}\n")
            f.write("}\n\n")


def write_csl(records, path):
    out = []
    for r in records:
        au = []
        for a in ris_authors(r):
            if "," in a:
                fam, giv = a.split(",", 1)
                au.append({"family": fam.strip(), "given": giv.strip()})
            else:
                au.append({"family": a})
        item = {
            "id": r.get("doi") or "",
            "type": "article-journal",
            "title": r.get("title") or "",
            "author": au,
            "container-title": r.get("journal") or "",
            "DOI": r.get("doi") or "",
            "URL": r.get("url") or "",
        }
        if r.get("year"):
            try:
                item["issued"] = {"date-parts": [[int(r["year"])]]}
            except (TypeError, ValueError):
                pass
        item["keyword"] = "; ".join(kw_list(r))
        out.append(item)
    path.parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def write_catalog_csv(records, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=catalog_io.KEYS, extrasaction="ignore")
        w.writeheader()
        for r in records:
            row = {k: _join(r.get(k)) for k in catalog_io.KEYS}
            w.writerow(row)


def apply_abstract_flips(all_records):
    """Set has_abstract=True where doi is present in data/abstracts/<year>.json.
    Returns the number of records flipped."""
    dois_with_abstract = set()
    for p in sorted(glob.glob(str(ABSTRACTS_DIR / "*.json"))):
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            for doi in data.keys():
                dois_with_abstract.add(catalog_io.norm_doi(doi))
        elif isinstance(data, list):
            for entry in data:
                doi = entry.get("doi") if isinstance(entry, dict) else None
                if doi:
                    dois_with_abstract.add(catalog_io.norm_doi(doi))

    flipped = 0
    for r in all_records.values():
        doi = catalog_io.norm_doi(r.get("doi") or "")
        if doi in dois_with_abstract and r.get("has_abstract") is not True:
            r["has_abstract"] = True
            flipped += 1
    return flipped


def recompute_digest_counts(verified_records):
    digests = catalog_io.load_digests()
    if not digests:
        return digests, 0
    by_id_counts = {}
    for r in verified_records:
        for d in r.get("digests") or []:
            counts = by_id_counts.setdefault(d, {})
            sec = r.get("section") or "Misc"
            counts[sec] = counts.get(sec, 0) + 1
    changed = 0
    for dg in digests:
        did = dg.get("id")
        new_counts = by_id_counts.get(did, {})
        if dg.get("counts") != new_counts:
            changed += 1
        dg["counts"] = new_counts
    return digests, changed


def main():
    all_records = catalog_io.load_all()

    flipped = apply_abstract_flips(all_records)
    if flipped:
        v, u = catalog_io.save_all(all_records)
        print(f"has_abstract flips: {flipped} (catalog re-saved: {v} verified / {u} unverified)")
    else:
        print("has_abstract flips: 0")

    verified = sorted(
        (r for r in all_records.values() if r.get("verified") is True),
        key=lambda r: r["doi"],
    )

    write_catalog_csv(verified, ROOT / "data" / "catalog.csv")
    print(f"data/catalog.csv: {len(verified)} records")

    write_ris(verified, EXPORTS / "library.ris")
    print(f"exports/library.ris: {len(verified)} records")

    write_bib(verified, EXPORTS / "library.bib")
    print(f"exports/library.bib: {len(verified)} records")

    write_csl(verified, EXPORTS / "library.csl.json")
    print(f"exports/library.csl.json: {len(verified)} records")

    for sec in SECTIONS:
        sub = [r for r in verified if r.get("section") == sec]
        fname = f"{RIS_SAFE[sec]}.ris"
        write_ris(sub, EXPORTS / "by-section" / fname)
        print(f"exports/by-section/{fname}: {len(sub)} records")

    digests, changed = recompute_digest_counts(verified)
    for dg in digests:
        did = dg.get("id")
        sub = [r for r in verified if did in (r.get("digests") or [])]
        write_ris(sub, EXPORTS / "digests" / f"{did}.ris")
        print(f"exports/digests/{did}.ris: {len(sub)} records")
    if digests:
        catalog_io.save_digests(digests)
    print(f"digest counts recomputed: {changed} of {len(digests)} digests changed")

    print("build: ok")


if __name__ == "__main__":
    main()
