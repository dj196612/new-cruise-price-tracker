from __future__ import annotations

import asyncio
import re

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

from .rcg_solo_graph import fetch_solo_prices


CONFIG = {
    "Royal Caribbean": {
        "room_url": "https://www.royalcaribbean.com/itinerary/api/v1/sailings",
        "referer": "https://www.royalcaribbean.com/aus/en/cruises",
        "prefix": "RoyalCaribbean",
    },
    "Celebrity Cruises": {
        "room_url": "https://www.celebritycruises.com/itinerary/api/v1/sailings",
        "referer": "https://www.celebritycruises.com/au/cruises",
        "prefix": "Celebrity",
    },
}


def _normalise(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _room_kind(room: dict) -> str | None:
    token = _normalise(" ".join(str(room.get(k) or "") for k in ("code", "name", "categoryCode")))
    if any(x in token for x in ("INTERIOR", "INSIDE")):
        return "interior"
    if any(x in token for x in ("OCEANVIEW", "OUTSIDE")):
        return "oceanview"
    if any(x in token for x in ("BALCONY", "VERANDA", "VERANDAH")):
        return "balcony"
    if any(x in token for x in ("DELUXE", "SUITE")):
        return "suite"
    return None


def _select_sailing(body: dict, package: str, sail_date: str) -> dict | None:
    rows = body.get("sailings") or []
    if not rows:
        return None
    target_id = f"{package}_{sail_date}"
    package_norm = _normalise(package)
    for row in rows:
        if row.get("id") == target_id:
            return row
    for row in rows:
        row_date = (row.get("sailDate") or row.get("startDate") or "")[:10]
        row_package = _normalise(row.get("packageCode") or ((row.get("itinerary") or {}).get("code")))
        if row_date == sail_date and row_package == package_norm:
            return row
    date_matches = [row for row in rows if (row.get("sailDate") or row.get("startDate") or "")[:10] == sail_date]
    if len(date_matches) == 1:
        return date_matches[0]
    if len(rows) == 1:
        return rows[0]
    return None


def _solo_prices(sailing: dict) -> dict[str, float | None]:
    out = {"interior": None, "oceanview": None, "balcony": None, "suite": None}
    for room in sailing.get("rooms") or []:
        kind = _room_kind(room)
        if not kind:
            continue
        pricing = room.get("pricing") or {}
        candidates = [pricing.get("total"), pricing.get("amount"), pricing.get("price")]
        value = next((float(v) for v in candidates if isinstance(v, (int, float)) and v > 0), None)
        if value is not None and (out[kind] is None or value < out[kind]):
            out[kind] = value
    return out


async def _fetch_solo(request, *, room_url: str, referer: str, package: str, sail_date: str) -> dict[str, float | None] | None:
    params = {
        "packageCode": package,
        "sailDate": sail_date,
        "adults": "1",
        "children": "0",
        "countryCode": "AUS",
        "currencyCode": "AUD",
        "languageCode": "en",
        "officeCode": "SYD",
    }
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-AU,en;q=0.9",
        "referer": referer,
    }
    for attempt in range(1, 3):
        try:
            response = await request.get(room_url, params=params, headers=headers, timeout=30000)
            if response.status == 404:
                return None
            if response.status != 200:
                print(f"Solo repair HTTP {response.status} package={package} date={sail_date} attempt={attempt}/2")
                if attempt < 2:
                    await asyncio.sleep(1.5)
                    continue
                return None
            body = await response.json()
            sailing = _select_sailing(body, package, sail_date)
            return _solo_prices(sailing) if sailing else None
        except PlaywrightTimeoutError:
            print(f"Solo repair timeout package={package} date={sail_date} attempt={attempt}/2")
            if attempt < 2:
                await asyncio.sleep(1.5)
        except Exception as exc:
            print(f"Solo repair error package={package} date={sail_date}: {type(exc).__name__}: {exc}")
            return None
    return None


SOLO_CABINS = ("interior", "oceanview", "balcony", "suite")


