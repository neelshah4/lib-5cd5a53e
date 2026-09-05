#!/usr/bin/env python3
"""Tests for the ingest pipeline. Runs entirely against a temp copy of the
catalog data -- never touches the real data/ or local/ directories.

Run with: python3 -m unittest tests.test_ingest -v
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
REAL_DIGESTS_DIR = Path.home() / ".claude" / "digests"

sys.path.insert(0, str(SCRIPTS))

import catalog_io  # noqa: E402
import ingest_common as ic  # noqa: E402
import ingest_weekly  # noqa: E402
import ingest_monthly  # noqa: E402
import add_doi  # noqa: E402
import sections  # noqa: E402

# ---------------------------------------------------------------------------
# Canned resolve() stub metadata, keyed by normalized DOI.
# ---------------------------------------------------------------------------
CANNED = {
    "10.1097/mat.0000000000002822": dict(
        doi="10.1097/MAT.0000000000002822", pmid="42638325",
        title="The Unknown Others of Neonatal Respiratory Extracorporeal Membrane Oxygenation",
        authors=["Rose AT", "Lakhani A", "Conroy S"], journal="ASAIO J", year=2026,
        pub_date="2026-08-20", pub_types=["Journal Article"],
        url="https://doi.org/10.1097/MAT.0000000000002822",
        abstract="Fixture abstract for the neonatal ECMO reclassification study.",
    ),
    "10.1186/s13054-026-06213-4": dict(
        doi="10.1186/s13054-026-06213-4", pmid="42661221",
        title="Left ventricular unloading during VA-ECMO for refractory cardiogenic shock",
        authors=["Dettling A", "Saura O"], journal="Crit Care", year=2026,
        pub_date="2026-08-21", pub_types=["Journal Article", "Multicenter Study"],
        url="https://doi.org/10.1186/s13054-026-06213-4",
        abstract="Fixture abstract for the LV unloading target-trial emulation.",
    ),
    "10.1002/resp.70304": dict(
        doi="10.1002/resp.70304", pmid="42644455",
        title="Comparison of Efficacy Between NIV and HFNC in Acute Pulmonary Edema",
        authors=["Huang Q", "Niu J"], journal="Respirology", year=2026,
        pub_date="2026-08-22", pub_types=["Journal Article"],
        url="https://doi.org/10.1002/resp.70304",
        abstract="Fixture abstract for the NIV vs HFNC cohort.",
    ),
    "10.1007/s00134-026-08595-z": dict(
        doi="10.1007/s00134-026-08595-z", pmid="42658259",
        title="Balancing lung and brain: physiological strategies for ARDS management in acute brain injury",
        authors=["Robba C", "Romero-Garcia N"], journal="Intensive Care Med", year=2026,
        pub_date="2026-08-24", pub_types=["Journal Article", "Review"],
        url="https://doi.org/10.1007/s00134-026-08595-z",
        abstract="Fixture abstract for the lung-brain ARDS review.",
    ),
    "42584187": dict(
        doi="10.9999/fixture.pmidonly.1", pmid="42584187",
        title="The Association Between Obesity and ICU Duration in Pediatric Critical Asthma",
        authors=["Nobody N"], journal="Pediatr Crit Care Med", year=2026,
        pub_date="2026-08-01", pub_types=["Journal Article"],
        url="https://doi.org/10.9999/fixture.pmidonly.1",
        abstract="Fixture abstract for the PMID-only (no DOI in markdown) block.",
    ),
    "10.9999/fixture.borderline.1": dict(
        doi="10.9999/fixture.borderline.1", pmid="90000001",
        title="A fixture-only borderline paper that should NOT be ingested",
        authors=["Nobody N"], journal="Fixture J", year=2099,
        pub_date="2099-01-01", pub_types=["Journal Article"],
        url="https://doi.org/10.9999/fixture.borderline.1", abstract=None,
    ),
    "10.9999/fixture.badheading.1": dict(
        doi="10.9999/fixture.badheading.1", pmid="90000002",
        title="A fixture paper under an unrecognized heading",
        authors=["Nobody N"], journal="Fixture Journal", year=2099,
        pub_date="2099-01-01", pub_types=["Journal Article"],
        url="https://doi.org/10.9999/fixture.badheading.1", abstract=None,
    ),
}


def stub_resolve(identifier: str) -> dict:
    key = catalog_io.norm_doi(identifier)
    if key in CANNED:
        return dict(CANNED[key])
    return {}


def file_hashes(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class TempCatalogTestCase(unittest.TestCase):
    """Base class: copies data/ and local/ into a tempdir and monkeypatches
    catalog_io's module-level path constants to point there."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccrl_test_")
        self.tmp_data = Path(self.tmp) / "data"
        self.tmp_local = Path(self.tmp) / "local"
        shutil.copytree(REPO / "data", self.tmp_data)
        shutil.copytree(REPO / "local", self.tmp_local)

        self._orig_pub = catalog_io.PUB
        self._orig_loc = catalog_io.LOC
        self._orig_digests = catalog_io.DIGESTS
        self._orig_zstate = catalog_io.ZSTATE

        catalog_io.PUB = self.tmp_data / "catalog.json"
        catalog_io.LOC = self.tmp_local / "unverified.json"
        catalog_io.DIGESTS = self.tmp_data / "digests.json"
        catalog_io.ZSTATE = self.tmp_data / "zotero_state.json"

    def tearDown(self):
        catalog_io.PUB = self._orig_pub
        catalog_io.LOC = self._orig_loc
        catalog_io.DIGESTS = self._orig_digests
        catalog_io.ZSTATE = self._orig_zstate
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestWeeklyIngest(TempCatalogTestCase):
    def test_weekly_basic_ingest(self):
        n_added, n_merged, digest_id = ingest_weekly.run(
            str(FIXTURES / "pubmed-2099-01-04.md"), dry_run=False, resolve_fn=stub_resolve
        )
        self.assertEqual(n_added, 5)
        self.assertEqual(n_merged, 0)
        self.assertEqual(digest_id, "2099-W01")

        digests = catalog_io.load_digests()
        entry = next(d for d in digests if d["id"] == "2099-W01")
        self.assertEqual(len(entry["dois"]), 5)

        records = catalog_io.load_all()
        self.assertIn("10.1097/mat.0000000000002822", records)

        # PMID-only block (no DOI in markdown) resolved via resolve(pmid)
        pmid_only = records["10.9999/fixture.pmidonly.1"]
        self.assertEqual(pmid_only["pmid"], "42584187")

        # Borderline block is now ingested, tagged, with no non-empty impact
        # unless the block actually carried one.
        borderline = records["10.9999/fixture.borderline.1"]
        self.assertIn("borderline", borderline.get("tags") or [])
        self.assertIn("section-inferred", borderline.get("tags") or [])  # persisted so a later real heading can upgrade the section
        self.assertNotIn("_section_inferred", borderline,
                          "transient merge flag must never be persisted")

    def test_weekly_idempotent_second_run(self):
        ingest_weekly.run(str(FIXTURES / "pubmed-2099-01-04.md"), dry_run=False, resolve_fn=stub_resolve)
        before = file_hashes(Path(self.tmp))

        n_added, n_merged, _ = ingest_weekly.run(
            str(FIXTURES / "pubmed-2099-01-04.md"), dry_run=False, resolve_fn=stub_resolve
        )
        after = file_hashes(Path(self.tmp))

        self.assertEqual(n_added, 0)
        self.assertEqual(n_merged, 5)
        self.assertEqual(before, after, "second run must be byte-identical")

    def test_weekly_malformed_raises_and_leaves_files_unchanged(self):
        before = file_hashes(Path(self.tmp))
        with self.assertRaises(SystemExit) as cm:
            ingest_weekly.main([str(FIXTURES / "pubmed-2099-01-04-MALFORMED.md")])
        self.assertNotEqual(cm.exception.code, 0)
        after = file_hashes(Path(self.tmp))
        self.assertEqual(before, after, "a parse failure must not touch any file")

    def test_weekly_pmid_only_block_resolves_doi_via_pmid(self):
        n_added, n_merged, _ = ingest_weekly.run(
            str(FIXTURES / "pubmed-2099-01-04.md"), dry_run=False, resolve_fn=stub_resolve
        )
        records = catalog_io.load_all()
        rec = records["10.9999/fixture.pmidonly.1"]
        self.assertEqual(rec["section"], "Misc")
        self.assertFalse(rec.get("tags"))

    def test_weekly_dry_run_pmid_unresolved_does_not_exit(self):
        # resolve stubbed to {} offline -- the PMID-only block's DOI cannot
        # be resolved, but --dry-run must report it and continue, not raise.
        n_added, n_merged, digest_id = ingest_weekly.run(
            str(FIXTURES / "pubmed-2099-01-04.md"), dry_run=True, resolve_fn=lambda ident: {}
        )
        self.assertEqual(digest_id, "2099-W01")
        # everything else in the fixture still resolves via doi.org links,
        # only the PMID-only block and (transitively) nothing else is skipped
        self.assertGreaterEqual(n_added, 4)


