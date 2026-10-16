from __future__ import annotations

import asyncio
from urllib.parse import urlencode

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

SEARCH_API = "https://www.carnival.com.au/cruisesearch/api/search"
PORTS = ("SYD", "BNE", "MEL")
PAGE_SIZE = 50


def _headers() -> dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-AU,en;q=0.9",
        "referer": "https://www.carnival.com.au/cruise-search",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36",
    }


async def _get_json(request, *, port: str, guests: int, senior: bool, page: int) -> dict | None:
    params = {
        "pageNumber": str(page),
        "numadults": str(guests),
        "port": port,
        "pagesize": str(PAGE_SIZE),
        "sort": "FromPrice",
        "showBest": "true",
        "async": "true",
        "currency": "AUD",
        "locality": "7",
        "client": "cruisesearch",
        "senior": "true" if senior else "false",
    }
    for attempt in range(1, 3):
        try:
            response = await request.get(SEARCH_API, params=params, headers=_headers(), timeout=30000)
            if response.status != 200:
                print(
                    f"Carnival search HTTP {response.status} port={port} guests={guests} "
                    f"senior={senior} page={page} attempt={attempt}/2"
                )
                if attempt < 2:
                    await asyncio.sleep(1.5)
                    continue
                return None
            return await response.json()
        except PlaywrightTimeoutError:
            print(
                f"Carnival search timeout port={port} guests={guests} senior={senior} "
                f"page={page} attempt={attempt}/2"
            )
            if attempt < 2:
                await asyncio.sleep(1.5)
        except Exception as exc:
            print(
                f"Carnival search error port={port} guests={guests} senior={senior} "
                f"page={page}: {type(exc).__name__}: {exc}"
            )
            return None
    return None


def _room_prices(sailing: dict) -> dict[str, float | None]:
    out = {"interior": None, "oceanview": None, "balcony": None, "suite": None}
    rooms = sailing.get("rooms") or {}
    for source, target in (
        ("interior", "interior"),
        ("oceanview", "oceanview"),
        ("balcony", "balcony"),
        ("suite", "suite"),
    ):
        room = rooms.get(source) or {}
        if room.get("soldOut"):
            continue
        if (room.get("priceCurrency") or "AUD").upper() != "AUD":
            continue
        value = room.get("price")
        if isinstance(value, (int, float)):
            out[target] = float(value)
    return out


def _merge_lower(current: dict | None, candidate: dict, senior: bool) -> dict:
    if current is None:
        return {
            "prices": _room_prices(candidate),
            "sailing": candidate,
            "senior_used": {k: senior for k, v in _room_prices(candidate).items() if v is not None},
        }

    cand_prices = _room_prices(candidate)
    for cabin, cand in cand_prices.items():
        if cand is None:
            continue
        cur = current["prices"].get(cabin)
        if cur is None or cand < cur:
            current["prices"][cabin] = cand
            current["senior_used"][cabin] = senior
    return current


async def _collect_offer_set(request, *, port: str, guests: int, senior: bool) -> tuple[dict[str, dict], dict[str, dict]]:
    sailings: dict[str, dict] = {}
    itineraries: dict[str, dict] = {}
    page = 1
    last_page = 1

    while page <= last_page:
        body = await _get_json(request, port=port, guests=guests, senior=senior, page=page)
        if not body:
            break
        results = body.get("results") or {}
        last_page = int(results.get("lastPage") or 1)
        rows = results.get("itineraries") or []

        for itin in rows:
            code = (itin.get("code") or "").strip()
            dep = (itin.get("departurePortCode") or "").upper()
            if not code or dep != port:
                continue
            itineraries[code] = itin
            for sailing in itin.get("sailings") or []:
                sail_id = str(sailing.get("sailingId") or "").strip()
                sail_date = (sailing.get("departureDate") or "")[:10]
                if not sail_id or not sail_date:
                    continue
                key = f"{code}:{sail_id}:{sail_date}"
                sailings[key] = _merge_lower(sailings.get(key), sailing, senior)

        print(
            f"Carnival port={port} guests={guests} senior={senior} page={page}/{last_page}: "
            f"{len(rows)} itineraries, {len(sailings)} sailings"
        )
        page += 1

    return sailings, itineraries


def _absolute_url(path: str | None) -> str:
    if not path:
        return "https://www.carnival.com.au/cruise-search"
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return "https://www.carnival.com.au" + (path if path.startswith("/") else "/" + path)


def _itinerary_text(itin: dict) -> str:
    ports = [p for p in (itin.get("portsToDisplay") or []) if p]
    if ports:
        return " · ".join(ports)
    schedule = ((itin.get("leadSailing") or {}).get("schedule") or [])
    names = [x.get("port") for x in schedule if x.get("port") and "sea" not in x.get("port", "").lower()]
    return " · ".join(names)


