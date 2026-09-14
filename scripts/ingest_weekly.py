#!/usr/bin/env python3
"""Ingest a pubmed-watcher weekly digest markdown file into the catalog.

Usage: ingest_weekly.py <path-to-digest.md> [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys

import catalog_io
import ingest_common as ic
import sections

# Headings that are known non-paper trailers -- never fed to sections.canonical()
# and never ingested. "Borderline" and "Practice-changing this week" are NOT
# trailers: their papers are ingested with an inferred section (see below).
TRAILER_PATTERNS = [
    re.compile(r"^additional candidates noted"),
    re.compile(r"^run metadata"),
    re.compile(r"^self-appraisal"),
]

# Evidence that a section body carries real paper content. Used by the
# drift guard in parse_digest() to tell "legitimately empty section"
# ("Nothing this week") apart from "papers present but unparseable".
HAS_PAPER_CONTENT_RE = re.compile(r"doi\.org/|PMID:", re.I)

BORDERLINE_RE = re.compile(r"^borderline")
PRACTICE_CHANGING_RE = re.compile(r"^practice-changing this week")

HEADER_DATE_RE = re.compile(r"[Ww]eek of (\d{4}-\d{2}-\d{2})")
# Fallback for non-standard headers (e.g. a "catch-up run, YYYY-MM-DD" title
# with no "Week of" phrasing at all): take the trailing date off the H1, or
# the end date of a "Date range: X to Y" line.
HEADER_DATE_FALLBACK_RES = [
    re.compile(r"^#.*?(\d{4}-\d{2}-\d{2})\s*$", re.M),
    re.compile(r"[Dd]ate range:\s*\d{4}-\d{2}-\d{2}\s*to\s*(\d{4}-\d{2}-\d{2})"),
]


class ParseFailure(Exception):
    def __init__(self, msg, block=""):
        super().__init__(msg)
        self.block = block


def normalize_heading(raw: str) -> str:
    s = raw.strip()
    s = re.sub(r"^[^\w]+", "", s)  # strip leading emoji/symbols like "⚡ "
    s = re.sub(r"\s*\(\d+\)\s*$", "", s)  # strip trailing "(N)" count annotation
    return s.strip()


def is_trailer(heading_norm: str) -> bool:
    low = re.sub(r"\s+", " ", heading_norm.strip().lower())
    return any(p.match(low) for p in TRAILER_PATTERNS)


def is_borderline(heading_norm: str) -> bool:
    low = re.sub(r"\s+", " ", heading_norm.strip().lower())
    return bool(BORDERLINE_RE.match(low))


def is_practice_changing(heading_norm: str) -> bool:
    low = re.sub(r"\s+", " ", heading_norm.strip().lower())
    return bool(PRACTICE_CHANGING_RE.match(low))


# A heading may carry the DOI as a trailing link, sometimes itself wrapped in
# parentheses:  '### 6. [Title](pubmed-url) ([DOI](https://doi.org/10.x/y))'
# Peeled off BEFORE the title is parsed. Without this, the lazy title regex
# matched across the whole line and produced both a title ending in '](h' and a
# DOI carrying a stray ')' -- 24 catalog rows with unresolvable DOIs and dead
# links on the site (found 2026-09-14).
_HEADING_DOI_SUFFIX = re.compile(
    r"\(?\[DOI\]\(\s*(?:https?://)?(?:dx\.)?doi\.org/([^)\s]+)\s*\)\)?\s*$",
    re.I,
)


def parse_heading_title(head: str):
    """Returns (title, doi_from_link_or_None)."""
    s = head.strip()
    # '6. ' and also '26 (Misc slot 1 of 25). ' -- the latter left the whole
    # numbering prefix inside the title (1 catalog row, found 2026-09-14).
    s = re.sub(r"^\d+\s*(?:\([^)]*\))?\.\s*", "", s)
    s = re.sub(r"^\[[^\]]*\]\s*(?!\()", "", s)
    doi = None
    m_doi = _HEADING_DOI_SUFFIX.search(s)
    if m_doi:
        doi = m_doi.group(1)
        s = s[:m_doi.start()].strip()
    m = re.match(r"^\[(.*)\]\((\S+?)\)\s*$", s)
    if m:
        title, url = m.group(1), m.group(2)
        if doi is None:
            # never let the closing ')' of the markdown link into the DOI
            du = re.search(r"doi\.org/([^)\s]+)", url)
            doi = du.group(1) if du else None
        return title, doi
    return s, doi


def extract_doi(blk: str):
    m = re.search(r"doi\.org/([^\s\)\]]+)", blk)
    if m:
        return m.group(1)
    m = re.search(r"DOI:\*{0,2}\s*\[([^\]]+)\]", blk, re.I)
    if m:
        return m.group(1)
    m = re.search(r"DOI:\*{0,2}\s*([^\s\)\]]+)", blk, re.I)
    if m:
        return m.group(1)
    return None


def extract_pmid(blk: str):
    m = re.search(r"PMID:\*{0,2}\s*\[?(\d+)\]?", blk, re.I)
    return m.group(1) if m else None


def extract_journal_year_pt(blk: str):
    m = re.search(r"\*\*([A-Za-z][^*\n]*?,\s*(?:19|20)\d{2}[^*\n]*)\*\*\s*[—-]\s*([^\n]+)", blk)
    if m:
        blob, authors = m.group(1), m.group(2).strip()
        parts = [p.strip() for p in blob.split(",")]
        journal = parts[0] if parts else None
        year = None
        for p in parts:
            if re.match(r"^(19|20)\d{2}$", p):
                year = int(p)
                break
        pts = [p for p in parts[1:] if not re.match(r"^(19|20)\d{2}$", p)]
        return journal, year, pts, authors
    m_j = re.search(r"\*\*Journal/PT:\*\*\s*([^\n]+)", blk)
    m_a = re.search(r"\*\*Authors:\*\*\s*([^\n]+)", blk)
    if m_j:
        parts = [p.strip() for p in m_j.group(1).split(",")]
        journal = parts[0] if parts else None
        year = None
        for p in parts:
            if re.match(r"^(19|20)\d{2}$", p):
                year = int(p)
                break
        pts = [p for p in parts[1:] if not re.match(r"^(19|20)\d{2}$", p)]
        authors = m_a.group(1).strip() if m_a else None
        return journal, year, pts, authors
    m_j2 = re.search(r"\*\*Journal:\*\*\s*([^\|\n]+)\|\s*\*\*Year:\*\*\s*(\d{4})\s*\|\s*\*\*Type:\*\*\s*([^\n]+)", blk)
    if m_j2:
        journal = m_j2.group(1).strip()
        year = int(m_j2.group(2))
        pts = [p.strip() for p in m_j2.group(3).split(",")]
        return journal, year, pts, None
    m_it = re.search(r"\*([A-Za-z][^*\n]*?,\s*(?:19|20)\d{2}[^*\n]*)\.\*\s*([^\n]+)", blk)
    if m_it:
        blob, rest = m_it.group(1), m_it.group(2).strip()
        parts = [p.strip() for p in blob.split(",")]
        journal = parts[0] if parts else None
        year = None
        for p in parts:
            if re.match(r"^(19|20)\d{2}$", p):
                year = int(p)
                break
        pts = [p for p in parts[1:] if not re.match(r"^(19|20)\d{2}$", p)]
        authors = rest if rest and not rest.upper().startswith("PMID") else None
        return journal, year, pts, authors
    return None, None, [], None


# --- author-shape guard -------------------------------------------------------
# Some digest layouts put a PROSE BLURB exactly where the author list lives.
# The clearest case is the "## Borderline (worth a peek)" bullet:
#   - [borderline] **Title** — *Journal, 2026, Journal Article.* <blurb...> [PMID: ..](..)
# The italic-journal branch of extract_journal_year_pt() matches that line and
# hands back the blurb as `authors`, which then gets comma-split into fake
# "authors" like 'Meta-analysis of 3 RCTs (n=431) found no significant ...'.
# Zotero rejects those server-side ("creator name is too long to sync"), which
# is how this surfaced (2026-09-14: 1 of 26 papers failed to sync, 3 catalog
# rows corrupted). The exports and the site carried the garbage too.
#
# Guard: validate the SHAPE of the whole extracted string before splitting, and
# discard it wholesale when it doesn't look like an author list. Whole-string,
# not per-token, so legitimate long collaborator names survive -- e.g.
# 'the PLeUral pressure working Group (PLUG...)' or
# 'European Resuscitation Council Guidelines 2025 Collaborator Group'.
# Losing an author list is recoverable (resolve() backfills from PubMed);
# writing prose into a creator field is not.

# Markers that never appear in an author list but are everywhere in a blurb.
_PROSE_MARKERS = re.compile(
    r"\]\("                                  # markdown link
    r"|https?://"
    r"|\bPMID\b|\bDOI\b"
    r"|%|\bn\s*=|\bvs\b|\bCI\b|\bHR\b|\bOR\b"
    r"|\bfound\b|\bshowing\b|\bshowed\b|\bassociated with\b|\bdespite\b"
    r"|\bsuggests?\b|\bsuggesting\b|\bcompared\b|\btrial\b|\bcohort\b|\bstudy\b",
    re.I,
)

# A plausible author token: 'Rose AT' / 'Mauri, Tommaso' / "O'Brien J" /
# 'van Herwerden MC' / a group name ('the ... Group', 'for the ...').
_NAME_TOKEN = re.compile(
    r"^(?:"
    r"(?:the|for|on behalf of|with)\b.*"
    r"|(?:(?:van|von|de|del|della|der|den|di|da|dos|du|la|le|ten|ter|bin|al)\s+)*"
    r"[A-ZÀ-Ý][\wÀ-ſ'’.\-]*"
    r"(?:\s+[\wÀ-ſ'’.\-]+){0,5}"
    r"(?:\s+[A-Z]{1,4})?"
    r")$",
    re.U,
)


def _strip_trailing_annotation(s: str) -> str:
    """Drop a trailing '(...)' SITE/N annotation from an author string.

    Several digest layouts append provenance after the names:
        'Manning JC, Latour JM, Draper E, et al. (Curley MAQ; 10 English PICUs, N=326)'
    Comma-splitting that whole string yields junk authors like '(Teixeira C'.
    Only annotation-shaped parentheticals are stripped -- a trailing group-name
    parenthetical such as '(PLUG-Acute Respiratory Failure section)' is kept,
    because there the parenthetical IS part of the collaborator's name.
    """
    s = s.strip()
    if not s.endswith(")"):
        return s
    depth = 0
    for i in range(len(s) - 1, -1, -1):
        if s[i] == ")":
            depth += 1
        elif s[i] == "(":
            depth -= 1
            if depth == 0:
                inner, prefix = s[i + 1:-1], s[:i].strip()
                if not prefix:
                    return s
                annotation = (
                    ";" in inner
                    or re.search(r"\bN\s*=", inner, re.I)
                    or re.search(
                        r"\d+[^)]{0,25}?(?:centers?|centres?|ICUs?|PICUs?|NICUs?|"
                        r"hospitals?|sites?|countries|institutions?|patients?|participants?)",
                        inner, re.I)
                )
                return prefix if annotation else s
    return s


def looks_like_author_list(s: str) -> bool:
    """True when `s` plausibly IS an author list rather than prose."""
    if not s or not s.strip():
        return False
    s = s.strip()
    if _PROSE_MARKERS.search(s):
        return False
    tokens = [t.strip() for t in re.split(r"[,;]|\bet al\.?", s) if t.strip()]
    if not tokens:
        return False
    named = sum(1 for t in tokens if _NAME_TOKEN.match(t))
    # A real list is overwhelmingly name-shaped. Prose that dodges every marker
    # above still fails here, because its clauses are not name-shaped.
    return named / len(tokens) >= 0.6


def parse_authors(authors_str):
    """Author string -> list of names, or None when it isn't an author list."""
    if not authors_str:
        return None
    cleaned = _strip_trailing_annotation(authors_str)
    # Some layouts run the author list straight into a PMID link and a blurb:
    #   'Titherington LM, Bottesi T, et al. PMID: [42525046](...). Narrative...'
    # Keep the prefix rather than discarding real names along with the prose.
    truncated = False
    m = _PROSE_MARKERS.search(cleaned)
    if m:
        cleaned = cleaned[:m.start()].strip().strip(",;.").strip()
        cleaned = _strip_trailing_annotation(cleaned)
        truncated = True
    # Truncating prose can leave a fragment that happens to be name-shaped
    # ('Combined clinical' from 'Combined clinical cohort (121 ...) found ...').
    # A surviving real author list still reads as a LIST, so demand that here.
    if truncated and not (re.search(r"\bet al\b", cleaned, re.I)
                          or re.search(r"[,;]", cleaned)):
        return None
    if not looks_like_author_list(cleaned):
        return None
    names = [a.strip() for a in re.split(r",|\bet al\.?", cleaned) if a.strip()]
    return names or None


