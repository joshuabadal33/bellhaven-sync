# bellhaven-sync

Keeps the CRM's picture of which Bellhaven communities belong to the Bellhaven Senior Living parent account in line with Bellhaven's own website.

```
website ──scraper.py──> 35 locations ─┐
                                      ├─ matcher.py ──> proposals ──> review app ──(approve)──> CRM API
CRM API ──crm.py──> accounts+contacts ┘        (no writes)        (store.py remembers decisions)
```

| File | What it does |
|---|---|
| `scraper.py` | Walks the paginated directory, plus the homepage/about page for communities not in the directory yet. Parses each detail page. |
| `matcher.py` | Matches locations to accounts and builds proposals. Pure logic, no network, no writes. The billing SOP lives here. |
| `store.py` | SQLite file (`sync_state.db`) holding proposals and decisions. This is what makes re-runs safe. |
| `pipeline.py` | The daily job: scrape, load CRM, match, queue new proposals. |
| `app.py` + `templates/index.html` | Flask review app. Shows evidence, approve/reject. Approve is the only path that writes. |
| `apply.py` | Turns one approved proposal into API calls. Re-reads the account first and refuses stale or SOP-breaking writes. |
| `crm.py` | Small API client. |
| `tests/` | 21 tests against a snapshot of the sandbox data: matching, the SOP, "approve everything, re-run, nothing new is proposed", and the failure cases (stale approval, half-finished CHOW, broken scrape). |

## Running it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Windows: setup.bat
cp .env.example .env          # put your token in .env
python pipeline.py            # Windows: run_pipeline.bat
python app.py                 # Windows: start_review_app.bat, then open http://127.0.0.1:5000
python -m unittest discover tests
```

## Matching approach

Every live (not duplicate, not CHOW'd) non-parent account in the CRM is a candidate, not just the ones under Bellhaven, because acquired communities are often still sitting under their old parent.

1. **Address first.** Streets are normalized (`Road West`→`rd w`, `Northwest`→`nw`, `Pk`→`pike`) and compared within the same city and state. Names change on every rebrand; the building doesn't move.
2. **Exact name second.** Same normalized name in the same city catches a stale address on file (Ashtabula has a PO Box).
3. **Otherwise it's new.** If a same-city account shares the distinctive part of the name (Union Square), it's shown as a *low-confidence possible match* next to the "create" proposal instead of being matched automatically. Same name in a different state (Amberly Manor, CO vs OH) is not considered.

When several accounts sit at one address, one survives. It's a strict priority, checked in order: already under Bellhaven, has the website's administrator as a contact, phone matches, exact name, exact street spelling, has a parent, Active, number of contacts, then lowest account id, so the pick never depends on the order the API returns rows. The rest become duplicates (`duplicate_of_account` + Inactive). Kettering has three records and none of the first four signals, so its proposals are marked medium confidence.

## Outcomes

| Proposal | When | What approval writes |
|---|---|---|
| Fix existing | Matched, but wrong parent / name / street / zip / status | PATCH those fields + note |
| Change of ownership (new account) | Needs to move to Bellhaven, **revenue > 0 and AR > 0** | Old account untouched except `chow_current_account`; new account created under Bellhaven |
| Change of ownership (link) | Under Bellhaven, gone from website, another owner has an account at the same address, revenue and AR > 0 | `chow_current_account` on the old account → the buyer's existing account (Sandusky → Millstone) |
| Duplicate | Extra record at a matched address | `duplicate_of_account`, Inactive, note |
| New account | Location with no CRM record | POST under Bellhaven |
| Not on website | Under Bellhaven in CRM, absent from the site, no buyer found | Needs Review + note. We can't tell closed from sold, so a person decides. |

Billing SOP: before any re-parent, check `lifetime_revenue` and `outstanding_ar`. Both > 0 means preserve the account and do a CHOW; otherwise re-parent directly (e.g. Lima has revenue but no AR, so it's re-parented). Every note this tool writes starts with `[bellhaven-sync] <date>` so changes are traceable. CHOW'd accounts get no note, per "leave it exactly as it is" (the only field written on them is `chow_current_account`).

## Write-time safety

The matcher works from a snapshot, and a proposal can sit in the queue for a while. So `apply.py` re-reads the account right before every write and refuses (nothing written, reason shown in the app) if:

- the account's parent changed since the proposal was built, or it has since been marked a duplicate;
- the write would re-parent an account that now has revenue **and** AR (this covers fixes, possible matches and stale approvals, not just the normal path);
- the write would make an account with revenue and AR Inactive as a duplicate. It stays in the queue until billing clears the AR.

Change of ownership and new-account writes are two steps, and the API has no transactions. Before creating, `apply.py` looks for an account it already created for that location and reuses it, and it skips the old account if its pointer is already set. If the API fails between the two steps, approving again finishes the job instead of creating a second account.

## Re-run safety

Each proposal's key is built from the account and the exact change. Approved or rejected keys are never queued again (as long as `sync_state.db` is kept, which is why it lives next to the review app). After an approval the CRM itself no longer differs, so the matcher wouldn't propose it anyway. Duplicates and CHOW'd accounts are excluded from matching, and created accounts match on the next run. If the data changes later (say the website lists a new address), the key changes and a fresh proposal appears, on purpose, since it's a new question. Stale pending items are cleared each run.

If a scrape comes back with fewer than 90% of last run's communities, the run stops before queuing anything, so a website outage can't make real communities look "gone".

## Schedule

The pipeline runs daily on the machine that hosts the review app (`schedule/crontab.txt`, with the Windows Task Scheduler line), because both need the same `sync_state.db`. That file holds CRM data, so it is git-ignored and never committed. The job only queues proposals, so unattended runs can't damage data. `.github/workflows/daily-sync.yml` runs the test suite daily and on every push. In production I'd move the state to a private shared database so the pipeline could run there too.

## What I'd build next

- Move contacts from a duplicate to its survivor (the API supports it; for now the duplicate proposal lists them).
- Move review state into a private shared database and add sign-in to the review app, so more than one reviewer can use it.
- Compare phone numbers and care offerings too. Phone mismatches are common here (about 15 matched accounts), and I didn't want to auto-propose those without knowing which source is trusted.
- Alert when the website's count ("35 communities") disagrees with what was scraped.
- Run against every parent with a website, not just Bellhaven, and track ownership history over time.
