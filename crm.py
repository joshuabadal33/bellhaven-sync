"""Thin client for the CRM sandbox API."""
import requests

import config


class CRMError(Exception):
    pass


class CRM:
    def __init__(self, base_url=config.API_URL, token=config.API_TOKEN):
        if not token:
            raise CRMError("No API token. Set BELLHAVEN_TOKEN in .env (see README).")
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def _request(self, method, path, **kwargs):
        resp = self.session.request(method, f"{self.base_url}{path}", timeout=30, **kwargs)
        if resp.status_code >= 400:
            raise CRMError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def _list_all(self, path):
        rows, page = [], 1
        while True:
            payload = self._request("GET", path, params={"page": page, "page_size": 100})
            batch = payload.get("data", [])
            rows.extend(batch)
            if len(rows) >= payload.get("total", 0) or not batch:
                return rows
            page += 1

    def accounts(self):
        return self._list_all("/accounts")

    def contacts(self):
        return self._list_all("/contacts")

    def get_account(self, account_id):
        return self._request("GET", f"/accounts/{account_id}")

    def update_account(self, account_id, fields):
        return self._request("PATCH", f"/accounts/{account_id}", json=fields)

    def create_account(self, fields):
        created = self._request("POST", "/accounts", json=fields)
        return created.get("data", created)
