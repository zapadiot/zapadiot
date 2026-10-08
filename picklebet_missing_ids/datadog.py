"""Check SinglesCreated delivery in the Sportcast Datadog org."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

SITE_ENV = "DD_SITE_URL"
DEFAULT_SITE = "https://openbet-feeds-sportcast.datadoghq.com"
NON_PRODUCTION = (".dev.", ".staging.", ".t1.", "stage", "test")

_DELIVERY = re.compile(
    r'Successfully sent and delivered a SinglesCreated message to Client: "(?P<client>[^"]+)".*?'
    r'Fixture: (?P<fixture>\d+),Url: "(?P<url>[^"]+)".*?Response code: (?P<code>\d+)',
    re.S,
)


@dataclass
class Delivery:
    timestamp: str
    client: str
    fixture_id: int
    url: str
    code: int

    @property
    def production(self) -> bool:
        return not any(marker in self.url for marker in NON_PRODUCTION)


def configured() -> bool:
    return bool(os.environ.get("DD_API_KEY") and os.environ.get("DD_APPLICATION_KEY"))


def search(query: str, start: str, end: str = "now", limit: int = 100) -> list[dict]:
    site = os.environ.get(SITE_ENV, "").rstrip("/") or DEFAULT_SITE
    body = {
        "filter": {"query": query, "from": start, "to": end},
        "sort": "-timestamp",
        "page": {"limit": limit},
    }
    request = urllib.request.Request(
        f"{site}/api/v2/logs/events/search",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "DD-API-KEY": os.environ.get("DD_API_KEY", ""),
            "DD-APPLICATION-KEY": os.environ.get("DD_APPLICATION_KEY", ""),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Datadog returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Datadog request failed: {getattr(exc, 'reason', exc)}") from None
    return payload.get("data", [])


def parse_deliveries(events: list[dict]) -> list[Delivery]:
    deliveries = []
    for event in events:
        attributes = event.get("attributes", {})
        match = _DELIVERY.search(str(attributes.get("message", "")))
        if match:
            deliveries.append(
                Delivery(
                    timestamp=str(attributes.get("timestamp", "")),
                    client=match["client"],
                    fixture_id=int(match["fixture"]),
                    url=match["url"],
                    code=int(match["code"]),
                )
            )
    return deliveries


def production_deliveries(fixture_id: int, client: str, start: str) -> list[Delivery]:
    query = f'service:Messaging.SinglesCreated {int(fixture_id)} "{client}" "Successfully sent"'
    return [
        delivery
        for delivery in parse_deliveries(search(query, start))
        if delivery.production
        and delivery.code == 200
        and delivery.fixture_id == fixture_id
        and delivery.client.lower() == client.lower()
    ]
