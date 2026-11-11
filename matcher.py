"""Match website locations to CRM accounts and turn the differences into proposals.

Nothing in here talks to the network or writes anything. It takes the scraped
locations plus a snapshot of CRM accounts/contacts and returns a list of
proposals for a human to approve or reject.

How a location gets matched, in order of trust:
  1. Same normalized street address in the same city/state. Addresses are the
     most stable identifier we have: names change on every rebrand.
  2. Same normalized name in the same city/state (catches stale addresses,
     e.g. a PO Box on file instead of the street address).
  3. Neither, but a CRM account in the same city shares the distinctive part of
     the name. That is NOT treated as a match. It is surfaced as a low-confidence
     "possible match" so a reviewer can decide, alongside a "create" proposal.
"""
import hashlib
import json
import re

import config

CARE_TYPE_MAP = {
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
    "independent living": "Independent Living",
}

STREET_WORDS = {
    "street": "st", "st": "st", "avenue": "ave", "ave": "ave", "av": "ave",
    "road": "rd", "rd": "rd", "drive": "dr", "dr": "dr", "boulevard": "blvd", "blvd": "blvd",
    "lane": "ln", "ln": "ln", "pike": "pike", "pk": "pike", "court": "ct", "ct": "ct",
    "place": "pl", "pl": "pl", "parkway": "pkwy", "pkwy": "pkwy", "highway": "hwy", "hwy": "hwy",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}

NAME_SUBS = [
    (r"&", " and "),
    (r"\bcentre\b", "center"),
    (r"\bhealthcare\b", "health care"),
    (r"\brehab\b", "rehabilitation"),
]

# Words that say nothing about which facility it is.
GENERIC_NAME_WORDS = {
    "bellhaven", "the", "of", "at", "and", "senior", "living", "care", "center", "health",
    "nursing", "rehabilitation", "assisted", "retirement", "home", "campus", "community",
}


# ---------------------------------------------------------------- normalizing

def _words(text):
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split()


def norm_street(street):
    return " ".join(STREET_WORDS.get(w, w) for w in _words(street))


def norm_name(name):
    name = (name or "").lower()
    for pattern, repl in NAME_SUBS:
        name = re.sub(pattern, repl, name)
    words = _words(name)
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def distinctive_words(name):
    return {w for w in _words(norm_name(name)) if w not in GENERIC_NAME_WORDS}


def same_place(account, loc):
    return (account.get("billing_state") == loc["state"]
            and norm_name(account.get("billing_city")) == norm_name(loc["city"]))


def is_parent_account(account):
    return account["name"].endswith("(Parent Account)")


def must_preserve(account):
    """Billing SOP: revenue history AND open AR means billing needs the old account kept."""
    return (account.get("lifetime_revenue") or 0) > 0 and (account.get("outstanding_ar") or 0) > 0


def crm_care_type(loc):
    first = loc["care_offerings"][0] if loc["care_offerings"] else ""
    return CARE_TYPE_MAP.get(first.lower(), first)


# ---------------------------------------------------------------- proposals

def _key(kind, *parts, payload=None):
    digest = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:10]
    return ":".join([kind, *[p for p in parts if p], digest])


def _site_view(loc):
    return {
        "name": loc["name"],
        "address": f"{loc['street']}, {loc['city']}, {loc['state']} {loc['zip']}",
        "care": ", ".join(loc["care_offerings"]),
        "administrator": loc.get("administrator", ""),
        "url": loc.get("url", ""),
    }


def _account_view(acct, parents):
    return {
        "id": acct["account_id"],
        "name": acct["name"],
        "parent": parents.get(acct.get("parent_id"), "(none)"),
        "address": f"{acct.get('billing_street')}, {acct.get('billing_city')}, "
                   f"{acct.get('billing_state')} {acct.get('billing_zip')}",
        "status": acct.get("status"),
        "lifetime_revenue": acct.get("lifetime_revenue") or 0,
        "outstanding_ar": acct.get("outstanding_ar") or 0,
    }


def new_account_fields(loc, parent_id):
    return {
        "name": loc["name"],
        "parent_id": parent_id,
        "billing_street": loc["street"],
        "billing_city": loc["city"],
        "billing_state": loc["state"],
        "billing_zip": loc["zip"],
        "care_type": crm_care_type(loc),
        "phone": loc.get("phone", ""),
        "status": "Active",
    }


