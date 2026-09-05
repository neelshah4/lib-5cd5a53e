#!/usr/bin/env python3
"""Unit tests for scripts/zotero_push.py. No network: urllib.request.urlopen
is monkeypatched throughout."""
import io
import json
import pathlib
import sys
import unittest
import urllib.error
from email.message import Message
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import zotero_push  # noqa: E402
import catalog_io  # noqa: E402

FAKE_KEY = "TOTALLY_SECRET_KEY_ABC123"
FAKE_ITEM_KEY = "ZKEY9999"


def _headers(d=None):
    m = Message()
    for k, v in (d or {}).items():
        m[k] = v
    return m


class FakeResponse:
    """Mimics http.client.HTTPResponse enough for zotero_push.api_request."""

    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = json.dumps(body).encode("utf-8") if not isinstance(body, (bytes, str)) else (
            body.encode("utf-8") if isinstance(body, str) else body
        )
        self.headers = _headers(headers)

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_dispatcher(rules, calls):
    """rules: list of (method, path_substring) -> response_or_callable.
    calls: list appended with (method, url) for every request."""

    def _urlopen(req, timeout=None):
        method = req.get_method()
        url = req.full_url
        calls.append((method, url, req.data))
        for (m, sub), resp in rules:
            if m == method and sub in url:
                if callable(resp):
                    resp = resp(req)
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unhandled request: {method} {url}")

    return _urlopen


def sample_record(doi="10.1/abc", digests=None, section="ECMO"):
    r = catalog_io.new_record(
        doi=doi, title="A Study of Things", journal="J Crit Care", year=2026,
        pmid="12345678", url="https://example.org/a", section=section,
    )
    r["authors"] = ["Smith, John A.", "Doe, Jane"]
    r["verified"] = True
    r["digests"] = digests or []
    return r


COLLECTIONS_ALL_EXIST = [
    {"key": "SECX", "data": {"name": "ECMO", "parentCollection": False}},
    {"key": "SECM", "data": {"name": "Misc", "parentCollection": False}},
    {"key": "WEEKC", "data": {"name": "Weekly-2026-01", "parentCollection": False}},
    {"key": "DIGC", "data": {"name": "Digests-Manual", "parentCollection": False}},
]


class Args:
    def __init__(self, **kw):
        self.digest = kw.get("digest")
        self.manual = kw.get("manual", False)
        self.doi = kw.get("doi")
        self.dry_run = kw.get("dry_run", False)
        self.limit = kw.get("limit")
        self.state = kw.get("state")
        self.list_collections = False


