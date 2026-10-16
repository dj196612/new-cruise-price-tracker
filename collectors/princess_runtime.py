from __future__ import annotations

import re

from . import princess as base


async def _first_visible(locator):
    for i in range(await locator.count()):
        item = locator.nth(i)
        try:
            if await item.is_visible():
                return item
        except Exception:
            pass
    return None


async def _set_guest_count(page, guests: int) -> bool:
    """Set Princess detail-page occupancy to the requested guest count."""
    current = await _first_visible(
        page.get_by_text(re.compile(r"1\s*room\s*/\s*\d+\s*guests?", re.I))
    )
    if current is None:
        print("Solo guest selector: rooms/guests control not found")
        return False

    try:
        await current.click(timeout=3000, force=True)
        await page.wait_for_timeout(500)
    except Exception as exc:
        print(f"Solo guest selector: could not open control: {exc}")
        return False

    if guests == 1:
        clicked = False
        candidates = [
            page.get_by_role("button", name=re.compile(r"(decrease|decrement|minus|remove).*(adult|guest)", re.I)),
            page.get_by_role("button", name=re.compile(r"(adult|guest).*(decrease|decrement|minus|remove)", re.I)),
            page.locator('button[aria-label*="decrease" i]'),
            page.locator('button[aria-label*="decrement" i]'),
            page.locator('button[aria-label*="minus" i]'),
            page.locator('button[title*="decrease" i]'),
        ]
        for loc in candidates:
            button = await _first_visible(loc)
            if button is None:
                continue
            try:
                await button.click(timeout=2000, force=True)
                clicked = True
                break
            except Exception:
                pass

        if not clicked:
            adults = await _first_visible(page.get_by_text(re.compile(r"^Adults?$", re.I)))
            if adults is not None:
                node = adults
                for _ in range(5):
                    try:
                        node = node.locator("xpath=..")
                        buttons = node.locator("button")
                        visible = []
                        for i in range(await buttons.count()):
                            b = buttons.nth(i)
                            if await b.is_visible() and await b.is_enabled():
                                visible.append(b)
                        if len(visible) >= 2:
                            await visible[0].click(timeout=2000, force=True)
                            clicked = True
                            break
                    except Exception:
                        continue

        if not clicked:
            print("Solo guest selector: adult decrement button not found")
            return False

    for label in (r"^Apply$", r"^Done$", r"^Save$", r"^Update$", r"^Continue$"):
        button = await _first_visible(page.get_by_role("button", name=re.compile(label, re.I)))
        if button is not None:
            try:
                await button.click(timeout=2200, force=True)
                break
            except Exception:
                pass

    confirmed = False
    for _ in range(16):
        try:
            body = " ".join((await page.locator("body").inner_text()).split())
            if re.search(rf"1\s*room\s*/\s*{guests}\s*guests?", body, re.I):
                confirmed = True
                break
        except Exception:
            pass
        await page.wait_for_timeout(500)

    if confirmed:
        print(f"Solo guest selector: confirmed 1 room / {guests} guest{'s' if guests != 1 else ''}")
    else:
        print(f"Solo guest selector: occupancy did not confirm {guests} guest(s)")
    return confirmed


# base.scrape_detail calls wait_for_cabin_prices twice on each page: first for the
# default 2-person fares, then again after switching to 1 guest. Princess leaves
# the old 2-person prices visible briefly while its pricing API refreshes. Track
# the first set of cabin prices and require the second call to change before it
# is accepted as a solo fare.
_original_wait_for_cabin_prices = base.wait_for_cabin_prices
_first_prices_by_page: dict[int, dict] = {}


async def _wait_for_fresh_cabin_prices(page, timeout_seconds: int = 20) -> str:
    key = id(page)

    if key not in _first_prices_by_page:
        body = await _original_wait_for_cabin_prices(page, timeout_seconds)
        _first_prices_by_page[key] = base.cabin_prices(body)
        return body

    original = _first_prices_by_page[key]
    last_body = ""
    for _ in range(timeout_seconds * 2):
        try:
            last_body = " ".join((await page.locator("body").inner_text()).split())
            current = base.cabin_prices(last_body)
            has_price = any(v is not None for v in current.values())
            changed = any(current.get(c) != original.get(c) for c in base.CABINS)
            if has_price and changed:
                print(
                    "Solo fare refresh: prices changed after switching to 1 guest "
                    f"(Interior {original.get('interior')} -> {current.get('interior')})"
                )
                return last_body
        except Exception:
            pass
        await page.wait_for_timeout(500)

    # Do not copy the 2-person fares into the solo fields if Princess did not
    # return a distinct solo quote within the timeout.
    print("Solo fare refresh: no changed fare received; leaving solo pricing unavailable")
    return ""


async def collect(headless: bool = True):
    base.set_guest_count_on_detail = _set_guest_count
    base.wait_for_cabin_prices = _wait_for_fresh_cabin_prices

    try:
        records = await base.collect(headless=headless)
    finally:
        base.wait_for_cabin_prices = _original_wait_for_cabin_prices
        _first_prices_by_page.clear()

    priced = [r for r in records if r.get("two_guest_interior_pp") is not None]
    if not priced:
        raise RuntimeError(
            "Princess returned no usable cabin prices. Refusing to overwrite the live dashboard."
        )

    non_aud = [r for r in priced if (r.get("currency") or "").upper() != "AUD"]
    if non_aud:
        currencies = sorted({(r.get("currency") or "UNKNOWN").upper() for r in non_aud})
        raise RuntimeError(
            f"Princess returned non-AUD pricing ({', '.join(currencies)}). "
            "Refusing to overwrite the live dashboard."
        )

    solo_priced = sum(1 for r in records if r.get("one_guest_interior") is not None)
    identical = sum(
        1 for r in records
        if r.get("one_guest_interior") is not None
        and r.get("two_guest_interior_pp") is not None
        and r.get("one_guest_interior") == r.get("two_guest_interior_pp")
    )
    print(f"Princess solo Interior pricing captured for {solo_priced}/{len(records)} cruises; {identical} identical to 2-person pp fare.")

    return records
