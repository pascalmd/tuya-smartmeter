"""Tibber-Preise ueber die offizielle GraphQL-API.

Braucht einen persoenlichen Zugriffstoken von https://developer.tibber.com/
(Login mit dem normalen Tibber-Konto -> "Access Token").
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import httpx

API_URL = "https://api.tibber.com/v1-beta/gql"

HOMES_QUERY = """
{
  viewer {
    homes {
      id
      appNickname
      address { address1 postalCode city }
    }
  }
}
"""

PRICE_QUERY = """
query Prices($homeId: ID!) {
  viewer {
    home(id: $homeId) {
      currentSubscription {
        priceInfo {
          current { total energy tax startsAt level currency }
          today { total startsAt level }
          tomorrow { total startsAt level }
        }
      }
    }
  }
}
"""

LEVELS = ["VERY_CHEAP", "CHEAP", "NORMAL", "EXPENSIVE", "VERY_EXPENSIVE"]

LEVEL_LABELS = {
    "VERY_CHEAP": "sehr guenstig",
    "CHEAP": "guenstig",
    "NORMAL": "normal",
    "EXPENSIVE": "teuer",
    "VERY_EXPENSIVE": "sehr teuer",
}


class TibberError(RuntimeError):
    pass


@dataclass
class TibberClient:
    token: str
    timeout: float = 15.0

    async def _query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.token:
            raise TibberError("Kein Tibber-Token hinterlegt")
        # Der Token wandert in eine HTTP-Kopfzeile, und die vertraegt nur
        # ASCII. Steht dort etwas anderes -- ein Wort, ein Name, irgendetwas
        # mit Umlaut --, scheitert schon das Absenden, und die Meldung
        # ("ascii codec can't encode character") sagt niemandem etwas.
        if not self.token.isascii():
            falsch = next(c for c in self.token if not c.isascii())
            raise TibberError(
                f"Der Tibber-Token enthält »{falsch}«. Ein echter Token besteht "
                "nur aus Buchstaben, Ziffern, Bindestrichen und Unterstrichen — "
                "hier ist offenbar etwas anderes ins Feld geraten."
            )
        async with httpx.AsyncClient(timeout=self.timeout) as http:
            resp = await http.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                json={"query": query, "variables": variables or {}},
            )
        if resp.status_code == 401:
            raise TibberError("Tibber lehnt den Token ab (401). Token neu erzeugen.")
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise TibberError(data["errors"][0].get("message", "unbekannter Tibber-Fehler"))
        return data.get("data", {})

    async def list_homes(self) -> list[dict[str, Any]]:
        data = await self._query(HOMES_QUERY)
        homes = data.get("viewer", {}).get("homes", []) or []
        out = []
        for home in homes:
            address = home.get("address") or {}
            label = home.get("appNickname") or address.get("address1") or home.get("id", "")
            out.append({"id": home.get("id"), "label": label})
        return out

    async def prices(self, home_id: str) -> dict[str, Any]:
        """Aktueller Preis + Tagesreihen. Preise in EUR/kWh, Zeiten mit Zeitzone."""
        data = await self._query(PRICE_QUERY, {"homeId": home_id})
        home = (data.get("viewer") or {}).get("home") or {}
        subscription = home.get("currentSubscription") or {}
        info = subscription.get("priceInfo") or {}
        if not info:
            raise TibberError(
                "Kein Preisabruf moeglich - hat dieses Zuhause einen aktiven Tibber-Vertrag?"
            )
        return {
            "current": info.get("current") or {},
            "today": info.get("today") or [],
            "tomorrow": info.get("tomorrow") or [],
        }


# --------------------------------------------------------------------- Helfer


def parse_ts(value: str) -> dt.datetime:
    """Tibber liefert ISO-8601 mit Offset, z.B. 2026-08-16T14:00:00.000+02:00."""
    return dt.datetime.fromisoformat(value)


def cheapest_hours(entries: list[dict[str, Any]], count: int) -> set[str]:
    """startsAt-Werte der n guenstigsten Stunden."""
    usable = [e for e in entries if e.get("total") is not None]
    ranked = sorted(usable, key=lambda e: e["total"])[: max(0, count)]
    return {e["startsAt"] for e in ranked}


def upcoming(entries: list[dict[str, Any]], now: dt.datetime, hours: int = 12) -> list[dict[str, Any]]:
    """Die naechsten Stunden ab jetzt, fuer die Anzeige."""
    out = []
    for entry in entries:
        try:
            starts = parse_ts(entry["startsAt"])
        except (KeyError, ValueError):
            continue
        if starts + dt.timedelta(hours=1) <= now:
            continue
        out.append(
            {
                "startsAt": entry["startsAt"],
                "hour": starts.strftime("%H:%M"),
                "day": starts.strftime("%d.%m."),
                "total": entry.get("total"),
                "ct": round((entry.get("total") or 0) * 100, 2),
                "level": entry.get("level"),
                "level_label": LEVEL_LABELS.get(entry.get("level", ""), entry.get("level", "")),
            }
        )
        if len(out) >= hours:
            break
    return out
