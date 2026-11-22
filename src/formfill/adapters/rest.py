"""REST adapter skeleton for a CRM API, authenticated with OAuth2 client credentials.

Read-only by construction: the only HTTP verb used against the CRM is ``GET``. The single
``POST`` goes to the identity provider's token endpoint, which is not the CRM. Any attempt to
send another verb to the CRM raises :class:`ReadOnlyViolation` before a request leaves the
process.

Adapt ``client_path`` and ``record_key`` to the target API. Credentials come from the
environment or a secret store, never from mapping files or the repository.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from .base import ClientDataSource, ClientNotFound, ClientRecord, ReadOnlyViolation


@dataclass
class OAuth2ClientCredentials:
    token_url: str
    client_id: str
    client_secret: str
    scope: str | None = None
    leeway_seconds: int = 30

    _token: str | None = None
    _expires_at: float = 0.0

    def token(self, http: httpx.Client, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        if self._token and now < self._expires_at - self.leeway_seconds:
            return self._token
        form = {"grant_type": "client_credentials"}
        if self.scope:
            form["scope"] = self.scope
        response = http.post(self.token_url, data=form, auth=(self.client_id, self.client_secret))
        response.raise_for_status()
        payload = response.json()
        self._token = payload["access_token"]
        self._expires_at = now + float(payload.get("expires_in", 300))
        return self._token


class RestCrmSource(ClientDataSource):
    def __init__(
        self,
        base_url: str,
        auth: OAuth2ClientCredentials,
        client_path: str = "/clients/{client_id}",
        list_path: str = "/clients",
        record_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.client_path = client_path
        self.list_path = list_path
        self.record_key = record_key
        self._http = httpx.Client(transport=transport, timeout=timeout)

    def _request(self, method: str, path: str) -> httpx.Response:
        if method.upper() != "GET":
            raise ReadOnlyViolation(f"{method} refused: this adapter is read-only")
        token = self.auth.token(self._http)
        return self._http.request(
            "GET",
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

    def _fetch(self, client_id: str) -> ClientRecord:
        response = self._request("GET", self.client_path.format(client_id=client_id))
        if response.status_code == 404:
            raise ClientNotFound(client_id)
        response.raise_for_status()
        data: Any = response.json()
        return data[self.record_key] if self.record_key else data

    def list_clients(self) -> list[str]:
        response = self._request("GET", self.list_path)
        response.raise_for_status()
        data = response.json()
        items = data.get("items", data) if isinstance(data, dict) else data
        return sorted(str(item["client_id"]) for item in items)

    def close(self) -> None:
        self._http.close()

    def describe(self) -> str:
        return f"rest:{self.base_url}"
