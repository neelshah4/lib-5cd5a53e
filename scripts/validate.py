#!/usr/bin/env python3
"""Validate the built reading-library repo. Exit 0 on pass, 1 on any failure.
Prints one line per check: OK / FAIL <detail>.
"""
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sections  # noqa: E402

CATALOG_KEYS = {
    "doi": str, "pmid": (str, type(None)), "title": str, "authors": list,
    "journal": (str, type(None)), "year": (int, type(None)), "pub_date": (str, type(None)),
    "pub_types": list, "section": str, "subtopic": (str, type(None)), "tags": list,
    "take": str, "impact": (int, float, type(None)), "digests": list,
    "first_seen": (str, type(None)), "source": (str, type(None)), "confidence": (int, float, type(None)),
    "verified": bool, "oa": bool, "has_abstract": bool, "url": (str, type(None)),
}

DIGEST_ID_RE = re.compile(r"^\d{4}-(W\d{2}|\d{2})$")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    args = ap.parse_args()
    root = os.path.abspath(args.root)

    results = []  # (bool, message)

    def check(ok, msg):
        results.append((ok, msg))
        print("OK" if ok else f"FAIL {msg}")

    # ---- Check 1: catalog.json parses, is a list, exact 22 keys, right types
    catalog_path = os.path.join(root, "data", "catalog.json")
    catalog = None
    try:
        with open(catalog_path, encoding="utf-8") as f:
            catalog = json.load(f)
        if not isinstance(catalog, list):
            raise ValueError("catalog.json top-level is not a list")
        bad = []
        for i, rec in enumerate(catalog):
            if not isinstance(rec, dict):
                bad.append(f"record {i} is not an object")
                continue
            keys = set(rec.keys())
            expected = set(CATALOG_KEYS.keys())
            if keys != expected:
                missing = expected - keys
                extra = keys - expected
                bad.append(f"record {i} key mismatch missing={missing} extra={extra}")
                continue
            for k, t in CATALOG_KEYS.items():
                if not isinstance(rec[k], t):
                    bad.append(f"record {i} field {k!r} wrong type {type(rec[k]).__name__}")
            if len(bad) > 5:
                bad.append("... (more errors truncated)")
                break
        if bad:
            check(False, "check1 catalog schema: " + "; ".join(bad))
        else:
            check(True, "check1 catalog.json parses, is a list, records have exactly 22 keys with correct types")
    except Exception as e:
        check(False, f"check1 catalog.json failed to parse/validate: {e}")

    # ---- Check 2: doi lowercase, non-empty, unique
    if catalog is not None:
        dois = [rec.get("doi") for rec in catalog if isinstance(rec, dict)]
        bad_doi = [d for d in dois if not d or d != d.lower()]
        dup_dois = len(dois) - len(set(dois))
        if bad_doi:
            check(False, f"check2 {len(bad_doi)} doi(s) empty or not lowercase")
        elif dup_dois:
            check(False, f"check2 {dup_dois} duplicate doi(s)")
        else:
            check(True, "check2 all dois lowercase, non-empty, unique")
    else:
        check(False, "check2 skipped: catalog not loaded")

    # ---- Check 3: section in SECTIONS
    if catalog is not None:
        bad_sections = [rec.get("section") for rec in catalog if isinstance(rec, dict) and rec.get("section") not in sections.SECTIONS]
        if bad_sections:
            check(False, f"check3 {len(bad_sections)} record(s) with section not in SECTIONS: {set(bad_sections)}")
        else:
            check(True, "check3 all sections in canonical SECTIONS list")
    else:
        check(False, "check3 skipped: catalog not loaded")

    # ---- Check 4: verified bool, confidence number 0-1
    if catalog is not None:
        bad4 = []
        for i, rec in enumerate(catalog):
            if not isinstance(rec.get("verified"), bool):
                bad4.append(f"record {i} verified not bool")
            conf = rec.get("confidence")
            if conf is not None:
                if not isinstance(conf, (int, float)) or isinstance(conf, bool) or not (0 <= conf <= 1):
                    bad4.append(f"record {i} confidence out of [0,1] or wrong type: {conf!r}")
        if bad4:
            check(False, "check4: " + "; ".join(bad4[:5]) + (" ..." if len(bad4) > 5 else ""))
        else:
            check(True, "check4 verified is bool and confidence in [0,1] on every record")
    else:
        check(False, "check4 skipped: catalog not loaded")

    # ---- Check 5: digest id format + 2099- guard on main
    if catalog is not None:
        branch = "main"
        try:
            out = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=root, capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                branch = out.stdout.strip()
        except Exception:
            branch = "main"

        bad5 = []
        for i, rec in enumerate(catalog):
            for d in rec.get("digests", []):
                if not isinstance(d, str) or not DIGEST_ID_RE.match(d):
                    bad5.append(f"record {i} digest id {d!r} does not match pattern")
                if branch == "main" and isinstance(d, str) and d.startswith("2099-"):
                    bad5.append(f"record {i} digest id {d!r} is a 2099- test fixture on branch main")
        if bad5:
            check(False, "check5: " + "; ".join(bad5[:5]) + (" ..." if len(bad5) > 5 else ""))
        else:
            check(True, f"check5 all digest ids well-formed; no 2099- fixtures on branch {branch!r}")
    else:
        check(False, "check5 skipped: catalog not loaded")

    # ---- Check 6: digests.json parses, items valid, dois exist in catalog
    digests_path = os.path.join(root, "data", "digests.json")
    try:
        with open(digests_path, encoding="utf-8") as f:
            digests = json.load(f)
        if not isinstance(digests, list):
            raise ValueError("digests.json top-level is not a list")
        catalog_dois = {rec.get("doi") for rec in catalog} if catalog is not None else set()
        bad6 = []
        valid_kinds = {"weekly", "monthly", "manual"}
        date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for i, item in enumerate(digests):
            if not isinstance(item, dict):
                bad6.append(f"digest {i} not an object")
                continue
            required = {"id", "kind", "date", "title", "dois", "counts", "source_file"}
            missing = required - set(item.keys())
            if missing:
                bad6.append(f"digest {i} missing keys {missing}")
            if item.get("kind") not in valid_kinds:
                bad6.append(f"digest {i} kind {item.get('kind')!r} not in {valid_kinds}")
            if not isinstance(item.get("date"), str) or not date_re.match(item.get("date") or ""):
                bad6.append(f"digest {i} date {item.get('date')!r} not YYYY-MM-DD")
            if not isinstance(item.get("dois"), list):
                bad6.append(f"digest {i} dois not a list")
            elif not isinstance(item.get("counts"), dict):
                bad6.append(f"digest {i} counts not a dict")
            else:
                for d in item.get("dois", []):
                    if d not in catalog_dois:
                        bad6.append(f"digest {i} references unknown doi {d!r}")
        if bad6:
            check(False, "check6: " + "; ".join(bad6[:5]) + (" ..." if len(bad6) > 5 else ""))
        else:
            check(True, f"check6 digests.json parses, {len(digests)} item(s) valid, all dois exist in catalog")
    except Exception as e:
        check(False, f"check6 digests.json failed: {e}")

    # ---- Check 7: no .pdf files under root (excluding .git)
    pdf_hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            if fn.lower().endswith(".pdf"):
                pdf_hits.append(os.path.relpath(os.path.join(dirpath, fn), root))
    if pdf_hits:
        check(False, f"check7 {len(pdf_hits)} .pdf file(s) found: {pdf_hits[:5]}")
    else:
        check(True, "check7 no .pdf files under root")

    # ---- Check 8: catalog.json size < 15MB
    try:
        size = os.path.getsize(catalog_path)
        if size < 15 * 1024 * 1024:
            check(True, f"check8 catalog.json size {size} bytes < 15MB")
        else:
            check(False, f"check8 catalog.json size {size} bytes >= 15MB")
    except Exception as e:
        check(False, f"check8 failed: {e}")

    # ---- Check 9: slug leak check
    slug = os.environ.get("LIBRARY_SLUG")
    if not slug:
        print("SKIP slug check (LIBRARY_SLUG unset)")
    else:
        try:
            out = subprocess.run(
                ["grep", "-rIl", "--exclude-dir=.git", slug, "."],
                cwd=root, capture_output=True, text=True, timeout=30,
            )
            hits = [l for l in out.stdout.splitlines() if l.strip()]
            if hits:
                check(False, f"check9 slug {slug!r} found in: {hits}")
            else:
                check(True, f"check9 slug {slug!r} not found anywhere")
        except Exception as e:
            check(False, f"check9 failed: {e}")

    # ---- Check 10: zotero_state.json parses to dict
    zotero_path = os.path.join(root, "data", "zotero_state.json")
    try:
        with open(zotero_path, encoding="utf-8") as f:
            zstate = json.load(f)
        if isinstance(zstate, dict):
            check(True, "check10 zotero_state.json parses to a dict")
        else:
            check(False, f"check10 zotero_state.json top-level is {type(zstate).__name__}, not dict")
    except Exception as e:
        check(False, f"check10 zotero_state.json failed: {e}")

    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print(f"validate: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
