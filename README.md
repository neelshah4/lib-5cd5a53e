# Critical Care Reading Library

A static, link-only bibliographic catalog of critical-care literature.
It stores metadata (title, authors, journal, DOI/PMID, section, one-line
take) — never PDFs or full text.

## Layout

- `data/` — the catalog (`catalog.json`), digest records, and abstract cache
- `exports/` — generated `.ris` / `.bib` / `.csl.json` exports, overall and
  by section/digest
- `scripts/` — ingest, resolution, build, and validation code (stdlib-only
  Python 3.12)
- `tests/` — unit tests (`python3 -m unittest discover -s tests`)

## How records enter

- Weekly digest ingest (`scripts/ingest_weekly.py`)
- Monthly digest ingest (`scripts/ingest_monthly.py`)
- Manual addition by identifier: `python3 scripts/add_doi.py <doi|pmid|url>
  [--section S] [--take T]`
- The "Add article" GitHub issue form, which runs `add_doi.py` and opens a
  pull request automatically

After any addition, run `python3 scripts/build.py` to regenerate exports and
`python3 scripts/validate.py` to check the catalog before merging.

## Running locally

```
bash scripts/serve.sh
```

Then open http://localhost:8642.

## Licenses

- Code: MIT (see `LICENSE`)
- Metadata: CC BY 4.0
