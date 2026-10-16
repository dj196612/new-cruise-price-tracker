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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import async_playwright

LINES = {
    "Royal Caribbean": {
        "search_url": "https://www.royalcaribbean.com/aus/en/cruises?country=AUS&currency=AUD&search=adults%3A1%7Cchildren%3A0&sort=by%3ARECOMMENDED",
        "host": "royalcaribbean.com",
        "graph_url": "https://www.royalcaribbean.com/graph",
        "brand": "R",
        "referer": "https://www.royalcaribbean.com/aus/en/cruises",
    },
    "Celebrity Cruises": {
        "search_url": "https://www.celebritycruises.com/au/cruises?country=AUS&currency=AUD&search=adults%3A1%7Cchildren%3A0%7CdeparturePort%3ASYD&sort=by%3ARECOMMENDED",
        "host": "celebritycruises.com",
        "graph_url": "https://www.celebritycruises.com/graph",
        "brand": "C",
        "referer": "https://www.celebritycruises.com/au/cruises",
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

def _payload_from_get(url: str) -> tuple[str, dict]:
    """Persisted-query GETs carry operationName/variables/extensions in the URL."""
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    payload: dict[str, Any] = {}
    for key, value in params.items():
        if key in ("variables", "extensions"):
            try:
                payload[key] = json.loads(value)
                continue
            except Exception:
                pass
        payload[key] = value
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")), payload


def _is_search_result(body: Any) -> bool:
    try:
        return isinstance(body["data"]["cruiseSearch"]["results"], dict)
    except Exception:
        return False


async def _capture(context, line: str) -> tuple[str, str, dict, dict] | None:
    """Find the request the AU search page uses for cruiseSearch results.

    Matching is done on the RESPONSE (data.cruiseSearch.results present), so it
    works whether the page sends full query text, fragments, or a persisted
    query hash, by POST or GET.
    """
    cfg = LINES[line]
    page = await context.new_page()
    captured: dict[str, Any] = {}
    seen: list[str] = []

    other: list[str] = []

    async def on_response(resp):
        if captured:
            return
        if "graph" not in resp.url.lower():
            if resp.request.resource_type in ("xhr", "fetch") and len(other) < 40:
                other.append(f"{resp.request.method} {resp.status} {resp.url[:110]}")
            return
        req = resp.request
        op = ""
        try:
            data = req.post_data or ""
            if data:
                op = (json.loads(data) or {}).get("operationName") or ""
        except Exception:
            pass
        if not op:
            op = dict(parse_qsl(urlsplit(req.url).query)).get("operationName", "")
        try:
            body = await resp.json()
        except Exception:
            body = None
        has_results = _is_search_result(body)
        has_pricing = has_results and "stateroomClassPricing" in json.dumps(body)[:2_000_000]
        seen.append(f"{req.method} {resp.status} op={op or '?'} results={has_results} pricing={has_pricing} {resp.url[:120]}")
        if not has_results:
            return
        if req.method == "POST":
            try:
                payload = json.loads(req.post_data or "{}")
            except Exception:
                return
            url = req.url
        else:
            url, payload = _payload_from_get(req.url)
        captured.update(
            method=req.method,
            url=url,
            payload=payload,
            pricing=has_pricing,
            headers={k: v for k, v in req.headers.items() if k.lower() not in DROP_HEADERS},
        )

    page.on("response", on_response)
    status = title = "?"
    try:
        response = await page.goto(cfg["search_url"], wait_until="domcontentloaded", timeout=90000)
        status = response.status if response else "none"
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
        try:
            title = await page.title()
        except Exception:
            pass
    except Exception as exc:
        print(f"{line} solo graph: page load failed: {type(exc).__name__}: {exc}")
    finally:
        await page.close()

    if not captured:
        print(f"{line} solo graph: no cruiseSearch results seen. page status={status} title={title!r}; graph calls={len(seen)}")
        for item in seen[:8]:
            print(f"{line} solo graph call: {item}")
        for item in [o for o in other if "api" in o.lower() or "search" in o.lower()][:8]:
            print(f"{line} solo graph other xhr: {item}")
        return None
    variables = captured["payload"].get("variables") or {}
    print(
        f"{line} solo graph: captured {captured['method']} {captured['url'][:120]} "
        f"op={captured['payload'].get('operationName')!r} pricing_in_page_response={captured['pricing']} "
        f"persisted={'extensions' in captured['payload'] and 'query' not in captured['payload']} "
        f"qualifiers={variables.get('qualifiers')!r} filters={str(variables.get('filters'))[:160]!r}"
    )
    return captured["method"], captured["url"], captured["headers"], captured["payload"]


async def _send(requester, method: str, url: str, headers: dict, payload: dict):
    if method == "GET":
        params = {k: (json.dumps(v, separators=(",", ":")) if isinstance(v, (dict, list)) else v) for k, v in payload.items()}
        return await requester.get(f"{url}?{urlencode(params)}", headers=headers, timeout=45000)
    return await requester.post(url, headers=headers, data=json.dumps(payload), timeout=45000)


async def _run_query(requester, method: str, url: str, headers: dict, payload: dict, line: str, label: str):
    prices: dict[tuple[str, str], dict[str, float]] = {}
    skip, total = 0, None
    while total is None or skip < total:
        response = await _send(requester, method, url, headers, _with_page(payload, skip))
        if response.status != 200:
            print(f"{line} solo graph [{label}]: HTTP {response.status} at skip={skip}: {' '.join((await response.text()).split())[:600]}")
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


# ------------------------------------------------------------ direct query
# The trackers already call this endpoint successfully for the catalogue, so
# ask it for per-cabin prices directly (AU market, 1 vs 2 adults) before
# falling back to capturing the page's own request. Field selections are tried
# in order; GraphQL errors name the right fields and are logged.
_QUERY_HEAD = """query cruiseSearch_Cruises($filters: String, $qualifiers: String, $sort: CruiseSearchSort, $pagination: CruiseSearchPagination) {
  cruiseSearch(filters: $filters, qualifiers: $qualifiers, sort: $sort, pagination: $pagination) {
    results { total cruises { id masterSailing { itinerary { code } } sailings { id sailDate itinerary { code } stateroomClassPricing { %s } } } }
  }
}"""
PRICING_SELECTIONS = (
    "price { value } stateroomClass { id }",
    "price { value } stateroomClass { content { code } }",
    "price { value currency { code } } stateroomClass { id content { code } }",
)


def _direct_headers(line: str) -> dict:
    cfg = LINES[line]
    return {
        "accept": "*/*",
        "accept-language": "en-AU,en;q=0.9",
        "apollographql-client-name": "cruise-search-widget",
        "app-name": "graph",
        "brand": cfg["brand"],
        "content-type": "application/json",
        "country": "AUS",
        "countryalpha2code": "AU",
        "currency": "AUD",
        "language": "en",
        "office": "SYD",
        "referer": cfg["referer"],
        "user-agent": UA,
    }


def _direct_payload(selection: str) -> dict:
    return {
        "operationName": "cruiseSearch_Cruises",
        "variables": {
            "filters": "{}",
            "qualifiers": "adults:2|children:0",
            "sort": {"by": "RECOMMENDED"},
            "pagination": {"count": PAGE_SIZE, "skip": 0},
        },
        "query": _QUERY_HEAD % selection,
    }


async def _direct(p, line: str):
    request = await p.request.new_context()
    try:
        url = LINES[line]["graph_url"]
        headers = _direct_headers(line)
        for i, selection in enumerate(PRICING_SELECTIONS, 1):
            template = _direct_payload(selection)
            solo_payload, _ = _set_adults(template, 1)
            solo = await _run_query(request, "POST", url, headers, solo_payload, line, f"direct v{i} 1 adult")
            if solo is None:
                continue  # errors were logged; try the next field selection
            double_payload, _ = _set_adults(template, 2)
            double = await _run_query(request, "POST", url, headers, double_payload, line, f"direct v{i} 2 adults")
            return solo, double
        return None, None
    finally:
        await request.dispose()


def _verified(line: str, solo, double, label: str) -> dict:
    if not solo or not double:
        return {}
    # Solo must cost clearly more than the 2-adult per-person price, or the
    # adult qualifier did not take effect.
    higher = same = 0
    for key, cabins in solo.items():
        d = (double.get(key) or {}).get("interior")
        s_ = cabins.get("interior")
        if s_ is None or d is None:
            continue
        if s_ > d * 1.05:
            higher += 1
        elif abs(s_ - d) <= 1:
            same += 1
    compared = higher + same
    print(f"{line} solo graph check [{label}]: {higher}/{compared} solo > 2-adult pp, {same} identical")
    if compared < 3 or higher < compared * 0.8:
        print(f"{line} solo graph REJECTED [{label}]: 1-adult prices not distinct from 2-adult prices")
        return {}
    return solo


# --------------------------------------------------------------------- main

async def fetch_solo_prices(line: str, headless: bool = True) -> dict[tuple[str, str], dict[str, float]]:
    """Return {(PACKAGECODE, 'YYYY-MM-DD'): {cabin: AUD solo price}}; empty on any failure."""
    try:
        async with async_playwright() as p:
            # 1) Direct query to the graph endpoint.
            solo, double = await _direct(p, line)
            result = _verified(line, solo, double, "direct")
            if result:
                return result

            # 2) Fallback: capture the AU search page's own request and replay it.
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
                method, url, headers, template = captured
                solo_payload, rewrote = _set_adults(template, 1)
                double_payload, _ = _set_adults(template, 2)
                print(f"{line} solo graph: adult qualifier {'rewritten' if rewrote else 'added'}")
                solo = await _run_query(context.request, method, url, headers, solo_payload, line, "page 1 adult")
                double = await _run_query(context.request, method, url, headers, double_payload, line, "page 2 adults")
            finally:
                await browser.close()
            return _verified(line, solo, double, "page capture")
    except Exception as exc:
        print(f"{line} solo graph failed: {type(exc).__name__}: {exc}")
        return {}
