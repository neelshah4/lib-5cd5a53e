#!/usr/bin/env python3
"""Re-verify seed rows with match_confidence < 0.90 OR needs_review == "True" against Crossref.

Reads: /Users/neel/Downloads/Claude Sandbox/interesting-articles-library/master-catalog.csv (read-only)
Writes: review/recheck_overlay.json, review/residue.csv, scripts/apply_recheck.py (not run)
Cache: review/recheck_cache/<md5(query)>.json

Does NOT touch data/catalog.json.
"""
import argparse
import csv
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse

import requests
from rapidfuzz import fuzz

SEED = "/Users/neel/Downloads/Claude Sandbox/interesting-articles-library/master-catalog.csv"
ROOT = "/Users/neel/critical-care-reading-library"
CACHE_DIR = os.path.join(ROOT, "review", "recheck_cache")
OVERLAY_PATH = os.path.join(ROOT, "review", "recheck_overlay.json")
RESIDUE_PATH = os.path.join(ROOT, "review", "residue.csv")
EMAIL = "neels31@gmail.com"
UA = f"neel-reference-library/1.0 (mailto:{EMAIL})"

SLEEP = 0.35
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})

GENERIC = {
    "pediatric critical care medicine", "critical care medicine", "anesthesiology", "shock",
    "pubmed", "sciencedirect", "redirecting", "", "circulation", "chest", "intensive care medicine",
    "critical care", "jama", "the lancet", "pediatrics", "resuscitation", "neurology",
}

DROP_SLUG_TOKENS = {
    "full", "fulltext", "abstract", "article", "html", "epdf", "pdf", "meta", "aspx", "asp",
    "en", "amp", "articleid", "articleview", "content", "citation", "cgi", "landing", "doi",
}


def clean_title(t):
    t = html.unescape(t or "").strip()
    for sep in [" | ", " - ", " — ", " : "]:
        if sep in t:
            parts = t.split(sep)
            if len(parts[0]) >= 15:
                t = parts[0]
    return t.strip()


def is_generic(t):
    tt = (t or "").strip().lower()
    return tt in GENERIC or len(tt) < 6


def lww_slug_title(url):
    if "lww.com" not in url:
        return None
    seg = [s for s in urllib.parse.urlparse(url).path.split("/") if s]
    if not seg:
        return None
    s = re.sub(r"\.aspx$", "", seg[-1], flags=re.I)
    if re.fullmatch(r"\d+", s):
        return None
    t = re.sub(r"\s+", " ", s.replace("_", " ").replace(".", " ").replace("-", " ")).strip()
    return t if len(t.split()) > 4 else None


def generic_slug_title(url):
    """De-hyphenate the last meaningful path segment of a URL into a title-like string."""
    try:
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path)
    except Exception:
        return None
    segs = [s for s in path.split("/") if s]
    # walk from the end, skip pure-numeric ids and known junk tokens
    for seg in reversed(segs):
        s = re.sub(r"\.(aspx|html?|php|pdf)$", "", seg, flags=re.I)
        if re.fullmatch(r"\d+", s):
            continue
        if s.lower() in DROP_SLUG_TOKENS:
            continue
        t = s.replace("-", " ").replace("_", " ").replace("+", " ")
        t = re.sub(r"\s+", " ", t).strip()
        # strip a leading/trailing bare numeric id fragment e.g. "12345-some-title"
        t = re.sub(r"^\d+\s+", "", t)
        t = re.sub(r"\s+\d+$", "", t)
        words = t.split()
        if len(words) > 4 and not t.isdigit():
            return t
    return None


def build_claimed_title(row):
    t = clean_title(row.get("title", ""))
    if t and not is_generic(t) and len(t.split()) > 4:
        return t, "row_title"
    url = row.get("url") or ""
    lww = lww_slug_title(url)
    if lww:
        return lww, "lww_slug"
    gen = generic_slug_title(url)
    if gen:
        return gen, "url_slug"
    return None, None


