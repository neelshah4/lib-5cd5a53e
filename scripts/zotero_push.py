#!/usr/bin/env python3
"""Push catalog records into Zotero via the Web API v3 (stdlib urllib only).

Never prints, logs, or writes the API key. Never deletes anything, never
modifies title/authors/DOI/date of an existing Zotero item.
"""
import argparse
import datetime
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import catalog_io

API_BASE = "https://api.zotero.org"
SECTION_NAMES = ["ECMO", "Respiratory/ARDS", "Shock & Sepsis",
                  "Neurocritical Care", "Cardiac CC", "Renal", "Misc"]

WEEKLY_RE = re.compile(r"^(\d{4})-W(\d{2})$")
MONTHLY_RE = re.compile(r"^(\d{4})-(\d{2})$")


# ---------------------------------------------------------------- credentials
def load_credentials():
    key = os.environ.get("ZOTERO_API_KEY")
    lib_id = os.environ.get("ZOTERO_LIBRARY_ID")
    lib_type = os.environ.get("ZOTERO_LIBRARY_TYPE")
    if not key or not lib_id:
        cfg_path = pathlib.Path.home() / ".claude.json"
        try:
            cfg = json.load(open(cfg_path, encoding="utf-8"))
        except Exception as e:
            raise SystemExit(f"could not read zotero credentials: {e}")
        env = cfg.get("mcpServers", {}).get("zotero", {}).get("env", {})
        key = key or env.get("ZOTERO_API_KEY")
        lib_id = lib_id or env.get("ZOTERO_LIBRARY_ID")
        lib_type = lib_type or env.get("ZOTERO_LIBRARY_TYPE")
    lib_type = lib_type or "user"
    if not key or not lib_id:
        raise SystemExit("zotero credentials not found (env or ~/.claude.json); key redacted regardless")
    return key, str(lib_id), lib_type


# ---------------------------------------------------------------- HTTP layer
def api_request(method, path, key, params=None, body=None):
    """One Zotero Web API call. Honors Backoff / Retry-After by sleeping.
    Returns (status, parsed_json_or_None, headers_dict)."""
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Zotero-API-Version": "3", "Zotero-API-Key": key}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    for _ in range(6):
        req = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            status = resp.status
            raw = resp.read()
            hdrs = resp.headers
        except urllib.error.HTTPError as e:
            status = e.code
            raw = e.read()
            hdrs = e.headers

        get_h = (lambda name: hdrs.get(name)) if hasattr(hdrs, "get") else (lambda name: None)
        wait = get_h("Backoff") or get_h("Retry-After")
        should_retry = status in (429, 503) and wait
        if wait:
            try:
                time.sleep(float(wait))
            except (TypeError, ValueError):
                pass
        if should_retry:
            continue

        try:
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = raw.decode("utf-8", "replace") if raw else None
        hdrs_dict = dict(hdrs.items()) if hasattr(hdrs, "items") else dict(hdrs or {})
        return status, parsed, hdrs_dict

    raise RuntimeError(f"zotero API retry exhausted for {method} {path}")


# ---------------------------------------------------------------- digest id parsing
def digest_kind(digest_id):
    if digest_id == "manual":
        return "manual"
    if WEEKLY_RE.match(digest_id):
        return "weekly"
    if MONTHLY_RE.match(digest_id):
        return "monthly"
    raise ValueError(f"unrecognized digest id: {digest_id!r}")


def weekly_month_bucket(digest_id):
    m = WEEKLY_RE.match(digest_id)
    y, w = int(m.group(1)), int(m.group(2))
    thursday = datetime.date.fromisocalendar(y, w, 4)
    return f"{thursday.year:04d}-{thursday.month:02d}"


def digest_collection_path(digest_id, kind):
    """Returns (parent_name, child_name) for a digest id."""
    if kind == "weekly":
        return "Weekly", weekly_month_bucket(digest_id)
    if kind == "monthly":
        return "Digests", digest_id
    if kind == "manual":
        return "Digests", "Manual"
    raise ValueError(f"unrecognized digest kind: {kind!r}")


