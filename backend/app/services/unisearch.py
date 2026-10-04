from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests


DEFAULT_SERVER = "https://unisearchso.shop"
TIMEOUT = (15, 60)


class UniSearchClient:
    def __init__(self, server: str = DEFAULT_SERVER, *, username: str = "", password: str = "", token: str = ""):
        self.server = server.rstrip("/")
        self.username = username
        self.password = password
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def close(self) -> None:
        self.session.close()

    @staticmethod
    def _data(response: requests.Response) -> dict[str, Any]:
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("UniSearch returned an invalid response")
        if body.get("code") not in (None, 0, 200):
            raise ValueError("UniSearch request failed (code=%s)" % body.get("code"))
        data = body.get("data", body)
        if not isinstance(data, dict):
            raise ValueError("UniSearch returned invalid data")
        return data

    def login(self) -> str:
        if not self.username or not self.password:
            raise ValueError("UniSearch username and password are required")
        response = self.session.post(
            f"{self.server}/api/auth/login",
            json={"username": self.username, "password": self.password},
            timeout=TIMEOUT,
        )
        data = self._data(response)
        token = str(data.get("access_token") or "")
        if not token:
            raise ValueError("UniSearch login did not return an access token")
        self.token = token
        self.session.headers["Authorization"] = f"Bearer {token}"
        return token

    def search(self, keyword: str, *, cloud_type: str | None = None, refresh: bool = False) -> list[dict[str, Any]]:
        if not self.token:
            self.login()
        payload: dict[str, Any] = {"kw": keyword, "src": "all", "res": "merge", "refresh": refresh}
        if cloud_type:
            payload["cloud_types"] = [cloud_type]
        response = self.session.post(f"{self.server}/api/search", json=payload, timeout=TIMEOUT)
        if response.status_code == 401:
            self.login()
            response = self.session.post(f"{self.server}/api/search", json=payload, timeout=TIMEOUT)
        return self.format_results(self._data(response))

    @staticmethod
    def format_results(data: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(link: dict[str, Any], resource: dict[str, Any]) -> None:
            if link.get("access_mode") == "scan_transfer" or link.get("scan_transfer"):
                return
            if (link.get("resolution") or {}).get("status") == "invalid":
                return
            url = str(link.get("url") or "").strip()
            if not url.startswith(("https://", "http://")):
                return
            password = str(link.get("password") or "").strip()
            if password:
                parts = urlsplit(url)
                query = parse_qsl(parts.query, keep_blank_values=True)
                if not any(k.lower() in {"pwd", "password", "passcode"} for k, _ in query):
                    query.append(("pwd", password))
                    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
            if url in seen:
                return
            seen.add(url)
            source = resource.get("source") or {}
            out.append({
                "shareurl": url,
                "taskname": str(link.get("work_title") or resource.get("title") or link.get("title") or link.get("note") or ""),
                "content": str(resource.get("description") or link.get("note") or ""),
                "datetime": str(resource.get("datetime") or resource.get("published_at") or link.get("datetime") or ""),
                "channel": str(source.get("id") if isinstance(source, dict) else source),
                "source": "UniSearch",
            })

        for resource in data.get("resources") or []:
            if isinstance(resource, dict):
                for link in resource.get("links") or []:
                    if isinstance(link, dict):
                        add(link, resource)
        merged = data.get("merged_by_type") or {}
        if isinstance(merged, dict):
            for links in merged.values():
                if isinstance(links, list):
                    for link in links:
                        if isinstance(link, dict):
                            add(link, {})
        return out
