#!/usr/bin/env python3
"""Resolve a DOI / PMID / URL to core metadata + abstract availability + OA status.

Ported from ~/Downloads/Claude Sandbox/interesting-articles-library/work/resolver.py:
kept the Crossref works/{doi} + works?query.bibliographic lookups, the NCBI
esummary/elink/esearch/efetch calls, the Unpaywall is_oa lookup, the UA string,
the mailto param, and the >=0.35s NCBI rate-lock. Dropped the ThreadPoolExecutor
and the landing-page HTML scrape (no requests dependency, stdlib urllib only).

CLI:
  python3 scripts/resolve.py <doi|pmid|url> [--json]
  python3 scripts/resolve.py --abstracts-for-catalog [--limit N]
"""
import os, re, sys, json, time, hashlib, pathlib, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from catalog_io import norm_doi, load_all  # noqa: E402

EMAIL = "neels31@gmail.com"
UA = f"neel-reference-library/1.0 (mailto:{EMAIL})"
NCBI_API_KEY = os.environ.get("NCBI_API_KEY")

ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "local" / "resolve_cache"
ABSTRACTS_DIR = ROOT / "data" / "abstracts"

DOI_RE = re.compile(r'10\.\d{4,9}/\S+')

_ncbi_last = [0.0]
_crossref_last = [0.0]


class ResolveError(Exception):
    pass


# ---------------------------------------------------------------- HTTP + cache

def _cache_path(url: str) -> pathlib.Path:
    return CACHE_DIR / f"{hashlib.md5(url.encode()).hexdigest()}.json"


def _http_get(url: str, rate_lock=None, min_interval=0.0) -> dict:
    """GET url, return parsed JSON or {'_raw': text} for XML/text. Cached forever."""
    cp = _cache_path(url)
    if cp.exists():
        try:
            return json.load(open(cp, encoding="utf-8"))
        except Exception:
            pass

    if rate_lock is not None:
        dt = time.time() - rate_lock[0]
        if dt < min_interval:
            time.sleep(min_interval - dt)
        rate_lock[0] = time.time()

    req = urllib.request.Request(url, headers={"User-Agent": UA})
    attempt = 0
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                status = r.status
                body = r.read()
            if status == 200:
                break
            if status in (429, 500, 502, 503, 504) and attempt == 1:
                time.sleep(2)
                continue
            if status == 404:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                result = {"_status": 404}
                json.dump(result, open(cp, "w", encoding="utf-8"))
                return result
            raise ResolveError(f"HTTP {status} for {url}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                result = {"_status": 404}
                json.dump(result, open(cp, "w", encoding="utf-8"))
                return result
            if e.code in (429, 500, 502, 503, 504) and attempt == 1:
                time.sleep(2)
                continue
            raise ResolveError(f"HTTP {e.code} for {url}: {e}")
        except urllib.error.URLError as e:
            if attempt == 1:
                time.sleep(2)
                continue
            raise ResolveError(f"URL error for {url}: {e}")

    text = body.decode("utf-8", errors="replace")
    try:
        result = json.loads(text)
    except Exception:
        result = {"_raw": text}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    json.dump(result, open(cp, "w", encoding="utf-8"))
    return result


def _cache_stats():
    if not CACHE_DIR.exists():
        return 0
    return len(list(CACHE_DIR.glob("*.json")))


# ---------------------------------------------------------------- Crossref

def resolve_doi(doi: str):
    """Crossref works/{doi} -> normalized record, or None on 404."""
    doi = norm_doi(doi)
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='')}?mailto={EMAIL}"
    j = _http_get(url, rate_lock=_crossref_last, min_interval=0.1)
    if j.get("_status") == 404:
        return None
    msg = j.get("message")
    if not msg:
        return None
    return _meta_from_crossref(msg)


def _cr_query(title: str, rows=3):
    params = urllib.parse.urlencode({
        "query.bibliographic": title,
        "rows": rows,
        "mailto": EMAIL,
        "select": "DOI,title,author,container-title,short-container-title,published,issued,type,resource",
    })
    url = f"https://api.crossref.org/works?{params}"
    j = _http_get(url, rate_lock=_crossref_last, min_interval=0.1)
    return (j.get("message") or {}).get("items", [])


def _authors_from_crossref(w):
    out = []
    for a in w.get("author", []) or []:
        fam = a.get("family", "")
        giv = a.get("given", "")
        if fam:
            fm = "".join(p[0] for p in re.split(r"[ .-]+", giv) if p) if giv else ""
            out.append(f"{fam} {fm}".strip())
        elif a.get("name"):
            out.append(a["name"])
    return out