class TestMonthlyIngest(TempCatalogTestCase):
    def test_monthly_new_plus_merge(self):
        # Seed the shared DOI via the weekly ingest first.
        ingest_weekly.run(str(FIXTURES / "pubmed-2099-01-04.md"), dry_run=False, resolve_fn=stub_resolve)

        n_added, n_merged, digest_id = ingest_monthly.run(
            str(FIXTURES / "Test-2099-critical-care-digest.md"), dry_run=False,
            dois_file=None, resolve_fn=stub_resolve,
        )
        self.assertEqual(digest_id, "2099-01")
        self.assertEqual(n_added, 1)
        self.assertEqual(n_merged, 1)

        records = catalog_io.load_all()
        shared = records["10.1097/mat.0000000000002822"]
        self.assertEqual(sorted(shared["digests"]), ["2099-01", "2099-W01"])

    def test_monthly_shared_doi_take_only_set_if_empty(self):
        ingest_weekly.run(str(FIXTURES / "pubmed-2099-01-04.md"), dry_run=False, resolve_fn=stub_resolve)
        records = catalog_io.load_all()
        weekly_take = records["10.1097/mat.0000000000002822"]["take"]
        self.assertTrue(weekly_take, "weekly ingest should have set a non-empty take")

        ingest_monthly.run(
            str(FIXTURES / "Test-2099-critical-care-digest.md"), dry_run=False,
            dois_file=None, resolve_fn=stub_resolve,
        )
        records = catalog_io.load_all()
        self.assertEqual(records["10.1097/mat.0000000000002822"]["take"], weekly_take,
                          "monthly ingest must not overwrite an already-set take")

    def test_monthly_bad_heading_raises_zero_rows_written(self):
        before_count = len(catalog_io.load_all())
        with self.assertRaises(SystemExit) as cm:
            ingest_monthly.main([str(FIXTURES / "Test-2099-BADHEADING.md")])
        self.assertNotEqual(cm.exception.code, 0)
        after_count = len(catalog_io.load_all())
        self.assertEqual(before_count, after_count)
        self.assertNotIn("10.9999/fixture.badheading.1", catalog_io.load_all())