def extract_take(blk: str) -> str:
    m = re.search(r"\*\*Summary:\*\*\s*([^\n]+)", blk)
    if m:
        return m.group(1).strip()
    m = re.search(r"\*\*Abstract summary:\*\*\s*([^\n]+)", blk)
    if m:
        return m.group(1).strip()
    m = re.search(
        r"\n\n([^\n].*?)\n\n\*\*(?:Practice impact|Summary \(practice\)):",
        blk, re.S,
    )
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def extract_impact(blk: str):
    m = re.search(r"\*\*(?:Practice impact|Summary \(practice\)):\*\*\s*(.+)", blk)
    if not m:
        return None
    rest = m.group(1)
    rest = rest.split("\n")[0]
    first = re.split(r"\.| —", rest)[0]
    first = first.strip().lower()
    return first or None


def split_sections(text: str):
    """Yields (heading_raw, body) for every top-level '## ' section."""
    parts = re.split(r"(?m)^## ", text)
    for part in parts[1:]:
        lines = part.split("\n", 1)
        heading = lines[0].strip()
        body = lines[1] if len(lines) > 1 else ""
        yield heading, body


def split_papers(body: str):
    """Yields (heading_line, block_text) for every '### ' paper block in body."""
    parts = re.split(r"(?m)^### ", body)
    for part in parts[1:]:
        lines = part.split("\n", 1)
        head = lines[0].strip()
        rest = lines[1] if len(lines) > 1 else ""
        yield head, "### " + head + "\n" + rest


