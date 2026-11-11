"""Daily job: scrape the website, compare with the CRM, queue proposals for review.

    python pipeline.py

Safe to run as often as you like. It only ever adds proposals to the review
queue; nothing is written to the CRM until someone approves it in the app.
"""
import json
import sys

import config
import store
from crm import CRM
from matcher import Matcher
from scraper import scrape


class IncompleteScrape(Exception):
    pass


def _check_complete(conn, locations):
    """A half-broken scrape would make real communities look 'gone from the website'.
    Stop before queuing anything if we got far fewer than last time."""
    last = store.last_run(conn)
    expected = (last or {}).get("locations") or 0
    if not locations or len(locations) < 0.9 * expected:
        raise IncompleteScrape(f"Scraped {len(locations)} communities, last run had {expected}. "
                               "Nothing was queued; check the website/scraper.")


def run(crm=None, locations=None, conn=None):
    crm = crm or CRM()
    conn = conn or store.connect()
    locations = locations if locations is not None else scrape()
    _check_complete(conn, locations)

    accounts, contacts = crm.accounts(), crm.contacts()
    proposals, matches = Matcher(accounts, contacts).run(locations)
    new, skipped = store.sync_proposals(conn, proposals)

    stats = {"locations": len(locations), "accounts": len(accounts), "new": new,
             "skipped": skipped, "matches": len(matches)}
    store.log_run(conn, **stats)

    report = {"stats": stats, "confident_matches": matches,
              "proposals": [{"kind": p["kind"], "title": p["title"], "confidence": p["confidence"]} for p in proposals]}
    (config.ROOT / "last_run.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return stats, proposals, matches


if __name__ == "__main__":
    stats, proposals, matches = run()
    print(f"Website locations:      {stats['locations']}")
    print(f"CRM accounts:           {stats['accounts']}")
    print(f"Already correct:        {stats['matches']}")
    print(f"Proposals this run:     {len(proposals)}")
    print(f"  new in review queue:  {stats['new']}")
    print(f"  already decided:      {stats['skipped']} (not re-proposed)")
    sys.exit(0)