def _has_solo_gap(record: dict) -> bool:
    if record.get("two_guest_interior_pp") is None and record.get("two_guest_balcony_pp") is None:
        return False  # nothing bookable for 2 either; not a solo problem
    return any(record.get(f"one_guest_{c}") is None for c in SOLO_CABINS)


def _apply(record: dict, prices: dict[str, float | None]) -> int:
    """Fill only the solo cabins that are still missing. Returns cells filled."""
    filled = 0
    for cabin in SOLO_CABINS:
        key = f"one_guest_{cabin}"
        if record.get(key) is None and prices.get(cabin) is not None:
            record[key] = prices[cabin]
            filled += 1
    if record.get("one_guest_interior") is not None:
        record["one_guest_price"] = record["one_guest_interior"]
        record["available"] = True
    return filled


def _key(record: dict, prefix: str) -> tuple[str, str] | None:
    parts = (record.get("cruise_id") or "").split(":")
    if len(parts) < 3 or parts[0] != prefix:
        return None
    return parts[1].upper(), (record.get("departure_date") or parts[2])[:10]


def _currency_matches(records: list[dict], double: dict, prefix: str, line: str) -> bool:
    """Search 2-adult per-person prices must match the AUD itinerary prices."""
    ratios = []
    for r in records:
        key = _key(r, prefix)
        api = r.get("two_guest_interior_pp")
        graph = (double.get(key) or {}).get("interior") if key else None
        if api and graph:
            ratios.append(graph / api)
    if len(ratios) < 3:
        print(f"{line} solo currency check: only {len(ratios)} sailings to compare; accepting search prices")
        return True
    ratios.sort()
    median = ratios[len(ratios) // 2]
    ok = 0.85 <= median <= 1.15
    print(f"{line} solo currency check: median search/AUD 2-adult ratio {median:.3f} over {len(ratios)} sailings -> {'OK' if ok else 'REJECTED (not AUD?)'}")
    return ok


async def repair_missing_solo(records: list[dict], line: str) -> list[dict]:
    config = CONFIG[line]
    gaps = [r for r in records if _has_solo_gap(r)]
    before = sum(1 for r in records if r.get("one_guest_interior") is not None)
    if not gaps:
        print(f"{line} solo repair: no missing solo prices")
        return records

    # 1) Primary source: the site's own AU cruise-search GraphQL at 1 adult.
    graph, graph_double = await fetch_solo_prices(line)
    if graph and not _currency_matches(records, graph_double, config["prefix"], line):
        graph = {}
    graph_rows = graph_cells = 0
    for record in gaps:
        key = _key(record, config["prefix"])
        if key and key in graph:
            cells = _apply(record, graph[key])
            if cells:
                graph_rows += 1
                graph_cells += cells
    print(f"{line} solo repair (search graph): filled {graph_cells} cabin prices on {graph_rows}/{len(gaps)} sailings")

    # 2) Fallback: retry the itinerary API for anything still missing Interior
    #    (only helps with transient errors/timeouts from the first pass).
    missing = [r for r in records if _has_solo_gap(r) and r.get("one_guest_interior") is None]
    fixed = 0
    if missing:
        async with async_playwright() as p:
            request = await p.request.new_context(extra_http_headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36"
            })
            for record in missing:
                key = _key(record, config["prefix"])
                if not key:
                    continue
                prices = await _fetch_solo(request, room_url=config["room_url"], referer=config["referer"], package=key[0], sail_date=key[1])
                if prices and _apply(record, prices):
                    fixed += 1
                await asyncio.sleep(0.1)
            await request.dispose()
        print(f"{line} solo repair (itinerary retry): {fixed}/{len(missing)} more sailings priced")

    after = sum(1 for r in records if r.get("one_guest_interior") is not None)
    still = [r for r in records if _has_solo_gap(r) and r.get("one_guest_interior") is None]
    print(f"{line} solo result: Interior solo on {after}/{len(records)} sailings (was {before}); {len(still)} still missing")
    for r in still[:15]:
        print(f"  still missing solo: {r.get('departure_date')} {r.get('ship')} {r.get('cruise_id')}")
    return records