def split_bullet_papers(body: str):
    """Yields (title, line) for every bullet/paragraph paper entry in body.

    Borderline and Practice-changing sections are rendered as one-line entries
    by the pubmed-watcher spec, not as '### ' blocks, and three shapes appear
    across the real archive:

        - [borderline] **Title** — *J, Y, PT.* [PMID: N](..) · [DOI](..)
        **[borderline]** [Title](url) — *J*, Y, ... https://doi.org/..
        - **[⚡ practice-changing] Title** — *J, Y, PT.* ...

    So key off "the line carries a DOI or PMID" rather than off any one
    marker. Historically these sections had no reader at all, which silently
    dropped every borderline paper from the catalog.
    """
    for line in body.split("\n"):
        line = line.strip()
        if not HAS_PAPER_CONTENT_RE.search(line):
            continue
        if not (line.startswith("- ") or line.startswith("**[")):
            continue
        title = None
        m = re.search(r"\*\*(.+?)\*\*", line)
        if m:
            cand = re.sub(r"^\[[^\]]*\]\s*", "", m.group(1)).strip()
            # A bare marker like "**[borderline]**" leaves nothing behind.
            if cand:
                title = cand
        if not title:
            m = re.search(r"\[([^\]]{15,})\]\(", line)  # first substantive md link
            if m:
                title = m.group(1).strip()
        if not title:
            continue
        yield title, line