def tag_for(digest_id, kind):
    if kind == "weekly":
        return f"weekly:{digest_id}"
    if kind == "monthly":
        return f"digest:{digest_id}"
    if kind == "manual":
        return "manual"
    raise ValueError(f"unrecognized digest kind: {kind!r}")


# ---------------------------------------------------------------- Zotero context
class ZoteroCtx:
    def __init__(self, key, lib_id, lib_type, dry_run=False):
        self.key = key
        self.lib_id = lib_id
        self.lib_type = lib_type
        self.dry_run = dry_run
        self._collections = None  # list of {"key":..,"data":{"name":..,"parentCollection":..}}
        self._collection_cache = {}  # (name, parent_key_or_None) -> key

    def base_path(self, suffix=""):
        return f"/{self.lib_type}s/{self.lib_id}{suffix}"

    def all_collections(self):
        if self._collections is None:
            cols = []
            start = 0
            while True:
                status, data, hdrs = api_request(
                    "GET", self.base_path("/collections"), self.key,
                    params={"limit": 100, "start": start})
                if status >= 400:
                    raise RuntimeError(f"zotero collections GET failed: {status} {data}")
                if not data:
                    break
                cols.extend(data)
                if len(data) < 100:
                    break
                start += 100
            self._collections = cols
        return self._collections

    def find_collection(self, name, parent_key):
        parent_norm = parent_key or False
        for c in self.all_collections():
            d = c.get("data", {})
            if d.get("name") == name and (d.get("parentCollection") or False) == parent_norm:
                return c["key"]
        return None

    def ensure_collection(self, name, parent_key=None):
        cache_key = (name, parent_key or None)
        if cache_key in self._collection_cache:
            return self._collection_cache[cache_key]
        existing = self.find_collection(name, parent_key)
        if existing:
            self._collection_cache[cache_key] = existing
            return existing
        if self.dry_run:
            fake = f"DRYRUN:{name}"
            self._collection_cache[cache_key] = fake
            print(f"[dry-run] would create collection {name!r} (parent={parent_key!r})")
            return fake
        status, data, hdrs = api_request(
            "POST", self.base_path("/collections"), self.key,
            body=[{"name": name, "parentCollection": parent_key or False}])
        if status >= 400 or not data or data.get("failed"):
            raise RuntimeError(f"zotero collection create failed for {name!r}: {status} {data}")
        newkey = data["successful"]["0"]["key"]
        self._collections.append({"key": newkey, "data": {"name": name, "parentCollection": parent_key or False}})
        self._collection_cache[cache_key] = newkey
        return newkey

    def section_collection(self, section):
        section = section or "Misc"
        if section not in SECTION_NAMES:
            section = "Misc"
        return self.ensure_collection(section, None)

    def digest_collection(self, digest_id, kind):
        # Flat, top-level collections matching the convention already in the live
        # library (Digests-2026-07, Digests-2026-09): "Weekly-YYYY-MM", "Digests-YYYY-MM", "Digests-Manual".
        parent_name, child_name = digest_collection_path(digest_id, kind)
        return self.ensure_collection(f"{parent_name}-{child_name}", None)

    def search_by_doi(self, doi):
        status, data, hdrs = api_request(
            "GET", self.base_path("/items"), self.key,
            params={"q": doi, "qmode": "everything"})
        if status >= 400:
            raise RuntimeError(f"zotero item search failed for {doi}: {status} {data}")
        for item in data or []:
            item_doi = catalog_io.norm_doi((item.get("data") or {}).get("DOI") or "")
            if item_doi == doi:
                return item["key"], item.get("version")
        return None, None

    def get_item(self, item_key):
        status, data, hdrs = api_request("GET", self.base_path(f"/items/{item_key}"), self.key)
        if status >= 400:
            raise RuntimeError(f"zotero item GET failed for {item_key}: {status} {data}")
        return data

    def patch_collections_tags(self, item_key, add_collection_keys, add_tag):
        """Ensure add_collection_keys and add_tag are present on the item.
        Never touches title/authors/DOI/date. Returns True if a write happened."""
        item = self.get_item(item_key)
        idata = item.get("data", {})
        version = item.get("version")
        cur_collections = set(idata.get("collections") or [])
        cur_tags = {t.get("tag") for t in (idata.get("tags") or [])}

        need_collections = [c for c in add_collection_keys if c not in cur_collections]
        need_tag = add_tag and add_tag not in cur_tags

        if not need_collections and not need_tag:
            return False

        if self.dry_run:
            print(f"[dry-run] would PATCH item {item_key}: +collections {need_collections} +tag {add_tag if need_tag else None}")
            return True

        new_collections = sorted(cur_collections | set(add_collection_keys))
        new_tags = list(idata.get("tags") or [])
        if need_tag:
            new_tags.append({"tag": add_tag})

        status, data, hdrs = api_request(
            "PATCH", self.base_path(f"/items/{item_key}"), self.key,
            body={"collections": new_collections, "tags": new_tags, "version": version})
        if status not in (204, 200):
            raise RuntimeError(f"zotero item PATCH failed for {item_key}: {status} {data}")
        return True

    def create_items(self, bodies):
        """POST up to 50 item templates at a time. Returns list of (index, key_or_None, error_or_None)."""
        results = [None] * len(bodies)
        for start in range(0, len(bodies), 50):
            chunk = bodies[start:start + 50]
            if self.dry_run:
                for i, b in enumerate(chunk):
                    print(f"[dry-run] would POST new item: DOI={b.get('DOI')!r} title={b.get('title')!r}")
                    results[start + i] = (None, None)
                continue
            status, data, hdrs = api_request("POST", self.base_path("/items"), self.key, body=chunk)
            if status >= 400 or data is None:
                raise RuntimeError(f"zotero item create failed: {status} {data}")
            successful = data.get("successful", {})
            failed = data.get("failed", {})
            for i in range(len(chunk)):
                idx = str(i)
                if idx in successful:
                    results[start + i] = (successful[idx]["key"], None)
                elif idx in failed:
                    results[start + i] = (None, failed[idx])
                else:
                    results[start + i] = (None, {"message": "no result returned"})
        return results


