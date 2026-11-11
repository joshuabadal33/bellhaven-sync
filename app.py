"""Review app. Run `python app.py` and open http://127.0.0.1:5000

Every proposal shows its evidence. Approve writes to the CRM through the API;
reject just records the decision so the pipeline won't propose it again.
"""
from flask import Flask, redirect, render_template, request, url_for

import config
import store
from apply import BlockedError, apply
from crm import CRM, CRMError

app = Flask(__name__)

KIND_LABELS = {
    "chow_new": "Change of ownership (new account)",
    "chow_link": "Change of ownership (link)",
    "update": "Fix existing account",
    "duplicate": "Duplicate",
    "create": "New account",
    "needs_review": "Not on website",
    "possible_match": "Possible match",
}


@app.route("/")
def index():
    conn = store.connect()
    items = store.pending(conn)
    kind = request.args.get("kind")
    counts = {}
    for item in items:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    if kind:
        items = [i for i in items if i["kind"] == kind]
    return render_template(
        "index.html", items=items, counts=counts, kind=kind, labels=KIND_LABELS,
        history=store.decided(conn), last_run=store.last_run(conn), crm_browser=config.CRM_BROWSER_URL,
    )


@app.post("/decide/<path:key>")
def decide(key):
    conn = store.connect()
    proposal = store.get(conn, key)
    if not proposal or proposal["status"] not in ("pending", "failed"):
        return redirect(url_for("index"))

    note = request.form.get("reviewer_note", "").strip()
    if request.form["decision"] == "reject":
        store.mark(conn, key, "rejected", note)
    else:
        try:
            result = apply(CRM(), proposal["body"])
            store.mark(conn, key, "approved", note, result)
        except (CRMError, BlockedError) as exc:
            store.mark(conn, key, "failed", note, str(exc))
    return redirect(url_for("index", kind=request.args.get("kind")) + f"#{request.form.get('next', '')}")


@app.post("/run")
def run_pipeline():
    import pipeline
    pipeline.run()
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