def _build_paper(head, blk, section, section_inferred, tags, path):
    paper_title, doi_from_link = parse_heading_title(head)
    doi = doi_from_link or extract_doi(blk)
    pmid = extract_pmid(blk)
    if not doi and not pmid:
        raise ParseFailure(
            f"{path}: paper block has no resolvable DOI or PMID (title={paper_title!r})",
            blk,
        )
    journal, year, pub_types, authors_str = extract_journal_year_pt(blk)
    authors = parse_authors(authors_str)
    take = extract_take(blk)
    impact = extract_impact(blk)
    rec = {
        "doi": doi,
        "pmid": pmid,
        "title": paper_title,
        "journal": journal,
        "year": year,
        "pub_types": pub_types or None,
        "authors": authors,
        "section": section,
        "take": take,
        "impact": impact,
        "source": "weekly",
        "_section_inferred": section_inferred,
    }
    if tags:
        rec["tags"] = list(tags)
    return rec


def parse_digest(text: str, path: str):
    m = HEADER_DATE_RE.search(text)
    if not m:
        for rx in HEADER_DATE_FALLBACK_RES:
            m = rx.search(text)
            if m:
                break
    if not m:
        raise ParseFailure(f"{path}: could not find 'Week of YYYY-MM-DD' in header", text[:300])
    date = m.group(1)
    digest_id = ic.iso_week_id(date)
    title = f"Weekly digest {date}"

    papers = []
    for heading_raw, body in split_sections(text):
        heading_norm = normalize_heading(heading_raw)
        if is_trailer(heading_norm):
            continue

        borderline = is_borderline(heading_norm)
        practice_changing = is_practice_changing(heading_norm)

        if borderline or practice_changing:
            section = None  # resolved per-paper via infer_section below
        else:
            try:
                section = sections.canonical(heading_norm)
            except ValueError as e:
                raise ParseFailure(f"{path}: unrecognized section heading {heading_raw!r}: {e}",
                                    heading_raw)

        n_before = len(papers)
        # Borderline / Practice-changing are bullet-rendered by spec; the seven
        # real sections use '### ' blocks. Fall back rather than choosing up
        # front, so a section rendered either way still parses.
        blocks = list(split_papers(body))
        if not blocks and (borderline or practice_changing):
            blocks = list(split_bullet_papers(body))
        for head, blk in blocks:
            if borderline or practice_changing:
                # need title/journal before we can infer a section
                paper_title, _ = parse_heading_title(head)
                journal, *_rest = extract_journal_year_pt(blk)
                inferred = ic.infer_section(paper_title, journal)
                tags = ["borderline"] if borderline else None
                rec = _build_paper(head, blk, inferred, True, tags, path)
            else:
                rec = _build_paper(head, blk, section, False, None, path)
            papers.append(rec)

        # Drift guard. A section that carries paper-like content (a DOI or a
        # PMID) but yields no '### ' block means the generator rendered real
        # papers in a shape this parser cannot see. Ingesting that silently
        # would report success while importing nothing, which is strictly
        # worse than failing -- so fail loudly and name the section.
        if len(papers) == n_before and HAS_PAPER_CONTENT_RE.search(body):
            raise ParseFailure(
                f"{path}: section {heading_raw!r} contains paper content "
                f"(DOI/PMID) but no parseable '### ' paper block -- the digest "
                f"layout has drifted from the canonical format",
                body.strip()[:600],
            )

    # A digest that parses to zero papers is never legitimate.
    if not papers:
        raise ParseFailure(
            f"{path}: parsed 0 papers -- digest layout has drifted from the "
            f"canonical format (expected '## <Section>' blocks each containing "
            f"'### N. [Title](doi-url)' paper blocks)",
            text[:600],
        )
    return digest_id, date, title, papers