# ---------------------------------------------------------------- item body building
def ris_authors(rec):
    return [a.strip() for a in (rec.get("authors") or []) if a and a.strip()]


def build_item_body(rec, collections, tag, abstract=None):
    creators = []
    for a in ris_authors(rec):
        if "," in a:
            last, first = a.split(",", 1)
            creators.append({"creatorType": "author", "lastName": last.strip(), "firstName": first.strip()})
        else:
            creators.append({"creatorType": "author", "lastName": a, "firstName": ""})
    extra = []
    if rec.get("pmid"):
        extra.append(f"PMID: {rec['pmid']}")
    body = {
        "itemType": "journalArticle",
        "title": rec.get("title") or "",
        "creators": creators,
        "publicationTitle": rec.get("journal") or "",
        "date": str(rec.get("pub_date") or rec.get("year") or ""),
        "DOI": rec.get("doi") or "",
        "url": rec.get("url") or "",
        "abstractNote": abstract or "",
        "extra": "\n".join(extra),
        "collections": collections,
    }
    if tag:
        body["tags"] = [{"tag": tag}]
    return body


_ABSTRACT_CACHE = {}


def load_abstract(doi, year):
    if not year:
        return None
    year = str(year)
    if year not in _ABSTRACT_CACHE:
        path = ROOT / "data" / "abstracts" / f"{year}.json"
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception:
            data = {}
        _ABSTRACT_CACHE[year] = data
    data = _ABSTRACT_CACHE[year]
    if isinstance(data, dict):
        for k, v in data.items():
            if catalog_io.norm_doi(k) == doi:
                return v if isinstance(v, str) else v.get("abstract")
    elif isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and catalog_io.norm_doi(entry.get("doi") or "") == doi:
                return entry.get("abstract")
    return None


# ---------------------------------------------------------------- state file
def load_state(path):
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    return json.load(open(p, encoding="utf-8"))


def save_state(path, state):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    json.dump(state, open(p, "w", encoding="utf-8"), indent=1, sort_keys=True)


