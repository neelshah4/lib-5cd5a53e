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
            (("GET", "/items?"), FakeResponse(200, [])),
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


    # 8. a held (verified is not True) record is never selected for push
    def test_unverified_record_never_pushed(self):
        held = sample_record(doi="10.1/held", digests=["2026-W01"])
        held["verified"] = False
        ok = sample_record(doi="10.1/ok", digests=["2026-W01"])
        catalog = {held["doi"]: held, ok["doi"]: ok}
        selected = zotero_push.select_records(catalog, "2026-W01", False, None)
        self.assertEqual([r["doi"] for r in selected], ["10.1/ok"])

        held_manual = sample_record(doi="10.1/heldm")
        held_manual["source"] = "manual"
        held_manual["verified"] = False
        ok_manual = sample_record(doi="10.1/okm")
        ok_manual["source"] = "manual"
        catalog2 = {held_manual["doi"]: held_manual, ok_manual["doi"]: ok_manual}
        selected2 = zotero_push.select_records(catalog2, None, True, None)
        self.assertEqual([r["doi"] for r in selected2], ["10.1/okm"])


class DoiIndexTests(unittest.TestCase):
    """Directly exercises build_doi_index / search_by_doi -- the fix for the
    q=<doi> quick-search bug (Zotero's quick search does not match the DOI
    field at all)."""

    def setUp(self):
        self.ctx = zotero_push.ZoteroCtx("KEY", "999", "user", dry_run=False)

    def _patched(self, rules):
        calls = []
        return mock.patch.object(zotero_push.urllib.request, "urlopen",
                                  make_dispatcher(rules, calls)), calls

    # 1. full index built across a 2-page paginated response
    def test_full_index_two_pages(self):
        page1 = [{"key": f"K{i}", "data": {"itemType": "journalArticle", "DOI": f"10.1/{i}"}}
                 for i in range(100)]  # exactly 100 -> forces a second page fetch
        page2 = [{"key": "K100", "data": {"itemType": "journalArticle", "DOI": "10.1/100"}}]

        def items_get(req):
            url = req.full_url
            if "start=100" in url:
                return FakeResponse(200, page2, {"Last-Modified-Version": "50"})
            return FakeResponse(200, page1, {"Last-Modified-Version": "49"})

        rules = [(("GET", "/items?"), items_get)]
        patcher, calls = self._patched(rules)
        with patcher, mock.patch.object(zotero_push.time, "sleep"):
            state = {}
            idx = zotero_push.build_doi_index(self.ctx, state)
        self.assertEqual(len(idx), 101)
        self.assertEqual(idx["10.1/0"], "K0")
        self.assertEqual(idx["10.1/100"], "K100")
        self.assertEqual(state["_index"]["version"], "50")
        # two pages -> two requests
        self.assertEqual(self.ctx._doi_index_requests, 2)

    # 2. DOI recovered via data.DOI, a doi.org URL, and an "extra" DOI line
    def test_doi_extraction_three_sources(self):
        items = [
            {"key": "A", "data": {"itemType": "journalArticle", "DOI": "10.1/direct"}},
            {"key": "B", "data": {"itemType": "journalArticle",
                                   "url": "https://doi.org/10.1016/from-url"}},
            {"key": "C", "data": {"itemType": "journalArticle",
                                   "extra": "Some note\nDOI: 10.1016/from-extra\n"}},
            {"key": "D", "data": {"itemType": "attachment", "DOI": "10.1/should-be-skipped"}},
        ]
        rules = [(("GET", "/items?"), FakeResponse(200, items))]
        patcher, calls = self._patched(rules)
        with patcher, mock.patch.object(zotero_push.time, "sleep"):
            idx = zotero_push.build_doi_index(self.ctx, {})
        self.assertEqual(idx.get("10.1/direct"), "A")
        self.assertEqual(idx.get("10.1016/from-url"), "B")
        self.assertEqual(idx.get("10.1016/from-extra"), "C")
        self.assertNotIn("10.1/should-be-skipped", idx)

    # 3. incremental (since=) path merges into the existing cached index
    def test_incremental_merges_into_existing_index(self):
        state = {"_index": {"version": "10", "doi_to_key": {"10.1/old": "OLD"}}}
        new_items = [{"key": "NEW", "data": {"itemType": "journalArticle", "DOI": "10.1/new"}}]
        rules = [
            (("GET", "/items?"), FakeResponse(200, new_items, {"Last-Modified-Version": "20"})),
            (("GET", "/deleted?"), FakeResponse(200, {"items": []})),
        ]
        patcher, calls = self._patched(rules)
        with patcher, mock.patch.object(zotero_push.time, "sleep"):
            idx = zotero_push.build_doi_index(self.ctx, state)
        self.assertEqual(idx["10.1/old"], "OLD")
        self.assertEqual(idx["10.1/new"], "NEW")
        self.assertEqual(state["_index"]["version"], "20")
        since_calls = [c for c in calls if "since=10" in c[1] and "/items" in c[1]]
        self.assertEqual(len(since_calls), 1)

    # 4. a key reported deleted on Zotero is removed from the index
    def test_deleted_key_is_removed(self):
        state = {"_index": {"version": "10",
                             "doi_to_key": {"10.1/gone": "DEL1", "10.1/stays": "KEEP"}}}
        rules = [
            (("GET", "/items?"), FakeResponse(200, [], {"Last-Modified-Version": "11"})),
            (("GET", "/deleted?"), FakeResponse(200, {"items": ["DEL1"]})),
        ]
        patcher, calls = self._patched(rules)
        with patcher, mock.patch.object(zotero_push.time, "sleep"):
            idx = zotero_push.build_doi_index(self.ctx, state)
        self.assertNotIn("10.1/gone", idx)
        self.assertEqual(idx["10.1/stays"], "KEEP")

    # 5. a record whose DOI IS in the index -> no POST, exactly one PATCH
    def test_indexed_doi_no_post_one_patch(self):
        self.ctx._doi_index = {"10.1/hit": "HITKEY"}
        rules = [
            (("GET", "/items/HITKEY"), FakeResponse(200, {"version": 3,
                                                           "data": {"collections": [], "tags": []}})),
            (("PATCH", "/items/HITKEY"), FakeResponse(204, b"")),
        ]
        patcher, calls = self._patched(rules)
        with patcher:
            key, version = self.ctx.search_by_doi("10.1/hit")
            self.assertEqual(key, "HITKEY")
            wrote = self.ctx.patch_collections_tags("HITKEY", ["SEC1"], "tag1")
        self.assertTrue(wrote)
        post_calls = [c for c in calls if c[0] == "POST"]
        patch_calls = [c for c in calls if c[0] == "PATCH"]
        self.assertEqual(post_calls, [])
        self.assertEqual(len(patch_calls), 1)

    # 6. a record whose DOI is NOT in the index -> caller must POST (no live lookup)
    def test_unindexed_doi_returns_none_no_network_call(self):
        self.ctx._doi_index = {"10.1/other": "X"}
        patcher, calls = self._patched([])
        with patcher:
            key, version = self.ctx.search_by_doi("10.1/absent")
        self.assertIsNone(key)
        self.assertIsNone(version)
        self.assertEqual(calls, [])  # purely an in-memory dict lookup, no request

    # 7. "_index" is never treated as a pushed-record doi key
    def test_index_key_excluded_from_real_doi_keys(self):
        state = {"_index": {"version": "1", "doi_to_key": {}}, "10.1/real": {"key": "K"}}
        self.assertEqual(zotero_push.real_doi_keys(state), ["10.1/real"])

    def test_index_key_ignored_by_manual_state_filter(self):
        rec = sample_record(doi="10.1/manualnew")
        rec["source"] = "manual"
        catalog = {rec["doi"]: rec}
        state = {"_index": {"version": "1", "doi_to_key": {}}}
        json.dump(state, open(pathlib.Path(zotero_push.ROOT) / "tests" / "_tmp_state2.json", "w"))
        tmp2 = pathlib.Path(zotero_push.ROOT) / "tests" / "_tmp_state2.json"
        self.addCleanup(lambda: tmp2.exists() and tmp2.unlink())
        rules = [
            (("GET", "/collections"), FakeResponse(200, COLLECTIONS_ALL_EXIST)),
            (("GET", "/items?"), FakeResponse(200, [])),
            (("POST", "/items"),
             lambda req: FakeResponse(200, {"successful": {"0": {"key": "NEWKEY"}}, "failed": {}})),
        ]
        args = Args(manual=True, state=str(tmp2))
        calls = []
        with mock.patch.object(zotero_push.urllib.request, "urlopen", make_dispatcher(rules, calls)), \
             mock.patch.object(zotero_push, "load_credentials", return_value=(FAKE_KEY, "999", "user")), \
             mock.patch.object(zotero_push.catalog_io, "load_all", return_value=catalog), \
             mock.patch.object(zotero_push.time, "sleep"):
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = zotero_push.run(args)
        self.assertEqual(rc, 0)
        post_calls = [c for c in calls if c[0] == "POST" and c[1].endswith("/items")]
        # the "_index" blob must not have been mistaken for an already-pushed
        # doi, which would have skipped the record entirely
        self.assertEqual(len(post_calls), 1)


if __name__ == "__main__":
    unittest.main()