def run(path: str, dry_run: bool, resolve_fn=None):
    text = open(path, encoding="utf-8").read()
    digest_id, date, title, papers = parse_digest(text, path)

    if resolve_fn is None and papers:
        from resolve import resolve as resolve_fn  # lazy import

    records = catalog_io.load_all()
    digests = catalog_io.load_digests()

    n_added = 0
    n_merged = 0
    n_resolve_failed = 0
    dois = []
    for p in papers:
        identifier = p.get("doi") or p.get("pmid")
        try:
            meta = resolve_fn(identifier)
        except Exception as e:
            # A resolve failure is never fatal (the digest text may already
            # carry the DOI), but it must be visible: silently swallowing it
            # leaves a record permanently missing pmid/year/abstract.
            print(f"WARN: resolve failed for {identifier!r}: {e}", file=sys.stderr)
            n_resolve_failed += 1
            meta = {}
        if not p.get("doi"):
            p["doi"] = meta.get("doi")
        if not p["doi"]:
            if dry_run:
                print(f"WARN: could not resolve a DOI for PMID {p.get('pmid')!r} "
                      f"({p.get('title')!r}) -- skipped in dry-run", file=sys.stderr)
                continue
            raise ParseFailure(
                f"{path}: could not resolve a DOI for PMID {p.get('pmid')!r}",
                str(p),
            )
        for k in ("pmid", "title", "authors", "journal", "year", "pub_date", "pub_types", "url"):
            if not p.get(k) and meta.get(k):
                p[k] = meta[k]
        abstract = meta.get("abstract")
        year_for_abstract = p.get("year") or (p.get("pub_date") or "")[:4] or "unknown"

        doi, created = ic.upsert(records, p, digest_id)
        dois.append(doi)
        if created:
            n_added += 1
            if abstract and not dry_run:
                ic.store_abstract(doi, year_for_abstract, abstract)
        else:
            n_merged += 1

    digests = ic.register_digest(digests, digest_id, "weekly", date, title, dois, path)

    if not dry_run:
        ic.strip_transient_fields(records)
        catalog_io.save_all(records)
        catalog_io.save_digests(digests)

    print(f"ingest: {digest_id} +{n_added} added ~{n_merged} merged"
          + (f" !{n_resolve_failed} resolve-failed" if n_resolve_failed else ""))
    return n_added, n_merged, digest_id


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    try:
        run(args.path, args.dry_run)
    except ParseFailure as e:
        print(f"PARSE ERROR: {e}", file=sys.stderr)
        print("---- offending block ----", file=sys.stderr)
        print(e.block, file=sys.stderr)
        sys.exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
