"""Write an approved proposal back to the CRM. Only the review app calls this."""
from datetime import date

import config


def _with_note(crm, account_id, line):
    current = (crm.get_account(account_id).get("note") or "").strip()
    stamped = f"{config.NOTE_TAG} {date.today().isoformat()}: {line}"
    return f"{current}\n{stamped}".strip()


def apply(crm, proposal):
    """Returns a short human-readable description of what was written."""
    kind, p = proposal["kind"], proposal["payload"]
    account_id = proposal["account_id"]

    if kind in ("update", "possible_match"):
        fields = dict(p["changes"])
        fields["note"] = _with_note(crm, account_id, p["note"])
        crm.update_account(account_id, fields)
        return f"Updated {account_id}: {', '.join(p['changes'])}"

    if kind == "duplicate":
        crm.update_account(account_id, {
            "duplicate_of_account": p["duplicate_of_account"],
            "status": "Inactive",
            "note": _with_note(crm, account_id, p["note"]),
        })
        return f"Marked {account_id} as duplicate of {p['duplicate_of_account']}"

    if kind == "needs_review":
        crm.update_account(account_id, {
            "status": "Needs Review",
            "note": _with_note(crm, account_id, p["note"]),
        })
        return f"Flagged {account_id} as Needs Review"

    if kind == "create":
        fields = dict(p["create"])
        fields["note"] = f"{config.NOTE_TAG} {date.today().isoformat()}: Created from Bellhaven website listing"
        new = crm.create_account(fields)
        return f"Created {new['account_id']} ({fields['name']})"

    if kind == "chow_new":
        # Billing SOP: the old account is left exactly as it is apart from the CHOW pointer.
        fields = dict(p["create"])
        fields["note"] = (f"{config.NOTE_TAG} {date.today().isoformat()}: Created for change of ownership; "
                          f"previous account {p['old_account']} kept for billing")
        new = crm.create_account(fields)
        crm.update_account(p["old_account"], {"chow_current_account": new["account_id"]})
        return f"Created {new['account_id']}; set chow_current_account on {p['old_account']}"

    if kind == "chow_link":
        crm.update_account(account_id, {"chow_current_account": p["chow_current_account"]})
        return f"Set chow_current_account on {account_id} -> {p['chow_current_account']}"

    raise ValueError(f"Unknown proposal kind: {kind}")