def cache_path(query):
    h = hashlib.md5(query.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{h}.json")


def crossref_query(title):
    cp = cache_path(title)
    if os.path.exists(cp):
        try:
            return json.load(open(cp))
        except Exception:
            pass
    params = {
        "query.bibliographic": title,
        "rows": 3,
        "mailto": EMAIL,
        "select": "DOI,title,author,container-title,published,issued,volume,issue,page,type",
    }
    result = {"ok": False, "items": []}
    for attempt in range(2):
        try:
            r = SESSION.get("https://api.crossref.org/works", params=params, timeout=20)
            if r.status_code == 200:
                items = r.json().get("message", {}).get("items", [])
                result = {"ok": True, "items": items}
                break
            elif r.status_code == 429 or r.status_code >= 500:
                if attempt == 0:
                    time.sleep(2)
                    continue
                else:
                    result = {"ok": False, "items": [], "error": f"http_{r.status_code}"}
            else:
                result = {"ok": False, "items": [], "error": f"http_{r.status_code}"}
                break
        except Exception as e:
            if attempt == 0:
                time.sleep(2)
                continue
            result = {"ok": False, "items": [], "error": str(e)}
    time.sleep(SLEEP)
    json.dump(result, open(cp, "w"))
    return result


def cand_title(it):
    ct = it.get("title") or [""]
    return ct[0] if isinstance(ct, list) and ct else (ct or "")


def cand_doi(it):
    return (it.get("DOI") or "").lower()


def load_seed_rows():
    rows = list(csv.DictReader(open(SEED)))
    out = []
    for row in rows:
        mc = row.get("match_confidence") or ""
        try:
            mc_val = float(mc) if mc != "" else None
        except ValueError:
            mc_val = None
        needs_review = row.get("needs_review") == "True"
        low_conf = (mc_val is None) or (mc_val < 0.90)
        if low_conf or needs_review:
            out.append(row)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(OVERLAY_PATH), exist_ok=True)

    rows = load_seed_rows()
    if args.limit:
        rows = rows[: args.limit]

    overlay = {}
    if args.resume and os.path.exists(OVERLAY_PATH):
        try:
            overlay = json.load(open(OVERLAY_PATH))
        except Exception:
            overlay = {}

    residue_rows = []
    n_no_title = 0
    n_error = 0
    n_processed = 0

    for i, row in enumerate(rows, 1):
        doi = (row.get("doi") or "").strip().lower()
        url = row.get("url") or ""
        orig_conf_raw = row.get("match_confidence") or ""
        try:
            orig_conf = float(orig_conf_raw) if orig_conf_raw != "" else None
        except ValueError:
            orig_conf = None

        key = doi if doi else f"__nodoi__:{url}"

        if args.resume and key in overlay:
            n_processed += 1
            if i % 100 == 0:
                print(f"progress {i}/{len(rows)} (resumed)")
            continue

        claimed, source = build_claimed_title(row)
        if not claimed:
            n_no_title += 1
            overlay[key] = {
                "confidence": orig_conf,
                "verified": False,
                "matched_title": None,
                "reason": "no_title",
            }
            n_processed += 1
            if i % 100 == 0:
                print(f"progress {i}/{len(rows)}")
            continue

        result = crossref_query(claimed)
        if not result.get("ok"):
            n_error += 1
            overlay[key] = {
                "confidence": orig_conf,
                "verified": False,
                "matched_title": None,
                "reason": "error",
            }
            residue_rows.append({
                "doi": doi, "original_confidence": orig_conf, "best_score": None,
                "reason": "error", "claimed_title": claimed, "matched_title": "", "url": url,
            })
            n_processed += 1
            if i % 100 == 0:
                print(f"progress {i}/{len(rows)}")
            continue

        items = result["items"]
        best = None
        best_score = 0.0
        doi_in_candidates = False
        for it in items:
            ct = cand_title(it)
            sc = fuzz.token_set_ratio(claimed, ct) / 100.0
            cd = cand_doi(it)
            if doi and cd == doi:
                doi_in_candidates = True
            if sc > best_score:
                best_score = sc
                best = it

        best_doi = cand_doi(best) if best else ""
        best_title = cand_title(best) if best else None
        doi_matches_best = bool(doi) and best_doi == doi

        if best_score >= 0.92 and doi_matches_best:
            verified = True
            reason = "doi_match_high"
            new_conf = best_score
        elif doi_matches_best:
            verified = False
            reason = "doi_match_low"
            new_conf = best_score
        else:
            verified = False
            reason = "doi_not_in_candidates"
            new_conf = orig_conf

        overlay[key] = {
            "confidence": new_conf,
            "verified": verified,
            "matched_title": best_title,
            "reason": reason,
        }

        if not verified:
            residue_rows.append({
                "doi": doi, "original_confidence": orig_conf, "best_score": best_score,
                "reason": reason, "claimed_title": claimed, "matched_title": best_title or "",
                "url": url,
            })

        n_processed += 1
        if i % 100 == 0:
            print(f"progress {i}/{len(rows)} processed={n_processed} errors={n_error} no_title={n_no_title}")

    json.dump(overlay, open(OVERLAY_PATH, "w"), indent=2)

    residue_rows.sort(key=lambda r: (r["best_score"] if r["best_score"] is not None else -1), reverse=True)
    with open(RESIDUE_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["doi", "original_confidence", "best_score", "reason",
                                          "claimed_title", "matched_title", "url"])
        w.writeheader()
        for r in residue_rows:
            w.writerow(r)

    print(f"DONE processed={n_processed} no_title={n_no_title} errors={n_error} "
          f"verified={sum(1 for v in overlay.values() if v['verified'])} residue={len(residue_rows)}")


if __name__ == "__main__":
    main()
