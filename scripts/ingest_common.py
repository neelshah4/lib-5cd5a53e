#!/usr/bin/env python3
"""Shared upsert / digest-registration / abstract-storage logic for the three
ingest scripts (ingest_weekly, ingest_monthly, add_doi).

Deliberately imports catalog_io as a module (not `from catalog_io import X`)
so that tests can monkeypatch catalog_io.PUB / .LOC / .DIGESTS and have every
helper in this file pick up the patched paths automatically.
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib

import catalog_io


class IngestError(Exception):
    """Raised on any malformed input. Callers should print and exit non-zero."""


def norm_doi(doi: str) -> str:
    return catalog_io.norm_doi(doi)


def today_iso() -> str:
    return _dt.date.today().isoformat()


def iso_week_id(date) -> str:
    """date: a datetime.date, or an ISO 'YYYY-MM-DD' string."""
    if isinstance(date, str):
        y, m, d = (int(x) for x in date.split("-"))
        date = _dt.date(y, m, d)
    iso_year, iso_week, _ = date.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def month_id(date) -> str:
    """date: a datetime.date, or an ISO 'YYYY-MM-DD' string, or 'YYYY-MM'."""
    if isinstance(date, str):
        parts = date.split("-")
        return f"{parts[0]}-{parts[1]}"
    return f"{date.year:04d}-{date.month:02d}"


def commit_message(digest_id: str, n_added: int, n_merged: int) -> str:
    return f"ingest: {digest_id} (+{n_added}, ~{n_merged})"


INFERRED_TAG = "section-inferred"


def upsert(records: dict, rec: dict, digest_id: str) -> tuple[str, bool]:
    """Insert or merge `rec` into `records` (doi -> record dict), keyed by
    normalized DOI. Returns (doi, created)."""
    doi = norm_doi(rec.get("doi", ""))
    if not doi:
        raise IngestError(f"record has no doi to upsert: {rec!r}")
    rec = dict(rec)
    rec["doi"] = doi

    # Transient, never persisted: whether rec["section"] was inferred by
    # keyword-matching (Borderline/Practice-changing blocks) rather than
    # taken from a real "## <Section>" heading.
    section_inferred = rec.pop("_section_inferred", False)

    if doi not in records:
        new = catalog_io.new_record(**rec)
        new.setdefault("digests", [])
        if digest_id not in new["digests"]:
            new["digests"].append(digest_id)
        new.setdefault("first_seen", today_iso())
        new["first_seen"] = new.get("first_seen") or today_iso()
        # Records created here come from a resolved DOI, not a fuzzy match.
        new["verified"] = True
        new["confidence"] = new.get("confidence")
        if new["confidence"] is None:
            new["confidence"] = 1.0
        new["_section_inferred"] = section_inferred
        if section_inferred:
            # Persisted marker (a tag, not a schema key) so a later run with a real
            # "## <Section>" heading can still upgrade the section.
            tags = list(new.get("tags") or [])
            if INFERRED_TAG not in tags: tags.append(INFERRED_TAG)
            new["tags"] = tags
        records[doi] = new
        return doi, True

    existing = records[doi]
    # digests: append if missing
    digests = existing.get("digests") or []
    if digest_id not in digests:
        digests = digests + [digest_id]
    existing["digests"] = digests

    # take: only set if existing take is empty
    if not existing.get("take"):
        new_take = rec.get("take")
        if new_take:
            existing["take"] = new_take

    # section: a real "## <Section>" heading always wins over an inferred
    # one, regardless of which was upserted first. An inferred section only
    # fills an empty slot.
    if rec.get("section"):
        if not existing.get("section"):
            existing["section"] = rec["section"]
            existing["_section_inferred"] = section_inferred
        elif (existing.get("_section_inferred") or INFERRED_TAG in (existing.get("tags") or [])) and not section_inferred:
            existing["section"] = rec["section"]
            existing["_section_inferred"] = False
            existing["tags"] = [t for t in (existing.get("tags") or []) if t != INFERRED_TAG]

    # fill any empty/None field from the new data, except the ones with
    # custom handling above/below (source, take, digests, verified, confidence, section)
    skip = {"source", "take", "digests", "verified", "confidence", "doi", "section"}
    for k in catalog_io.KEYS:
        if k in skip:
            continue
        if not existing.get(k) and rec.get(k):
            existing[k] = rec[k]

    # verified never moves downward
    if rec.get("verified") and not existing.get("verified"):
        existing["verified"] = True

    # confidence: fill only if missing
    if existing.get("confidence") is None and rec.get("confidence") is not None:
        existing["confidence"] = rec["confidence"]

    # source never changes on merge
    existing.setdefault("source", rec.get("source"))

    records[doi] = existing
    return doi, False


def strip_transient_fields(records: dict) -> None:
    """Remove in-memory-only bookkeeping keys (e.g. upsert()'s
    "_section_inferred") before persisting `records` via catalog_io.save_all().
    Every ingest script must call this immediately before save_all()."""
    for r in records.values():
        r.pop("_section_inferred", None)


def register_digest(digests: list, id: str, kind: str, date: str, title: str,
                     dois: list, source_file: str) -> list:
    """Idempotent: replaces any existing entry with the same id."""
    prev = next((d for d in digests if d.get("id") == id), None)
    entry = {
        "id": id,
        "kind": kind,
        "date": date,
        "title": title,
        "dois": sorted(set(norm_doi(d) for d in dois)),
        # preserve build.py's computed counts so a re-ingest is byte-identical
        "counts": (prev or {}).get("counts", {}),
        "source_file": source_file,
    }
    out = [d for d in digests if d.get("id") != id]
    out.append(entry)
    return out


def abstracts_path(year) -> pathlib.Path:
    return catalog_io.PUB.parent / "abstracts" / f"{year}.json"


def store_abstract(doi: str, year, text: str) -> None:
    if not text:
        return
    p = abstracts_path(year)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if p.exists():
        data = json.load(open(p, encoding="utf-8"))
    data[norm_doi(doi)] = text
    json.dump(data, open(p, "w", encoding="utf-8"), indent=1, ensure_ascii=False)


SECTION_KEYWORD_MAP = [
    ("ECMO", ("ecmo", "extracorporeal")),
    ("Respiratory/ARDS", ("ards", "ventilat", "oxygen", "respiratory", "pulmonary")),
    ("Shock & Sepsis", ("sepsis", "shock", "vasopress")),
    ("Neurocritical Care", ("brain", "neuro", "seizure")),
    ("Cardiac CC", ("cardiac", "arrest", "heart")),
    ("Renal", ("renal", "kidney", "crrt")),
]


def infer_section(title: str, journal: str | None = None) -> str:
    t = f"{title or ''} {journal or ''}".lower()
    for section, keywords in SECTION_KEYWORD_MAP:
        if any(kw in t for kw in keywords):
            return section
    return "Misc"


MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
