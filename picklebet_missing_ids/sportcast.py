"""Sportcast UI calls used by the SGM repush workflow.

Uses the same cookie login as the Sportcast admin UI. Credentials come from
SCL_UI_USERNAME and SCL_UI_PASSWORD and are never logged.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE_ENV = "SCL_UI_BASE_URL"
USER_ENV = "SCL_UI_USERNAME"
PASSWORD_ENV = "SCL_UI_PASSWORD"
DEFAULT_BASE = "https://sportcastlive.com"

# ConsumerFixtureIdSource values in Sportcast.
SOURCE_IDS = {"BetGenius": 1, "LSports": 2, "BetRadar": 4}

_TOKEN = (
    re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', re.I),
    re.compile(r'value="([^"]+)"[^>]*name="__RequestVerificationToken"', re.I),
)


class SportcastError(RuntimeError):
    """A Sportcast call failed. Messages never contain credentials or keys."""


def _base() -> str:
    base = os.environ.get(BASE_ENV, "").strip().rstrip("/") or DEFAULT_BASE
    if not base.startswith("https://"):
        raise SportcastError(f"{BASE_ENV} must be an https URL")
    return base


def missing_credentials() -> list[str]:
    return [name for name in (USER_ENV, PASSWORD_ENV) if not os.environ.get(name)]


class SportcastClient:
    def __init__(self, timeout: float = 60) -> None:
        self.base = _base()
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.logged_in = False

    def _open(self, method: str, path: str, *, query: dict[str, Any] | None = None,
              body: Any = None, form: dict[str, str] | None = None) -> tuple[int, str]:
        url = f"{self.base}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"Accept": "application/json, text/html"}
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status, response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SportcastError(f"{method} {path} failed: {getattr(exc, 'reason', exc)}") from None

    def login(self) -> None:
        missing = missing_credentials()
        if missing:
            raise SportcastError(f"missing environment variables: {', '.join(missing)}")
        path = "/Administration/Account/Login"
        query = {"returnUrl": "/Fixtures/Index"}
        status, page = self._open("GET", path, query=query)
        if status >= 400:
            raise SportcastError(f"login page returned HTTP {status}")
        token = next((m.group(1) for p in _TOKEN if (m := p.search(page))), None)
        if not token:
            raise SportcastError("login page had no antiforgery token")
        status, page = self._open(
            "POST",
            path,
            query=query,
            form={
                "__RequestVerificationToken": token,
                "UserName": os.environ[USER_ENV],
                "Password": os.environ[PASSWORD_ENV],
                "RememberMe": "false",
            },
        )
        if not any(cookie.name == "SClSession" for cookie in self.jar):
            if "Invalid login attempt" in page:
                raise SportcastError("login rejected: invalid username or password")
            if "VerifyCode" in page or "SendCode" in page:
                raise SportcastError("login needs a second factor")
            raise SportcastError(f"login did not set a session (HTTP {status})")
        self.logged_in = True

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.logged_in:
            self.login()
        status, text = self._open(method, path, **kwargs)
        if status in (301, 302) or "/Account/Login" in text[:2000]:
            raise SportcastError(f"{method} {path} redirected to login")
        if status != 200:
            raise SportcastError(f"{method} {path} returned HTTP {status}")
        try:
            data = json.loads(text) if text else {}
            # Some UI endpoints return the JSON document as a JSON string.
            if isinstance(data, str) and data[:1] in "{[":
                data = json.loads(data)
            return data
        except json.JSONDecodeError:
            raise SportcastError(f"{method} {path} did not return JSON") from None

    def get_fixture(self, fixture_id: int) -> dict[str, Any]:
        """Return the fixture definition. It includes client API keys: never log it."""
        return self._json("GET", f"/api/Fixtures/{int(fixture_id)}")

    def consumer_id(self, fixture_id: int, source: str = "BetRadar") -> str:
        data = self._json(
            "GET",
            "/Administration/Trader/GetConsumerFixtureInfo",
            query={"fixtureId": int(fixture_id), "sourceId": SOURCE_IDS[source]},
        )
        value = str(data.get("ConsumerId") or "").strip()
        return "" if value.lower() == "none" else value

    def add_consumer_id(self, fixture_id: int, feed_id: str, source: str = "BetRadar") -> str:
        data = self._json(
            "POST",
            "/Administration/Trader/AddUpdateConsumerFixtureId",
            query={"fixtureId": int(fixture_id), "sourceId": SOURCE_IDS[source], "feedId": feed_id},
        )
        return re.sub(r"<[^>]+>", " ", str(data.get("Result", ""))).strip()

    def republish(self, definition: dict[str, Any]) -> None:
        self._json(
            "POST",
            "/api/Fixtures/Publish",
            query={"isForceRunSim": "false", "isRepublish": "true"},
            body=definition,
        )