class Matcher:
    def __init__(self, accounts, contacts):
        self.accounts = accounts
        self.by_id = {a["account_id"]: a for a in accounts}
        self.parent_names = {a["account_id"]: a["name"] for a in accounts if is_parent_account(a)}

        operator = [a for a in accounts if a["name"] == config.OPERATOR_PARENT_NAME]
        if len(operator) != 1:
            raise ValueError(f"Expected exactly one '{config.OPERATOR_PARENT_NAME}' account")
        self.operator_id = operator[0]["account_id"]

        self.contacts_by_account = {}
        for c in contacts:
            if c.get("is_active", True):
                self.contacts_by_account.setdefault(c["account_id"], []).append(c)

        # Accounts already resolved by an earlier decision are out of the running:
        # a duplicate points at its survivor, a CHOW account points at its successor.
        self.live = [
            a for a in accounts
            if not is_parent_account(a)
            and not a.get("duplicate_of_account")
            and not a.get("chow_current_account")
        ]

    # -- helpers ---------------------------------------------------------

    def _admin_on_account(self, acct, loc):
        admin = (loc.get("administrator") or "").lower()
        return bool(admin) and any(c["name"].lower() == admin for c in self.contacts_by_account.get(acct["account_id"], []))

    def _survivor_score(self, acct, loc):
        """Which of several CRM records at one address should survive."""
        score = 0
        if acct.get("parent_id") == self.operator_id:
            score += 100
        if self._admin_on_account(acct, loc):
            score += 50
        if acct.get("phone") and acct.get("phone") == loc.get("phone"):
            score += 20
        if norm_name(acct["name"]) == norm_name(loc["name"]):
            score += 20
        if acct.get("parent_id"):
            score += 5  # an owned record beats an orphan
        if acct.get("status") == "Active":
            score += 2
        score += len(self.contacts_by_account.get(acct["account_id"], []))
        return score

    def _evidence_for(self, acct, loc, how):
        ev = [how]
        if self._admin_on_account(acct, loc):
            ev.append(f"Website administrator {loc['administrator']} is a contact on this account")
        if acct.get("phone") == loc.get("phone"):
            ev.append("Phone number matches the website")
        return ev

    # -- main entry points -------------------------------------------------

    def find_candidates(self, loc):
        street = norm_street(loc["street"])
        by_address = [a for a in self.live if same_place(a, loc) and norm_street(a.get("billing_street")) == street]
        if by_address:
            return by_address, "Street address matches the website"
        by_name = [a for a in self.live if same_place(a, loc) and norm_name(a["name"]) == norm_name(loc["name"])]
        if by_name:
            return by_name, "Name and city match the website (address on file differs)"
        return [], ""

    def run(self, locations):
        proposals, matches, matched_ids = [], [], set()

        for loc in locations:
            candidates, how = self.find_candidates(loc)
            if not candidates:
                proposals += self._unmatched_location(loc)
                continue

            candidates.sort(key=lambda a: self._survivor_score(a, loc), reverse=True)
            survivor, losers = candidates[0], candidates[1:]
            matched_ids.update(a["account_id"] for a in candidates)

            clear_winner = self._survivor_score(survivor, loc) >= 100
            for loser in losers:
                proposals.append(self._duplicate(loser, survivor, loc, clear_winner))

            fix = self._fix_existing(survivor, loc, how, contested=bool(losers))
            if fix:
                proposals.append(fix)
            else:
                matches.append({"location": loc["name"], "account_id": survivor["account_id"], "account": survivor["name"]})

        proposals += self._gone_from_website(matched_ids)
        return proposals, matches

    # -- proposal builders ---------------------------------------------------

    def _fix_existing(self, acct, loc, how, contested):
        evidence = self._evidence_for(acct, loc, how)
        site, crm = _site_view(loc), _account_view(acct, self.parent_names)
        confidence = "medium" if contested and self._survivor_score(acct, loc) < 100 else "high"
        if contested:
            evidence.append("Several CRM accounts share this address; this one was chosen to survive "
                            "(operator parent > administrator contact > phone > exact name)")

        if acct.get("parent_id") != self.operator_id and must_preserve(acct):
            # SOP: do not touch the old account. New account under Bellhaven, old one points to it.
            payload = {"create": new_account_fields(loc, self.operator_id), "old_account": acct["account_id"]}
            return {
                "key": _key("chow_new", acct["account_id"], payload=payload),
                "kind": "chow_new",
                "title": f"Change of ownership: {loc['name']}",
                "account_id": acct["account_id"],
                "location_slug": loc["slug"],
                "confidence": confidence,
                "payload": payload,
                "evidence": evidence + [
                    f"Currently under {crm['parent']}; website shows it as a Bellhaven community",
                    f"Billing SOP: lifetime revenue ${crm['lifetime_revenue']:,} and outstanding AR "
                    f"${crm['outstanding_ar']:,} are both > 0, so the old account is preserved as-is. "
                    "A new account is created under Bellhaven and chow_current_account on the old "
                    "account is set to it.",
                ],
                "site": site, "crm": crm,
            }

        changes, reasons = {}, []
        if acct.get("parent_id") != self.operator_id:
            changes["parent_id"] = self.operator_id
            reasons.append(f"parent {crm['parent']} -> {config.OPERATOR_PARENT_NAME}")
            if (acct.get("lifetime_revenue") or 0) > 0 or (acct.get("outstanding_ar") or 0) > 0:
                evidence.append(
                    f"Billing SOP checked: revenue ${crm['lifetime_revenue']:,}, AR ${crm['outstanding_ar']:,} "
                    "(not both > 0), so the existing account is re-parented directly")
            else:
                evidence.append("Billing SOP checked: no revenue history, re-parent directly")
        # Matching tolerates spelling variants, but the CRM should carry the
        # operator's exact current name (Centre vs Center, "The Arbors ...").
        if acct["name"].strip() != loc["name"].strip():
            changes["name"] = loc["name"]
            reasons.append(f"name '{acct['name']}' -> '{loc['name']}'")
        if norm_street(acct.get("billing_street")) != norm_street(loc["street"]):
            changes["billing_street"] = loc["street"]
            reasons.append(f"street '{acct.get('billing_street')}' -> '{loc['street']}'")
        if (acct.get("billing_zip") or "") != loc["zip"]:
            changes["billing_zip"] = loc["zip"]
            reasons.append(f"zip {acct.get('billing_zip')} -> {loc['zip']}")
        if acct.get("status") != "Active":
            changes["status"] = "Active"
            reasons.append(f"status {acct.get('status')} -> Active")

        if not changes:
            return None
        return {
            "key": _key("update", acct["account_id"], payload=changes),
            "kind": "update",
            "title": f"Update {acct['name']}",
            "account_id": acct["account_id"],
            "location_slug": loc["slug"],
            "confidence": confidence,
            "payload": {"changes": changes, "note": "Synced to Bellhaven website: " + "; ".join(reasons)},
            "evidence": evidence + ["Change: " + "; ".join(reasons)],
            "site": site, "crm": crm,
        }

    def _duplicate(self, loser, survivor, loc, clear_winner=True):
        payload = {"duplicate_of_account": survivor["account_id"], "status": "Inactive"}
        evidence = [
            f"Same address as {survivor['name']} ({survivor['account_id']}), which matches the website listing",
            f"{survivor['name']} was kept as the surviving record",
        ]
        if not clear_winner:
            evidence.append("None of the records is under Bellhaven or has the website's administrator as a "
                            "contact, so the survivor pick is a tie-break. Approve these together.")
        if must_preserve(loser):
            evidence.append("Warning: this record has revenue and open AR. Check with billing before approving.")
        moved = self.contacts_by_account.get(loser["account_id"], [])
        if moved:
            evidence.append("Has contacts that should be moved to the survivor by hand: "
                            + ", ".join(f"{c['name']} ({c['title']})" for c in moved))
        return {
            "key": _key("duplicate", loser["account_id"], payload=payload),
            "kind": "duplicate",
            "title": f"Mark duplicate: {loser['name']}",
            "account_id": loser["account_id"],
            "location_slug": loc["slug"],
            "confidence": "high" if clear_winner else "medium",
            "payload": {**payload, "note": f"Duplicate of {survivor['account_id']} ({survivor['name']}), "
                                           f"same address as Bellhaven website listing"},
            "evidence": evidence,
            "site": _site_view(loc), "crm": _account_view(loser, self.parent_names),
        }

    def _unmatched_location(self, loc):
        out = []
        fields = new_account_fields(loc, self.operator_id)
        out.append({
            "key": _key("create", loc["slug"], payload=fields),
            "kind": "create",
            "title": f"New account: {loc['name']}",
            "account_id": "",
            "location_slug": loc["slug"],
            "confidence": "high",
            "payload": {"create": fields},
            "evidence": [f"No CRM account at {loc['street']}, {loc['city']}, {loc['state']} "
                         "and no account with this name in that city"],
            "site": _site_view(loc), "crm": None,
        })

        # Same city + same distinctive name words, different address. Probably a
        # different facility, but a human should look before we create a near-twin.
        words = distinctive_words(loc["name"])
        for acct in self.live:
            other = distinctive_words(acct["name"])
            if words and other and same_place(acct, loc) and len(words & other) / len(words | other) >= 0.5:
                payload = {"changes": {"parent_id": self.operator_id, "name": loc["name"],
                                       "billing_street": loc["street"], "billing_zip": loc["zip"]}}
                out.append({
                    "key": _key("possible_match", acct["account_id"], loc["slug"], payload=payload),
                    "kind": "possible_match",
                    "title": f"Possible match: {loc['name']} = {acct['name']}?",
                    "account_id": acct["account_id"],
                    "location_slug": loc["slug"],
                    "confidence": "low",
                    "payload": {**payload, "note": f"Linked to Bellhaven website listing {loc['name']}"},
                    "evidence": [
                        "Similar name in the same city, but a different street address",
                        f"Approving treats this account as {loc['name']} and moves it under Bellhaven. "
                        "Reject if they are different facilities (then approve the 'New account' proposal).",
                    ],
                    "site": _site_view(loc), "crm": _account_view(acct, self.parent_names),
                })
        return out

    def _gone_from_website(self, matched_ids):
        out = []
        for acct in self.live:
            if acct.get("parent_id") != self.operator_id or acct["account_id"] in matched_ids:
                continue
            crm = _account_view(acct, self.parent_names)
            successor = next((
                a for a in self.live
                if a["account_id"] != acct["account_id"]
                and a.get("parent_id") not in ("", None, self.operator_id)
                and a.get("billing_state") == acct.get("billing_state")
                and norm_street(a.get("billing_street")) == norm_street(acct.get("billing_street"))
            ), None)

            if successor and must_preserve(acct):
                payload = {"chow_current_account": successor["account_id"]}
                out.append({
                    "key": _key("chow_link", acct["account_id"], payload=payload),
                    "kind": "chow_link",
                    "title": f"Change of ownership: {acct['name']} -> {successor['name']}",
                    "account_id": acct["account_id"],
                    "location_slug": "",
                    "confidence": "high",
                    "payload": payload,
                    "evidence": [
                        "Not listed on the Bellhaven website",
                        f"{successor['name']} ({successor['account_id']}) is at the same address under "
                        f"{self.parent_names.get(successor['parent_id'], successor['parent_id'])}: the facility was sold",
                        f"Billing SOP: revenue ${crm['lifetime_revenue']:,} and AR ${crm['outstanding_ar']:,} are both > 0, "
                        "so the old account stays as-is and points to the new owner's existing account "
                        "(no second new account needed)",
                    ],
                    "site": None, "crm": crm,
                })
                continue

            note = "Not listed on the Bellhaven website; ownership needs confirming"
            if successor:
                note += f". Possible new owner record: {successor['account_id']} ({successor['name']})"
            payload = {"status": "Needs Review", "note": note}
            out.append({
                "key": _key("needs_review", acct["account_id"], payload=payload),
                "kind": "needs_review",
                "title": f"No longer on website: {acct['name']}",
                "account_id": acct["account_id"],
                "location_slug": "",
                "confidence": "medium",
                "payload": payload,
                "evidence": [
                    "Under the Bellhaven parent in the CRM but not on the Bellhaven website",
                    "We can't tell from the website whether it closed or was sold, so it is flagged "
                    "Needs Review rather than re-parented or made Inactive",
                ],
                "site": None, "crm": crm,
            })
        return out
