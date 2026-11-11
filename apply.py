"""Write an approved proposal back to the CRM. Only the review app calls this.

The matcher decided what *should* change from a snapshot. This file re-reads
each account right before writing and refuses if:
  - the account changed since the proposal was built (stale approval), or
  - the write would break the billing SOP (re-parenting or deactivating an
    account with revenue AND open AR).
So the SOP holds at write time, not just when proposals are generated.

Multi-step writes (change of ownership, new account) check what already exists
first, so retrying after a half-finished attempt resumes instead of creating a
second account.
"""
from datetime import date

import config
from matcher import must_preserve, norm_name, norm_street


class BlockedError(Exception):
    """Refused before writing anything. The reason is shown in the review app."""


def _note(current, line):
    stamped = f"{config.NOTE_TAG} {date.today().isoformat()}: {line}"
    return f"{(current.get('note') or '').strip()}\n{stamped}".strip()


def _current(crm, account_id, proposal):
    """Fresh copy of the account, checked against what the proposal assumed."""
    current = crm.get_account(account_id)
    for field, was in (proposal.get("expected") or {}).items():
        now = current.get(field) or ""
        if now != (was or ""):
            raise BlockedError(f"{field} changed since this was proposed ({was or '(none)'} -> {now or '(none)'}). "
                               "Run the pipeline again so the proposal reflects the current CRM.")
    if current.get("duplicate_of_account"):
        raise BlockedError(f"Account is already a duplicate of {current['duplicate_of_account']}.")
    return current


def _sop_block(current, action):
    if must_preserve(current):
        raise BlockedError(
            f"Billing SOP: revenue ${current.get('lifetime_revenue') or 0:,.0f} and AR "
            f"${current.get('outstanding_ar') or 0:,.0f} are both > 0, so this account can't be {action}. "
            "Nothing was written.")


def _existing_twin(crm, fields):
    """An account this tool already created for the same location (e.g. on a failed earlier attempt)."""
    for a in crm.accounts():
        if (a.get("parent_id") == fields["parent_id"]
                and not a.get("duplicate_of_account")
                and norm_name(a.get("name")) == norm_name(fields["name"])
                and norm_street(a.get("billing_street")) == norm_street(fields["billing_street"])
                and a.get("billing_state") == fields["billing_state"]):
            return a
    return None


def apply(crm, proposal):
    """Returns a short human-readable description of what was written."""
    kind, p = proposal["kind"], proposal["payload"]
    account_id = proposal["account_id"]

    if kind in ("update", "possible_match"):
        current = _current(crm, account_id, proposal)
        changes = dict(p["changes"])
        if changes.get("parent_id") and changes["parent_id"] != current.get("parent_id"):
            _sop_block(current, "re-parented directly (it needs a change of ownership)")
        crm.update_account(account_id, {**changes, "note": _note(current, p["note"])})
        return f"Updated {account_id}: {', '.join(changes)}"

    if kind == "duplicate":
        current = _current(crm, account_id, proposal)
        _sop_block(current, "made Inactive as a duplicate until billing clears the AR")
        crm.update_account(account_id, {
            "duplicate_of_account": p["duplicate_of_account"],
            "status": "Inactive",
            "note": _note(current, p["note"]),
        })
        return f"Marked {account_id} as duplicate of {p['duplicate_of_account']}"

    if kind == "needs_review":
        current = _current(crm, account_id, proposal)
        crm.update_account(account_id, {"status": "Needs Review", "note": _note(current, p["note"])})
        return f"Flagged {account_id} as Needs Review"

    if kind == "create":
        fields = dict(p["create"])
        twin = _existing_twin(crm, fields)
        if twin:
            return f"Already exists: {twin['account_id']} ({fields['name']}), nothing created"
        fields["note"] = f"{config.NOTE_TAG} {date.today().isoformat()}: Created from Bellhaven website listing"
        new = crm.create_account(fields)
        return f"Created {new['account_id']} ({fields['name']})"

    if kind == "chow_new":
        # Billing SOP: the old account is left exactly as it is apart from the CHOW pointer.
        old = _current(crm, p["old_account"], proposal)
        if old.get("chow_current_account"):
            return f"Already done: {p['old_account']} points to {old['chow_current_account']}"
        fields = dict(p["create"])
        new = _existing_twin(crm, fields)     # created on an earlier attempt whose second step failed
        resumed = bool(new)
        if not new:
            fields["note"] = (f"{config.NOTE_TAG} {date.today().isoformat()}: Created for change of ownership; "
                              f"previous account {p['old_account']} kept for billing")
            new = crm.create_account(fields)
        crm.update_account(p["old_account"], {"chow_current_account": new["account_id"]})
        verb = "Reused" if resumed else "Created"
        return f"{verb} {new['account_id']}; set chow_current_account on {p['old_account']}"

    if kind == "chow_link":
        current = _current(crm, account_id, proposal)
        target = p["chow_current_account"]
        if current.get("chow_current_account") == target:
            return f"Already done: {account_id} points to {target}"
        if current.get("chow_current_account"):
            raise BlockedError(f"Already points to a different account ({current['chow_current_account']}).")
        crm.update_account(account_id, {"chow_current_account": target})
        return f"Set chow_current_account on {account_id} -> {target}"

    raise ValueError(f"Unknown proposal kind: {kind}")
