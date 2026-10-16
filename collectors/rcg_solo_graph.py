"""Solo (1 adult) fares for Royal Caribbean and Celebrity from the cruise-search GraphQL.

Why this exists
---------------
The itinerary API (`/itinerary/api/v1/sailings?adults=1`) returns 2-adult pricing
for almost every sailing but 1-adult pricing for only about a quarter of them, so
solo fares were missing even though the website shows them.

The website's own AU search calls a `.../graph` endpoint (`cruiseSearch`) whose
results carry `sailings[].stateroomClassPricing[]`. With the guest qualifier set
to 1 adult those are the solo prices (this is what the local CruiseFareTracker
uses). Rather than hand-writing that request, we open the AU search page in a
real browser, capture the exact request the page sends (AU headers, cookies,
query, qualifiers), then replay it with paging and the adult count set to 1.

Safety
------
The adult count is rewritten inside the captured qualifiers. To prove the
rewrite took effect, the same query is also replayed for 2 adults: if 1-adult
prices are not clearly higher than 2-adult per-person prices on most sailings,
the solo data is discarded (logged loudly) rather than publishing 2-person
prices as solo prices. Any failure returns an empty map; the caller then keeps
whatever the itinerary API produced.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from playwright.async_api import async_playwright

LINES = {
    "Royal Caribbean": {
        "search_url": "https://www.royalcaribbean.com/aus/en/cruises",
        "host": "royalcaribbean.com",
    },
    "Celebrity Cruises": {
        "search_url": "https://www.celebritycruises.com/au/cruises",
        "host": "celebritycruises.com",
    },
}

CABIN_MAP = {
    "INTERIOR": "interior",
    "INSIDE": "interior",
    "OUTSIDE": "oceanview",
    "OCEANVIEW": "oceanview",
    "BALCONY": "balcony",
    "VERANDA": "balcony",
    "DELUXE": "suite",
    "SUITE": "suite",
}
PAGE_SIZE = 100
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0 Safari/537.36"
DROP_HEADERS = {"content-length", "host", "cookie", "accept-encoding", ":authority", ":method", ":path", ":scheme"}


# ---------------------------------------------------------------- parsing

def _num(value: Any) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if isinstance(value, dict):
        for key in ("value", "amount", "price", "total"):
            found = _num(value.get(key))
            if found is not None:
                return found
    return None


def _class_id(entry: dict) -> str:
    cls = entry.get("stateroomClass") or {}
    raw = cls.get("id") or (cls.get("content") or {}).get("code") or cls.get("code") or entry.get("id") or ""
    return re.sub(r"[^A-Z]", "", str(raw).upper())


def _sailing_key(sailing: dict, cruise: dict) -> tuple[str, str] | None:
    date = str(sailing.get("sailDate") or sailing.get("startDate") or "")[:10]
    package = ((sailing.get("itinerary") or {}).get("code") or "").strip()
    if not package:
        sid = str(sailing.get("id") or "")
        if "_" in sid:
            package = sid.rsplit("_", 1)[0]
    if not package:
        package = (((cruise.get("masterSailing") or {}).get("itinerary") or {}).get("code") or "").strip()
    if not package or len(date) != 10:
        return None
    return package.upper(), date


def _parse(body: dict) -> tuple[int, int, dict[tuple[str, str], dict[str, float]]]:
    results = (((body.get("data") or {}).get("cruiseSearch") or {}).get("results") or {})
    cruises = results.get("cruises") or []
    out: dict[tuple[str, str], dict[str, float]] = {}
    for cruise in cruises:
        for sailing in cruise.get("sailings") or []:
            key = _sailing_key(sailing, cruise)
            if not key:
                continue
            prices: dict[str, float] = {}
            for entry in sailing.get("stateroomClassPricing") or []:
                if not isinstance(entry, dict):
                    continue
                cabin = CABIN_MAP.get(_class_id(entry))
                value = _num(entry.get("price"))
                if cabin and value is not None and (cabin not in prices or value < prices[cabin]):
                    prices[cabin] = value
            if prices:
                out[key] = prices
    return int(results.get("total") or 0), len(cruises), out


# ---------------------------------------------------------- request rewrite

_ADULTS_RE = re.compile(r'(adults?["\']?\s*[:=]\s*["\']?)(\d+)', re.I)


def _set_adults(payload: dict, adults: int) -> tuple[dict, bool]:
    """Set the adult count in the captured GraphQL variables. Returns (payload, changed)."""
    payload = copy.deepcopy(payload)
    variables = payload.setdefault("variables", {})
    changed = False

    for name in ("qualifiers", "filters"):
        value = variables.get(name)
        if isinstance(value, str) and _ADULTS_RE.search(value):
            variables[name] = _ADULTS_RE.sub(lambda m: f"{m.group(1)}{adults}", value)
            changed = True
        elif isinstance(value, dict):
            for key in list(value):
                if key.lower() in ("adults", "adult"):
                    value[key] = adults if isinstance(value[key], int) else str(adults)
                    changed = True

    if not changed:
        # The page sent no guest qualifier (site default). Add one in the
        # format the site uses in its own search URLs.
        current = variables.get("qualifiers")
        if isinstance(current, str) and current.strip():
            variables["qualifiers"] = f"{current}|adults:{adults}"
        else:
            variables["qualifiers"] = f"adults:{adults}"
    return payload, changed


def _with_page(payload: dict, skip: int) -> dict:
    payload = copy.deepcopy(payload)
    payload.setdefault("variables", {})["pagination"] = {"count": PAGE_SIZE, "skip": skip}
    return payload


# ------------------------------------------------------------------ capture

async def _capture(context, line: str) -> tuple[str, dict, dict] | None:
    cfg = LINES[line]
    page = await context.new_page()
    captured: dict[str, Any] = {}

    def on_request(req):
        if captured or req.method != "POST" or cfg["host"] not in req.url or "graph" not in req.url.lower():
            return
        data = req.post_data or ""
        if "cruiseSearch" not in data or "stateroomClassPricing" not in data:
            return
        try:
            captured["payload"] = json.loads(data)
        except Exception:
            return
        captured["url"] = req.url
        captured["headers"] = {k: v for k, v in req.headers.items() if k.lower() not in DROP_HEADERS}

    page.on("request", on_request)
    try:
        await page.goto(cfg["search_url"], wait_until="domcontentloaded", timeout=90000)
        for selector in ("#onetrust-accept-btn-handler", "button:has-text('Accept All')"):
            try:
                button = page.locator(selector).first
                if await button.is_visible(timeout=1500):
                    await button.click()
                    break
            except Exception:
                pass
        for _ in range(60):
            if captured:
                break
            await page.wait_for_timeout(500)
        if not captured:
            # Results are sometimes lazy-loaded on scroll.
            await page.mouse.wheel(0, 3000)
            for _ in range(20):
                if captured:
                    break
                await page.wait_for_timeout(500)
    except Exception as exc:
        print(f"{line} solo graph: page load failed: {type(exc).__name__}: {exc}")
    finally:
        await page.close()

    if not captured:
        print(f"{line} solo graph: page never sent a cruiseSearch request with stateroomClassPricing")
        return None
    variables = captured["payload"].get("variables") or {}
    print(
        f"{line} solo graph: captured {captured['url']} "
        f"qualifiers={variables.get('qualifiers')!r} filters={str(variables.get('filters'))[:200]!r}"
    )
    return captured["url"], captured["headers"], captured["payload"]


async def _run_query(context, url: str, headers: dict, payload: dict, line: str, label: str):
    prices: dict[tuple[str, str], dict[str, float]] = {}
    skip, total = 0, None
    while total is None or skip < total:
        response = await context.request.post(url, headers=headers, data=json.dumps(_with_page(payload, skip)), timeout=45000)
        if response.status != 200:
            print(f"{line} solo graph [{label}]: HTTP {response.status} at skip={skip}")
            return None
        body = await response.json()
        if body.get("errors"):
            print(f"{line} solo graph [{label}]: GraphQL errors {str(body['errors'])[:400]}")
            return None
        page_total, count, page_prices = _parse(body)
        if total is None:
            total = page_total
        prices.update(page_prices)
        if count == 0:
            break
        skip += count
    print(f"{line} solo graph [{label}]: {len(prices)} priced sailings from {total} groups")
    return prices


# --------------------------------------------------------------------- main

async def fetch_solo_prices(line: str, headless: bool = True) -> dict[tuple[str, str], dict[str, float]]:
    """Return {(PACKAGECODE, 'YYYY-MM-DD'): {cabin: AUD solo price}}; empty on any failure."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
            context = await browser.new_context(
                locale="en-AU",
                timezone_id="Australia/Sydney",
                user_agent=UA,
                viewport={"width": 1440, "height": 1000},
            )
            try:
                captured = await _capture(context, line)
                if not captured:
                    return {}
                url, headers, template = captured

                solo_payload, rewrote = _set_adults(template, 1)
                double_payload, _ = _set_adults(template, 2)
                print(f"{line} solo graph: adult qualifier {'rewritten' if rewrote else 'added'}")

                solo = await _run_query(context, url, headers, solo_payload, line, "1 adult")
                double = await _run_query(context, url, headers, double_payload, line, "2 adults")
            finally:
                await browser.close()
    except Exception as exc:
        print(f"{line} solo graph failed: {type(exc).__name__}: {exc}")
        return {}

    if not solo or not double:
        return {}

    # Verify the qualifier really changed occupancy: solo should cost more than
    # the 2-adult per-person price on (nearly) every sailing.
    higher = same = 0
    for key, cabins in solo.items():
        d = (double.get(key) or {}).get("interior")
        s = cabins.get("interior")
        if s is None or d is None:
            continue
        if s > d * 1.05:
            higher += 1
        elif abs(s - d) <= 1:
            same += 1
    compared = higher + same
    print(f"{line} solo graph check: {higher}/{compared} solo > 2-adult pp, {same} identical")
    if compared < 3 or higher < compared * 0.8:
        print(
            f"{line} solo graph REJECTED: 1-adult prices are not distinct from 2-adult prices, "
            "so the adult qualifier did not take effect. Solo fares left unchanged."
        )
        return {}
    return solo
