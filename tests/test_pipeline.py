"""Offline tests against a snapshot of the real sandbox data.

    python -m unittest discover tests

FakeCRM behaves like the API (in memory), so we can approve everything and then
check the end state and that a second run proposes nothing new.
"""
import copy
import itertools
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import store  # noqa: E402
from apply import BlockedError, apply  # noqa: E402
from matcher import Matcher, norm_name, norm_street  # noqa: E402
from pipeline import IncompleteScrape, run  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
BH = "0015QAPLGS3FVYEEEM"


def load(name):
    lines = (FIXTURES / name).read_text(encoding="utf-8").strip().splitlines()
    header = lines[0].split("|")
    return [dict(zip(header, line.split("|"))) for line in lines[1:]]


def accounts_fixture():
    rows = load("accounts.psv")
    for r in rows:
        r["lifetime_revenue"] = float(r["lifetime_revenue"])
        r["outstanding_ar"] = float(r["outstanding_ar"])
    return rows


def locations_fixture():
    rows = load("locations.psv")
    for r in rows:
        r["care_offerings"] = r["care_offerings"].split(";")
    return rows


def contacts_fixture():
    rows = load("contacts.psv")
    for r in rows:
        r["is_active"] = r["is_active"] == "true"
    return rows


class FakeCRM:
    _ids = itertools.count(1)

    def __init__(self):
        self.rows = {a["account_id"]: a for a in accounts_fixture()}
        self._contacts = contacts_fixture()

    def accounts(self):
        return copy.deepcopy(list(self.rows.values()))

    def contacts(self):
        return copy.deepcopy(self._contacts)

    def get_account(self, account_id):
        return dict(self.rows[account_id])

    def update_account(self, account_id, fields):
        self.rows[account_id].update(fields)
        return self.rows[account_id]

    def create_account(self, fields):
        new_id = f"001NEW{next(self._ids):012d}"
        row = {"account_id": new_id, "lifetime_revenue": 0, "outstanding_ar": 0,
               "chow_current_account": "", "duplicate_of_account": "", "note": "", **fields}
        self.rows[new_id] = row
        return row


class NormalizeTests(unittest.TestCase):
    def test_streets(self):
        self.assertEqual(norm_street("199 Barks Road West"), norm_street("199 Barks Rd W"))
        self.assertEqual(norm_street("4850 Northwest Sylvania Avenue"), norm_street("4850 NW Sylvania Ave"))
        self.assertEqual(norm_street("3313 Wilmington Pk"), norm_street("3313 Wilmington Pike"))

    def test_names(self):
        self.assertEqual(norm_name("Bellhaven Rehab and Nursing of Grove City"),
                         norm_name("Bellhaven Rehabilitation & Nursing of Grove City"))
        self.assertEqual(norm_name("Bellhaven Health Care Center of Ashland"),
                         norm_name("Bellhaven Healthcare Centre of Ashland"))
        self.assertEqual(norm_name("Arbors at Bellhaven Dayton"), norm_name("The Arbors at Bellhaven - Dayton"))


class MatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proposals, cls.matches = Matcher(accounts_fixture(), contacts_fixture()).run(locations_fixture())
        cls.by_account = {}
        for p in cls.proposals:
            cls.by_account.setdefault(p["account_id"], []).append(p)

    def kinds(self, account_id):
        return [p["kind"] for p in self.by_account.get(account_id, [])]

    def test_sop_preserves_accounts_with_revenue_and_ar(self):
        self.assertEqual(self.kinds("001U6RW32TY0WSXZZB"), ["chow_new"])   # Tiffin
        self.assertEqual(self.kinds("001A34WFSUYHCRBLFT"), ["chow_new"])   # Marietta

    def test_sop_reparents_when_no_open_ar(self):
        lima = self.by_account["001LGFPBJY4N9MB6KL"][0]                    # revenue, AR = 0
        self.assertEqual(lima["kind"], "update")
        self.assertEqual(lima["payload"]["changes"]["parent_id"], BH)

    def test_sold_facility_links_to_buyer(self):
        sandusky = self.by_account["001SXSF4ELF0Z2LGDM"][0]
        self.assertEqual(sandusky["kind"], "chow_link")
        self.assertEqual(sandusky["payload"]["chow_current_account"], "0017JP8Z1UQ763BVK3")

    def test_duplicates(self):
        for loser in ["001JD2MWRA74LTSN24", "001BLYF02K97SZLZHH", "0011AB44D05WLA9HTX",
                      "00159PL81N38KM4FHM", "001QU150PM4Z15UA71"]:
            self.assertEqual(self.kinds(loser), ["duplicate"], loser)

    def test_new_locations(self):
        created = {p["location_slug"] for p in self.proposals if p["kind"] == "create"}
        self.assertEqual(created, {"bellhaven-of-batavia", "bellhaven-at-union-square",
                                   "bellhaven-of-carlisle", "amberly-manor"})

    def test_same_name_other_state_is_not_a_match(self):
        self.assertEqual(self.kinds("0015D74ZLRY810RGY5"), [])   # Amberly Manor, Colorado

    def test_union_square_only_flagged_as_possible(self):
        self.assertEqual(self.kinds("001GNU41AVXZRLLJ9P"), ["possible_match"])

    def test_exact_website_name_is_proposed(self):
        # Same facility despite spelling variants, but the CRM should carry the website's exact name.
        for account_id, name in [("001TZCSTBZFM6K5BGN", "Bellhaven Rehabilitation & Nursing of Grove City"),
                                 ("0010KJFP601YNTZVDA", "The Arbors at Bellhaven - Dayton"),
                                 ("001RJ0D7Z5MAZN5HX5", "Bellhaven Healthcare Centre of Ashland")]:
            fix = self.by_account[account_id][0]
            self.assertEqual(fix["payload"]["changes"], {"name": name})

    def test_not_on_website(self):
        self.assertEqual(self.kinds("00116ETS45BL7DTQP7"), ["needs_review"])   # Alliance
        self.assertEqual(self.kinds("0016PVXH4B25HWR7QE"), ["needs_review"])   # Coldwater