# ---------------------------------------------------------------- main push logic
def select_records(catalog, digest_id, manual, doi):
    # The held partition (local/unverified.json, verified is not True) must
    # never reach the live Zotero library: this script never deletes, so a
    # wrongly-pushed record cannot be walked back from here. --doi stays an
    # explicit single-record operator override and is not filtered.
    if digest_id:
        return [r for r in catalog.values()
                if digest_id in (r.get("digests") or []) and r.get("verified") is True]
    if manual:
        return [r for r in catalog.values()
                if r.get("source") == "manual" and r.get("verified") is True]
    if doi:
        d = catalog_io.norm_doi(doi)
        rec = catalog.get(d)
        if rec:
            return [rec]
        return [{"doi": d, "title": None, "authors": [], "journal": None, "year": None,
                 "pub_date": None, "pmid": None, "url": None, "section": "Misc"}]
    return []


def run(args):
    key, lib_id, lib_type = load_credentials()
    ctx = ZoteroCtx(key, lib_id, lib_type, dry_run=args.dry_run)
    state = load_state(args.state)
    catalog = catalog_io.load_all()

    digest_id = args.digest
    kind = digest_kind(digest_id) if digest_id else None
    manual_mode = args.manual

    if manual_mode:
        digest_id = "manual"
        kind = "manual"

    records = select_records(catalog, args.digest, manual_mode, args.doi)

    if manual_mode:
        records = [r for r in records if catalog_io.norm_doi(r["doi"]) not in state]

    if args.limit is not None:
        records = records[: args.limit]

    n_created = n_linked = n_updated = n_skipped = 0
    failures = []
    pending_create = []  # (doi, body, section, digest_id, kind)

    for rec in records:
        doi = catalog_io.norm_doi(rec["doi"])
        tag = tag_for(digest_id, kind) if digest_id else None
        collections = [ctx.section_collection(rec.get("section") or "Misc")]
        if digest_id:
            collections.append(ctx.digest_collection(digest_id, kind))

        if doi in state:
            entry = state[doi]
            pushed = entry.setdefault("digests", [])
            if digest_id and digest_id in pushed:
                n_skipped += 1
                continue
            wrote = ctx.patch_collections_tags(entry["key"], collections, tag)
            if digest_id and digest_id not in pushed:
                pushed.append(digest_id)
            if wrote:
                n_updated += 1
            else:
                n_skipped += 1
            continue

        found_key, _version = ctx.search_by_doi(doi)
        if found_key:
            ctx.patch_collections_tags(found_key, collections, tag)
            state[doi] = {"key": found_key, "digests": [digest_id] if digest_id else []}
            n_linked += 1
            continue

        abstract = load_abstract(doi, rec.get("year"))
        body = build_item_body(rec, collections, tag, abstract=abstract)
        pending_create.append((doi, body, digest_id))

    if pending_create:
        bodies = [b for _, b, _ in pending_create]
        results = ctx.create_items(bodies)
        for (doi, body, dg), (key_out, err) in zip(pending_create, results):
            if err:
                failures.append((doi, err))
                continue
            if args.dry_run:
                n_created += 1
                continue
            state[doi] = {"key": key_out, "digests": [dg] if dg else []}
            n_created += 1

    if not args.dry_run:
        save_state(args.state, state)

    print(f"zotero: {n_created} created · {n_linked} linked-existing · {n_updated} updated · {n_skipped} skipped")
    if failures:
        for doi, err in failures:
            msg = err.get("message") if isinstance(err, dict) else str(err)
            print(f"FAILED {doi}: {msg}", file=sys.stderr)
        return 1
    return 0


def smoke_test_collections(args):
    key, lib_id, lib_type = load_credentials()
    ctx = ZoteroCtx(key, lib_id, lib_type, dry_run=True)
    cols = ctx.all_collections()
    print(f"zotero collections ({len(cols)}):")
    for c in cols:
        d = c.get("data", {})
        print(f"  - {d.get('name')!r} (parent={d.get('parentCollection')!r})")


def main():
    ap = argparse.ArgumentParser(description="Push catalog records into Zotero via the Web API v3.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--digest", help="push every catalog record whose digests contains this id")
    g.add_argument("--manual", action="store_true", help="push records with source=manual not yet in state")
    g.add_argument("--doi", help="push a single record by DOI")
    g.add_argument("--list-collections", action="store_true", help="smoke test: list existing collections")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--state", default=str(catalog_io.ZSTATE))
    args = ap.parse_args()

    if args.list_collections:
        smoke_test_collections(args)
        return 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