def _pub_date_from_crossref(w):
    for key in ("issued", "published-online", "published-print", "published"):
        d = w.get(key, {})
        parts = d.get("date-parts", [[None]])
        if parts and parts[0] and parts[0][0]:
            p = parts[0]
            y = p[0]
            m = p[1] if len(p) > 1 else 1
            day = p[2] if len(p) > 2 else 1
            try:
                return f"{y:04d}-{m:02d}-{day:02d}"
            except Exception:
                return None
    return None


def _year_from_crossref(w):
    for key in ("issued", "published-online", "published-print", "published"):
        d = w.get(key, {})
        parts = d.get("date-parts", [[None]])
        if parts and parts[0] and parts[0][0]:
            return int(parts[0][0])
    return None


def _meta_from_crossref(w: dict) -> dict:
    title = w.get("title", [""])
    title = title[0] if isinstance(title, list) and title else (title or None)
    short = w.get("short-container-title") or []
    cont = w.get("container-title") or []
    journal = (short[0] if short else None) or (cont[0] if cont else None)
    doi = (w.get("DOI") or "").lower()
    url = None
    res = w.get("resource") or {}
    prim = res.get("primary") or {}
    url = prim.get("URL") or (f"https://doi.org/{doi}" if doi else None)
    ptype = w.get("type")
    return {
        "doi": doi or None,
        "title": title,
        "authors": _authors_from_crossref(w),
        "journal": journal,
        "year": _year_from_crossref(w),
        "pub_date": _pub_date_from_crossref(w),
        "pub_types": [ptype] if ptype else [],
        "url": url,
    }


# ---------------------------------------------------------------- NCBI

def _ncbi_params(extra: dict) -> dict:
    p = dict(extra)
    if NCBI_API_KEY:
        p["api_key"] = NCBI_API_KEY
    return p


def pmid_for_doi(doi: str):
    doi = norm_doi(doi)
    params = _ncbi_params({"db": "pubmed", "term": f"{doi}[doi]", "retmode": "json"})
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(params)
    j = _http_get(url, rate_lock=_ncbi_last, min_interval=0.35)
    ids = (j.get("esearchresult") or {}).get("idlist") or []
    return ids[0] if ids else None


def resolve_pmid(pmid: str):
    params = _ncbi_params({"db": "pubmed", "id": pmid, "retmode": "json"})
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?" + urllib.parse.urlencode(params)
    j = _http_get(url, rate_lock=_ncbi_last, min_interval=0.35)
    res = (j.get("result") or {}).get(str(pmid))
    if not res:
        return None
    doi = None
    for aid in res.get("articleids", []) or []:
        if aid.get("idtype") == "doi":
            doi = (aid.get("value") or "").lower()
    authors = [a.get("name") for a in res.get("authors", []) or [] if a.get("name")]
    pubdate = (res.get("pubdate") or "")
    ym = re.match(r"(\d{4})(?:\s+(\w+))?(?:\s+(\d+))?", pubdate)
    year = int(ym.group(1)) if ym and ym.group(1) else None
    pub_date = None
    if ym and ym.group(1):
        pub_date = ym.group(1)  # only year reliably available from esummary
    return {
        "pmid": str(pmid),
        "doi": doi,
        "title": res.get("title"),
        "authors": authors,
        "journal": res.get("source"),
        "year": year,
        "pub_date": pub_date,
        "pub_types": res.get("pubtype") or [],
    }


def fetch_abstract(pmid: str):
    params = _ncbi_params({"db": "pubmed", "id": pmid, "rettype": "xml", "retmode": "xml"})
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urllib.parse.urlencode(params)
    j = _http_get(url, rate_lock=_ncbi_last, min_interval=0.35)
    raw = j.get("_raw")
    if not raw:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    parts = []
    for node in root.iter("AbstractText"):
        label = node.attrib.get("Label")
        text = "".join(node.itertext()).strip()
        if not text:
            continue
        if label:
            parts.append(f"{label}: {text}")
        else:
            parts.append(text)
    if not parts:
        return None
    return "\n".join(parts)


# ---------------------------------------------------------------- Unpaywall

def oa_status(doi: str):
    doi = norm_doi(doi)
    url = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi, safe='')}?email={EMAIL}"
    try:
        j = _http_get(url)
    except ResolveError:
        return None
    if j.get("_status") == 404:
        return None
    if "is_oa" not in j:
        return None
    return bool(j.get("is_oa"))


# ---------------------------------------------------------------- combined resolve

_PMID_RE = re.compile(r"^\d{4,9}$")
_PUBMED_URL_RE = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)")
_DOIORG_URL_RE = re.compile(r"doi\.org/(10\.\d{4,9}/\S+)", re.I)
_PUBLISHER_DOI_RE = re.compile(r"(10\.\d{4,9}/\S+)")