class TestAddDoi(TempCatalogTestCase):
    def test_add_doi_manual_source_and_section_inference(self):
        rec = add_doi.run(
            "10.1097/MAT.0000000000002822", section=None, take="",
            digest_id=None, resolve_fn=stub_resolve,
        )
        self.assertEqual(rec["source"], "manual")
        self.assertEqual(rec["section"], "ECMO",
                          "title contains 'extracorporeal' -> should infer ECMO")


class TestSections(unittest.TestCase):
    def test_respiratory_ards_count_suffix_alias(self):
        self.assertEqual(sections.canonical("## Respiratory & ARDS (5)"), "Respiratory/ARDS")


class TestBorderlineAndPracticeChanging(TempCatalogTestCase):
    def test_real_section_wins_over_inferred_regardless_of_order(self):
        # Practice-changing (inferred section) appears before the real ECMO
        # section in every production file, so a same-DOI duplicate must let
        # the later, real-section upsert win.
        inferred_first = {
            "doi": "10.1097/mat.0000000000002822", "title": "x", "source": "weekly",
            "_section_inferred": True,
        }
        real_second = {
            "doi": "10.1097/mat.0000000000002822", "title": "x", "source": "weekly",
            "section": "ECMO", "_section_inferred": False,
        }
        records = {}
        # inferred creates the record with a keyword-guessed section
        inferred_first["section"] = "Misc"
        ic.upsert(records, dict(inferred_first), "2099-W01")
        self.assertEqual(records["10.1097/mat.0000000000002822"]["section"], "Misc")
        # the real section entry must override it
        ic.upsert(records, dict(real_second), "2099-W01")
        self.assertEqual(records["10.1097/mat.0000000000002822"]["section"], "ECMO")
        self.assertFalse(records["10.1097/mat.0000000000002822"]["_section_inferred"])


