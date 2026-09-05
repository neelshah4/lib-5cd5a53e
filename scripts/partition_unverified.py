#!/usr/bin/env python3
"""Keep verified records in data/catalog.json (published); move verified=false to local/unverified.json (gitignored).
Idempotent. Run after migrate_seed.py or apply_recheck.py. Records flow back into data/ when verified flips true."""
import json, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
pub = ROOT / "data" / "catalog.json"
loc = ROOT / "local" / "unverified.json"
loc.parent.mkdir(exist_ok=True)
recs = json.load(open(pub))
recs += json.load(open(loc)) if loc.exists() else []
by = {}
for r in recs:  # dedupe on doi, prefer the verified copy
    k = r["doi"]
    if k not in by or (r["verified"] and not by[k]["verified"]):
        by[k] = r
allr = sorted(by.values(), key=lambda r: r["doi"])
v = [r for r in allr if r["verified"]]
u = [r for r in allr if not r["verified"]]
json.dump(v, open(pub, "w"), indent=1, ensure_ascii=False)
json.dump(u, open(loc, "w"), indent=1, ensure_ascii=False)
print(f"published={len(v)} unverified_local={len(u)} total={len(allr)}")