class ZoteroPushTests(unittest.TestCase):
    def setUp(self):
        zotero_push._ABSTRACT_CACHE.clear()
        self._cred_patch = mock.patch.object(
            zotero_push, "load_credentials", return_value=(FAKE_KEY, "999", "user")
        )
        self._cred_patch.start()
        self.addCleanup(self._cred_patch.stop)
        self.tmp = pathlib.Path(zotero_push.ROOT) / "tests" / "_tmp_state.json"
        if self.tmp.exists():
            self.tmp.unlink()
        self.addCleanup(lambda: self.tmp.exists() and self.tmp.unlink())

    def run_with_rules(self, args, catalog, rules):
        calls = []
        fake = make_dispatcher(rules, calls)
        with mock.patch.object(zotero_push.urllib.request, "urlopen", fake), \
             mock.patch.object(zotero_push.catalog_io, "load_all", return_value=catalog), \
             mock.patch.object(zotero_push.time, "sleep") as sleep_mock:
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = zotero_push.run(args)
        return rc, calls, buf.getvalue(), sleep_mock

    # 1. DOI-search hit -> no item POST
    def test_doi_search_hit_no_post(self):
        rec = sample_record(digests=["2026-W01"])
        catalog = {rec["doi"]: rec}
        rules = [
            (("GET", "/collections"), FakeResponse(200, COLLECTIONS_ALL_EXIST)),
            (("GET", "/items?"), FakeResponse(200, [
                {"key": FAKE_ITEM_KEY, "version": 5, "data": {"DOI": rec["doi"]}}
            ])),
            (("GET", f"/items/{FAKE_ITEM_KEY}"), FakeResponse(200, {
                "version": 5, "data": {"collections": [], "tags": []}
            })),
            (("PATCH", f"/items/{FAKE_ITEM_KEY}"), FakeResponse(204, b"")),
        ]
        args = Args(digest="2026-W01", state=str(self.tmp))
        rc, calls, out, sleep_mock = self.run_with_rules(args, catalog, rules)
        self.assertEqual(rc, 0)
        post_item_calls = [c for c in calls if c[0] == "POST" and c[1].endswith("/items")]
        self.assertEqual(post_item_calls, [])
        self.assertIn("linked-existing", out)
        state = json.load(open(self.tmp))
        self.assertEqual(state[rec["doi"]]["key"], FAKE_ITEM_KEY)

    # 2. miss -> one POST with correct fields
    def test_doi_miss_one_post(self):
        rec = sample_record(digests=["2026-W01"])
        catalog = {rec["doi"]: rec}
        captured = {}

        def post_items(req):
            body = json.loads(req.data.decode("utf-8"))
            captured["body"] = body
            return FakeResponse(200, {"successful": {"0": {"key": FAKE_ITEM_KEY}}, "failed": {}})

        rules = [
            (("GET", "/collections"), FakeResponse(200, COLLECTIONS_ALL_EXIST)),
            (("GET", "/items?"), FakeResponse(200, [])),
            (("POST", "/items"), post_items),
        ]
        args = Args(digest="2026-W01", state=str(self.tmp))
        rc, calls, out, sleep_mock = self.run_with_rules(args, catalog, rules)
        self.assertEqual(rc, 0)
        post_item_calls = [c for c in calls if c[0] == "POST" and c[1].endswith("/items")]
        self.assertEqual(len(post_item_calls), 1)
        body = captured["body"]
        self.assertEqual(len(body), 1)
        item = body[0]
        self.assertEqual(item["itemType"], "journalArticle")
        self.assertEqual(item["title"], rec["title"])
        self.assertEqual(item["DOI"], rec["doi"])
        self.assertEqual(item["creators"][0], {"creatorType": "author", "lastName": "Smith", "firstName": "John A."})
        self.assertIn({"tag": "weekly:2026-W01"}, item["tags"])
        self.assertIn("created", out)

    # 3. second run -> no writes
    def test_second_run_no_writes(self):
        rec = sample_record(digests=["2026-W01"])
        catalog = {rec["doi"]: rec}
        state = {rec["doi"]: {"key": FAKE_ITEM_KEY, "digests": ["2026-W01"]}}
        json.dump(state, open(self.tmp, "w"))
        rules = [
            (("GET", "/collections"), FakeResponse(200, COLLECTIONS_ALL_EXIST)),
        ]
        args = Args(digest="2026-W01", state=str(self.tmp))
        rc, calls, out, sleep_mock = self.run_with_rules(args, catalog, rules)
        self.assertEqual(rc, 0)
        write_calls = [c for c in calls if c[0] in ("POST", "PATCH")]
        self.assertEqual(write_calls, [])
        self.assertIn("1 skipped", out)

    # 4. weekly id -> Weekly / YYYY-MM collection path
    def test_weekly_collection_path(self):
        parent, child = zotero_push.digest_collection_path("2026-W01", "weekly")
        self.assertEqual(parent, "Weekly")
        # ISO week 1 of 2026 -> Thursday's month
        import datetime
        thursday = datetime.date.fromisocalendar(2026, 1, 4)
        self.assertEqual(child, f"{thursday.year:04d}-{thursday.month:02d}")

    # 5. Backoff header -> sleep called
    def test_backoff_header_triggers_sleep(self):
        rec = sample_record(digests=["2026-W01"])
        catalog = {rec["doi"]: rec}
        rules = [
            (("GET", "/collections"), FakeResponse(200, COLLECTIONS_ALL_EXIST, {"Backoff": "2"})),
            (("GET", "/items?"), FakeResponse(200, [
                {"key": FAKE_ITEM_KEY, "version": 5, "data": {"DOI": rec["doi"]}}
            ])),
            (("GET", f"/items/{FAKE_ITEM_KEY}"), FakeResponse(200, {
                "version": 5, "data": {"collections": [], "tags": []}
            })),
            (("PATCH", f"/items/{FAKE_ITEM_KEY}"), FakeResponse(204, b"")),
        ]
        args = Args(digest="2026-W01", state=str(self.tmp))
        rc, calls, out, sleep_mock = self.run_with_rules(args, catalog, rules)
        self.assertEqual(rc, 0)
        sleep_mock.assert_any_call(2.0)

    # 6. failed response -> non-zero exit
    def test_failed_item_create_nonzero_exit(self):
        rec = sample_record(digests=["2026-W01"])
        catalog = {rec["doi"]: rec}
        rules = [
            (("GET", "/collections"), FakeResponse(200, COLLECTIONS_ALL_EXIST)),
            (("GET", "/items?"), FakeResponse(200, [])),
            (("POST", "/items"), FakeResponse(200, {
                "successful": {}, "failed": {"0": {"key": "0", "code": 400, "message": "Invalid DOI field"}}
            })),
        ]
        args = Args(digest="2026-W01", state=str(self.tmp))
        rc, calls, out, sleep_mock = self.run_with_rules(args, catalog, rules)
        self.assertEqual(rc, 1)

    # 7. the API key / item key never appear in printed output
    def test_key_never_in_output(self):
        rec = sample_record(digests=["2026-W01"])
        catalog = {rec["doi"]: rec}

        def post_items(req):
            return FakeResponse(200, {"successful": {"0": {"key": FAKE_ITEM_KEY}}, "failed": {}})

        rules = [
            (("GET", "/collections"), FakeResponse(200, COLLECTIONS_ALL_EXIST)),
            (("GET", "/items?"), FakeResponse(200, [])),
            (("POST", "/items"), post_items),
        ]
        args = Args(digest="2026-W01", state=str(self.tmp))
        rc, calls, out, sleep_mock = self.run_with_rules(args, catalog, rules)
        self.assertNotIn(FAKE_KEY, out)
        self.assertNotIn(FAKE_ITEM_KEY, out)


if __name__ == "__main__":
    unittest.main()
