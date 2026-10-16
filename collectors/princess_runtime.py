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
    """Set Princess detail-page occupancy.

    Princess does not expose "1 room / 1 guest" as a direct option. The rooms
    panel uses adult +/- controls, so for solo pricing we open the panel and
    decrement Adults from 2 to 1.
    """
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

    # Some Princess variants may expose a direct occupancy choice.
    direct = await _first_visible(
        page.get_by_text(re.compile(rf"1\s*room\s*/\s*{guests}\s*guests?", re.I))
    )
    if direct is not None:
        try:
            await direct.click(timeout=2000, force=True)
        except Exception:
            pass

    if guests == 1:
        clicked = False

        # Preferred route: accessible decrement button names.
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

        # Fallback: find the Adults row and use its first enabled button,
        # which is the minus control in Princess's rooms-and-guests panel.
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

    # Apply/confirm if the panel requires it. Some layouts update immediately.
    for label in (r"^Apply$", r"^Done$", r"^Save$", r"^Update$", r"^Continue$"):
        button = await _first_visible(page.get_by_role("button", name=re.compile(label, re.I)))
        if button is not None:
            try:
                await button.click(timeout=2200, force=True)
                break
            except Exception:
                pass

    # Wait for the occupancy summary to confirm the new guest count, then give
    # the fare component time to refresh before base.wait_for_cabin_prices runs.
    confirmed = False
    for _ in range(12):
        try:
            body = " ".join((await page.locator("body").inner_text()).split())
            if re.search(rf"1\s*room\s*/\s*{guests}\s*guests?", body, re.I):
                confirmed = True
                break
        except Exception:
            pass
        await page.wait_for_timeout(500)

    if confirmed:
        await page.wait_for_timeout(1800)
        print(f"Solo guest selector: confirmed 1 room / {guests} guest{'s' if guests != 1 else ''}")
    else:
        print(f"Solo guest selector: occupancy did not confirm {guests} guest(s)")
    return confirmed


async def collect(headless: bool = True):
    # Override the older direct-option guest selector with the current Princess
    # adult decrement control without disturbing the proven AU pricing flow.
    base.set_guest_count_on_detail = _set_guest_count

    records = await base.collect(headless=headless)

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
    print(f"Princess solo Interior pricing captured for {solo_priced}/{len(records)} cruises.")

    return records