def _choose_url(itin: dict, sailing: dict, guests: int, senior_flags: dict[str, bool]) -> str:
    if any(senior_flags.values()):
        url = sailing.get("sailingURL")
        if url:
            if "isOver55=N" in url:
                url = url.replace("isOver55=N", "isOver55=Y")
            return _absolute_url(url)
    url = sailing.get("sailingURL") or itin.get("itineraryURLWithSailing") or itin.get("itineraryURL")
    return _absolute_url(url)


async def collect(headless: bool = True) -> list[dict]:
    del headless
    async with async_playwright() as p:
        request = await p.request.new_context(extra_http_headers=_headers())

        records: dict[str, dict] = {}
        priced_solo = 0
        priced_double = 0

        for port in PORTS:
            one_std, one_itins = await _collect_offer_set(request, port=port, guests=1, senior=False)
            one_sen, one_sen_itins = await _collect_offer_set(request, port=port, guests=1, senior=True)
            two_std, two_itins = await _collect_offer_set(request, port=port, guests=2, senior=False)
            two_sen, two_sen_itins = await _collect_offer_set(request, port=port, guests=2, senior=True)

            one = one_std
            for key, row in one_sen.items():
                if key not in one:
                    one[key] = row
                else:
                    for cabin, value in row["prices"].items():
                        cur = one[key]["prices"].get(cabin)
                        if value is not None and (cur is None or value < cur):
                            one[key]["prices"][cabin] = value
                            one[key]["senior_used"][cabin] = True

            two = two_std
            for key, row in two_sen.items():
                if key not in two:
                    two[key] = row
                else:
                    for cabin, value in row["prices"].items():
                        cur = two[key]["prices"].get(cabin)
                        if value is not None and (cur is None or value < cur):
                            two[key]["prices"][cabin] = value
                            two[key]["senior_used"][cabin] = True

            all_itins = {}
            all_itins.update(one_itins)
            all_itins.update(one_sen_itins)
            all_itins.update(two_itins)
            all_itins.update(two_sen_itins)

            keys = sorted(set(one) | set(two))
            for key in keys:
                code = key.split(":", 1)[0]
                itin = all_itins.get(code) or {}
                solo_row = one.get(key)
                double_row = two.get(key)
                source = (double_row or solo_row or {}).get("sailing") or {}
                sail_date = (source.get("departureDate") or "")[:10]
                if not sail_date:
                    continue

                solo = (solo_row or {}).get("prices") or {"interior": None, "oceanview": None, "balcony": None, "suite": None}
                double = (double_row or {}).get("prices") or {"interior": None, "oceanview": None, "balcony": None, "suite": None}
                if any(v is not None for v in solo.values()):
                    priced_solo += 1
                if any(v is not None for v in double.values()):
                    priced_double += 1

                sailing_id = str(source.get("sailingId") or "")
                cruise_id = f"Carnival:{code}:{sailing_id}:{sail_date}"
                senior_flags = {}
                senior_flags.update((solo_row or {}).get("senior_used") or {})
                senior_flags.update((double_row or {}).get("senior_used") or {})

                records[cruise_id] = {
                    "cruise_id": cruise_id,
                    "line": "Carnival Cruise Line",
                    "name": itin.get("itineraryTitleFormatted") or itin.get("itineraryTitle") or "Carnival Cruise",
                    "ship": itin.get("shipName"),
                    "departure_port": port,
                    "arrival_port": None,
                    "departure_date": sail_date,
                    "nights": itin.get("dur"),
                    "itinerary": _itinerary_text(itin),
                    "url": _choose_url(itin, source, 2, senior_flags),
                    "available": any(v is not None for v in solo.values()) or any(v is not None for v in double.values()),
                    "currency": "AUD",
                    "fare_basis": "Carnival Fare (best of Standard / 55+)",
                    "one_guest_price": solo.get("interior"),
                    "two_guest_pp_price": double.get("interior"),
                    "two_guest_total": double.get("interior") * 2 if double.get("interior") is not None else None,
                    "one_guest_interior": solo.get("interior"),
                    "one_guest_oceanview": solo.get("oceanview"),
                    "one_guest_balcony": solo.get("balcony"),
                    "one_guest_mini_suite": None,
                    "one_guest_suite": solo.get("suite"),
                    "two_guest_interior_pp": double.get("interior"),
                    "two_guest_oceanview_pp": double.get("oceanview"),
                    "two_guest_balcony_pp": double.get("balcony"),
                    "two_guest_mini_suite_pp": None,
                    "two_guest_suite_pp": double.get("suite"),
                }

        await request.dispose()

    out = sorted(records.values(), key=lambda r: (r.get("departure_date") or "", r.get("departure_port") or "", r.get("name") or ""))
    if not out:
        raise RuntimeError("Carnival collector returned no Australian sailings")
    if priced_double == 0:
        raise RuntimeError("Carnival collector obtained no 2-guest AUD pricing")

    print(
        f"Carnival production result: {len(out)} sailings, "
        f"{priced_double} with 2-guest pricing, {priced_solo} with solo pricing"
    )
    return out