class WriteSafetyTests(unittest.TestCase):
    """The approve path re-checks the CRM before writing. These are the failure
    cases: stale approvals, billing-protected accounts, half-finished writes."""

    def proposal(self, crm, kind, account_id=None, slug=None):
        proposals, _ = Matcher(crm.accounts(), crm.contacts()).run(locations_fixture())
        return next(p for p in proposals if p["kind"] == kind
                    and (account_id is None or p["account_id"] == account_id)
                    and (slug is None or p["location_slug"] == slug))

    def test_survivor_does_not_depend_on_api_order(self):
        kettering = next(l for l in locations_fixture() if l["slug"] == "bellhaven-of-kettering")
        for accounts in (accounts_fixture(), accounts_fixture()[::-1]):
            m = Matcher(accounts, contacts_fixture())
            survivor, _ = m._pick_survivor(m.find_candidates(kettering)[0], kettering)
            self.assertEqual(survivor["account_id"], "001WR41PYNWXCAE2X4")   # Harborview copy, exact street

    def test_possible_match_cannot_skip_the_sop(self):
        crm = FakeCRM()
        crm.rows["001GNU41AVXZRLLJ9P"].update(lifetime_revenue=9000.0, outstanding_ar=800.0)
        with self.assertRaises(BlockedError):
            apply(crm, self.proposal(crm, "possible_match"))
        self.assertEqual(crm.rows["001GNU41AVXZRLLJ9P"]["parent_id"], "001DAAUWV2J3SHQJ34")

    def test_duplicate_with_open_ar_is_not_deactivated(self):
        crm = FakeCRM()
        p = self.proposal(crm, "duplicate", "001JD2MWRA74LTSN24")
        crm.rows["001JD2MWRA74LTSN24"].update(lifetime_revenue=5000.0, outstanding_ar=300.0)
        with self.assertRaises(BlockedError):
            apply(crm, p)
        self.assertEqual(crm.rows["001JD2MWRA74LTSN24"]["status"], "Active")

    def test_ar_added_after_proposal_blocks_direct_reparent(self):
        crm = FakeCRM()
        lima = self.proposal(crm, "update", "001LGFPBJY4N9MB6KL")          # proposed when AR was 0
        crm.rows["001LGFPBJY4N9MB6KL"]["outstanding_ar"] = 1200.0           # billing posts AR before approval
        with self.assertRaises(BlockedError):
            apply(crm, lima)
        self.assertEqual(crm.rows["001LGFPBJY4N9MB6KL"]["parent_id"], "001FJZYHR7MLFMNPLL")

    def test_account_changed_since_proposal_is_not_written(self):
        crm = FakeCRM()
        p = self.proposal(crm, "update", "001LGFPBJY4N9MB6KL")
        crm.rows["001LGFPBJY4N9MB6KL"]["parent_id"] = "001YRHHXQ5HJ0TCL2U"  # someone moved it meanwhile
        with self.assertRaises(BlockedError):
            apply(crm, p)

    def test_chow_retry_after_partial_failure_creates_one_account(self):
        crm = FakeCRM()
        p = self.proposal(crm, "chow_new", "001U6RW32TY0WSXZZB")           # Tiffin
        real_update, calls = crm.update_account, {"n": 0}

        def flaky(account_id, fields):                                     # API dies on the second step once
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("timeout")
            return real_update(account_id, fields)

        crm.update_account = flaky
        with self.assertRaises(RuntimeError):
            apply(crm, p)
        apply(crm, p)                                                      # reviewer clicks Approve again
        apply(crm, p)                                                      # and once more, by accident

        tiffins = [a for a in crm.rows.values() if a["name"] == "Bellhaven of Tiffin" and a["parent_id"] == BH]
        self.assertEqual(len(tiffins), 1)
        self.assertEqual(crm.rows["001U6RW32TY0WSXZZB"]["chow_current_account"], tiffins[0]["account_id"])

    def test_create_is_not_repeated(self):
        crm = FakeCRM()
        p = self.proposal(crm, "create", slug="bellhaven-of-batavia")
        apply(crm, p)
        apply(crm, p)
        self.assertEqual(sum(a["name"] == "Bellhaven of Batavia" for a in crm.rows.values()), 1)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        # Keep test runs from overwriting the real last_run.json in the project folder.
        self._root, self._tmp = config.ROOT, tempfile.TemporaryDirectory()
        config.ROOT = Path(self._tmp.name)

    def tearDown(self):
        config.ROOT = self._root
        self._tmp.cleanup()

    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(store.SCHEMA)
        return conn

    def test_rejected_item_is_not_proposed_again(self):
        crm, conn, locations = FakeCRM(), self._conn(), locations_fixture()
        run(crm, locations, conn)
        union = next(i for i in store.pending(conn) if i["kind"] == "possible_match")
        store.mark(conn, union["key"], "rejected")

        stats, _, _ = run(crm, locations, conn)
        self.assertEqual(stats["new"], 0)
        self.assertNotIn(union["key"], [i["key"] for i in store.pending(conn)])

    def test_incomplete_scrape_queues_nothing(self):
        crm, conn, locations = FakeCRM(), self._conn(), locations_fixture()
        run(crm, locations, conn)
        before = len(store.pending(conn))
        with self.assertRaises(IncompleteScrape):
            run(crm, locations[:15], conn)                                  # website half-broken today
        self.assertEqual(len(store.pending(conn)), before)

    def test_approve_all_then_rerun_is_quiet(self):
        crm, conn, locations = FakeCRM(), self._conn(), locations_fixture()

        run(crm, locations, conn)
        for item in store.pending(conn):
            if item["kind"] == "possible_match":
                store.mark(conn, item["key"], "rejected")
            else:
                apply(crm, item["body"])
                store.mark(conn, item["key"], "approved")

        stats, proposals, matches = run(crm, locations, conn)
        self.assertEqual(stats["new"], 0)
        self.assertEqual(store.pending(conn), [])
        self.assertEqual(len(matches), 35)   # every website location now matches cleanly

        # SOP: old Tiffin account untouched except the CHOW pointer
        tiffin = crm.rows["001U6RW32TY0WSXZZB"]
        self.assertEqual(tiffin["parent_id"], "001FWSQ30SFW6S7604")
        self.assertTrue(tiffin["chow_current_account"])
        self.assertEqual(crm.rows[tiffin["chow_current_account"]]["parent_id"], BH)


if __name__ == "__main__":
    unittest.main()