def _detect_input(s: str):
    s = s.strip()
    if _PMID_RE.match(s):
        return ("pmid", s)
    m = _PUBMED_URL_RE.search(s)
    if m:
        return ("pmid", m.group(1))
    m = _DOIORG_URL_RE.search(s)
    if m:
        return ("doi", norm_doi(m.group(1)))
    if s.lower().startswith("10.") or DOI_RE.match(s):
        return ("doi", norm_doi(s))
    if s.startswith("http"):
        m = _PUBLISHER_DOI_RE.search(urllib.parse.unquote(s))
        if m:
            doi = re.split(r"[?#]", m.group(1))[0].rstrip(").,;")
            return ("doi", norm_doi(doi))
    return ("doi", norm_doi(s))


def resolve(doi_or_pmid_or_url: str) -> dict:
    kind, ident = _detect_input(doi_or_pmid_or_url)

    rec = {
        "doi": None, "pmid": None, "title": None, "authors": [], "journal": None,
        "year": None, "pub_date": None, "pub_types": [], "url": None,
        "has_abstract": False, "abstract": None, "oa": None,
    }

    cr = None
    if kind == "doi":
        cr = resolve_doi(ident)
        if cr:
            rec.update({k: v for k, v in cr.items() if v not in (None, [], "")})
            rec["doi"] = cr["doi"]
    elif kind == "pmid":
        rec["pmid"] = ident

    pmid = rec.get("pmid")
    if not pmid and rec.get("doi"):
        pmid = pmid_for_doi(rec["doi"])

    pm = None
    if pmid:
        pm = resolve_pmid(pmid)
        if pm:
            rec["pmid"] = pm["pmid"]
            if not rec.get("doi") and pm.get("doi"):
                rec["doi"] = pm["doi"]
            # prefer PubMed's ISO journal abbreviation when both exist
            if pm.get("journal"):
                rec["journal"] = pm["journal"]
            for k in ("title", "authors", "year", "pub_date", "pub_types"):
                if not rec.get(k) and pm.get(k):
                    rec[k] = pm[k]

    if rec.get("pmid"):
        abstract = fetch_abstract(rec["pmid"])
        if abstract:
            rec["abstract"] = abstract
            rec["has_abstract"] = True

    if rec.get("doi"):
        rec["oa"] = oa_status(rec["doi"])

    if not rec.get("url") and rec.get("doi"):
        rec["url"] = f"https://doi.org/{rec['doi']}"

    return rec


# ---------------------------------------------------------------- CLI

def _cmd_lookup(arg, as_json):
    rec = resolve(arg)
    if as_json:
        print(json.dumps(rec, indent=1, ensure_ascii=False))
    else:
        for k, v in rec.items():
            print(f"{k}: {v}")


def _cmd_abstracts_for_catalog(limit=None):
    by_doi = load_all()
    candidates = [
        r for r in by_doi.values()
        if r.get("verified") is True and r.get("pmid") and not r.get("has_abstract")
    ]
    if limit:
        candidates = candidates[:limit]

    by_year = {}
    fetched = absent = errored = 0
    for r in candidates:
        year_key = str(r["year"]) if r.get("year") else "unknown"
        try:
            abstract = fetch_abstract(r["pmid"])
        except ResolveError as e:
            errored += 1
            print(f"ERROR pmid={r['pmid']} doi={r.get('doi')}: {e}", file=sys.stderr)
            continue
        if abstract:
            by_year.setdefault(year_key, {})[r["doi"]] = abstract
            fetched += 1
        else:
            absent += 1

    ABSTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    for year_key, mapping in by_year.items():
        fp = ABSTRACTS_DIR / f"{year_key}.json"
        existing = {}
        if fp.exists():
            try:
                existing = json.load(open(fp, encoding="utf-8"))
            except Exception:
                existing = {}
        existing.update(mapping)
        json.dump(existing, open(fp, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    print(f"candidates={len(candidates)} fetched={fetched} absent={absent} errored={errored}")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    if "--abstracts-for-catalog" in args:
        limit = None
        if "--limit" in args:
            i = args.index("--limit")
            limit = int(args[i + 1])
        _cmd_abstracts_for_catalog(limit=limit)
        return

    as_json = "--json" in args
    positional = [a for a in args if not a.startswith("--")]
    if not positional:
        print("usage: resolve.py <doi|pmid|url> [--json]", file=sys.stderr)
        sys.exit(1)
    _cmd_lookup(positional[0], as_json)


if __name__ == "__main__":
    main()
