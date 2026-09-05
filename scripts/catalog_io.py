#!/usr/bin/env python3
"""Single IO layer for the catalog. Two physical files, one logical set:
  data/catalog.json      published (verified=True only)   -- tracked, served by Pages
  local/unverified.json  held back (verified=False)       -- gitignored
Every script reads with load_all() and writes with save_all(); the partition is enforced here."""
import json, pathlib, os, tempfile

def _dump(obj, path, **kw):
    """Atomic write: temp file in the same dir, fsync, os.replace."""
    path = pathlib.Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, **kw); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)

ROOT = pathlib.Path(__file__).resolve().parent.parent
PUB = ROOT / "data" / "catalog.json"
LOC = ROOT / "local" / "unverified.json"
DIGESTS = ROOT / "data" / "digests.json"
ZSTATE = ROOT / "data" / "zotero_state.json"

KEYS = ["doi","pmid","title","authors","journal","year","pub_date","pub_types","section","subtopic",
        "tags","take","impact","digests","first_seen","source","confidence","verified","oa","has_abstract","url"]

def norm_doi(doi: str) -> str:
    d = (doi or "").strip().lower()
    for p in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(p): d = d[len(p):]
    return d

def _load(p):
    return json.load(open(p, encoding="utf-8")) if p.exists() else []

def load_all() -> dict:
    """doi -> record, verified and unverified together."""
    out = {}
    for r in _load(PUB) + _load(LOC):
        k = norm_doi(r["doi"])
        if k not in out or (r.get("verified") and not out[k].get("verified")):
            out[k] = r
    return out

def save_all(by_doi: dict) -> tuple[int, int]:
    recs = sorted(by_doi.values(), key=lambda r: r["doi"])
    for r in recs:
        r["doi"] = norm_doi(r["doi"])
        for k in KEYS: r.setdefault(k, None)
    v = [r for r in recs if r["verified"] is True]
    u = [r for r in recs if r["verified"] is not True]
    LOC.parent.mkdir(exist_ok=True)
    _dump(v, PUB, indent=1, ensure_ascii=False)
    _dump(u, LOC, indent=1, ensure_ascii=False)
    return len(v), len(u)

def load_digests() -> list:  return _load(DIGESTS) or []
def save_digests(d: list):    _dump(sorted(d, key=lambda x: x["date"]), DIGESTS, indent=1, ensure_ascii=False)
def load_zstate() -> dict:    return json.load(open(ZSTATE)) if ZSTATE.exists() else {}
def save_zstate(s: dict):     _dump(s, ZSTATE, indent=1, sort_keys=True)

def new_record(**kw) -> dict:
    r = {k: None for k in KEYS}
    r.update(authors=[], pub_types=[], tags=[], digests=[], take="", verified=False, oa=False, has_abstract=False)
    r.update(kw); r["doi"] = norm_doi(r["doi"]); return r