class TestRealDigestDryRun(unittest.TestCase):
    """Runs ingest_weekly --dry-run against all 15 real pubmed-*.md digests,
    offline (resolve stubbed to {}). Does NOT touch the temp/real catalog --
    dry-run performs no writes."""

    EXPECTED_COUNTS = [8, 22, 28, 28, 28, 26, 17, 27, 12, 28, 29, 43, 21, 28, 21]

    def test_all_real_digests_parse_and_report_counts(self):
        files = sorted(glob.glob(str(REAL_DIGESTS_DIR / "pubmed-*.md")))
        self.assertEqual(len(files), 15, f"expected 15 real digest files, found {len(files)}: {files}")

        report = []
        for path, expected in zip(files, self.EXPECTED_COUNTS):
            text = open(path, encoding="utf-8").read()
            try:
                digest_id, date, title, papers = ingest_weekly.parse_digest(text, path)
            except ingest_weekly.ParseFailure as e:
                report.append((os.path.basename(path), "PARSE FAILURE", str(e), expected))
                continue
            actual = len(papers)
            report.append((os.path.basename(path), actual, "ok", expected))

        print("\n--- weekly --dry-run parse counts (actual vs expected) ---")
        for name, actual, status, expected in report:
            match = "MATCH" if actual == expected else "DIFF"
            print(f"{name}: actual={actual} expected={expected} status={status} [{match}]")

        # The backfill consumes these exact files. A hard parse failure on any
        # one of them is a stop-the-line defect, so it must fail the suite --
        # the previous assertion (len(report) == 15) could never fail.
        failures = [(n, msg) for n, a, msg, _e in report if a == "PARSE FAILURE"]
        self.assertEqual(failures, [], f"real weekly digests failed to parse: {failures}")
        self.assertEqual(len(report), 15)
        # Counts are reported, not asserted: EXPECTED_COUNTS is an unverified
        # hand tally and 13 of 15 currently disagree with the parser.


class TestRealMonthlyDigests(unittest.TestCase):
    """The two real monthly digests the backfill consumes must parse, and every
    '## ' heading must be either a canonical section or a declared trailer."""

    PATHS = [
        Path("/Users/neel/Downloads/Claude Sandbox/June-2026-critical-care-digest.md"),
        Path("/Users/neel/Downloads/Claude Sandbox/July-2026-critical-care-digest.md"),
    ]

    def test_monthly_headings_all_recognized(self):
        for path in self.PATHS:
            self.assertTrue(path.exists(), f"missing real monthly digest: {path}")
            text = path.read_text(encoding="utf-8")
            ingest_monthly.parse_header(text, str(path))
            for heading_raw, _body in ingest_monthly.split_sections(text):
                heading = heading_raw.strip()
                if ingest_monthly.is_trailer(heading):
                    continue
                sections.canonical(heading)  # raises ValueError if unknown

    def test_monthly_trailers_carry_no_papers(self):
        """Skipping a trailer must never drop a paper."""
        for path in self.PATHS:
            text = path.read_text(encoding="utf-8")
            for heading_raw, body in ingest_monthly.split_sections(text):
                if ingest_monthly.is_trailer(heading_raw.strip()):
                    entries = list(ingest_monthly.split_entries(body))
                    self.assertEqual(entries, [],
                                      f"trailer {heading_raw!r} in {path.name} carries {len(entries)} entries")

    def test_trailer_list_is_closed_not_a_prefix_match(self):
        self.assertTrue(ingest_monthly.is_trailer("Link audit"))
        self.assertTrue(ingest_monthly.is_trailer("  ACCURACY   AUDIT "))
        # a real section whose name merely starts with a trailer word is NOT swallowed
        self.assertFalse(ingest_monthly.is_trailer("Zotero exports and ECMO"))
        self.assertFalse(ingest_monthly.is_trailer("Renal"))
        self.assertFalse(ingest_monthly.is_trailer("Link audit findings"))


if __name__ == "__main__":
    unittest.main()
