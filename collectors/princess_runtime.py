from __future__ import annotations

from . import princess as base


def _valid_detail_url(url: str) -> str:
    # Princess itinerary pages use the global /itinerary-details route.
    # The previous /en-au/itinerary-details form returns a 404.
    return base.canonical_url(url)


# Patch only the broken detail-URL transformation while keeping the
# already-working 93-cruise catalogue discovery logic intact.
base.au_detail_url = _valid_detail_url


async def collect(headless: bool = True):
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

    return records
