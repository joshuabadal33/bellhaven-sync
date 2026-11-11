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
from apply import apply  # noqa: E402
from matcher import Matcher, norm_name, norm_street  # noqa: E402
from pipeline import run  # noqa: E402

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
