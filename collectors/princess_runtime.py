from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from . import princess as base


def _australian_detail_url(url: str) -> str:
    """Route Princess itinerary pages through the Australian market host."""
    parts = urlsplit(base.canonical_url(url))
    path = parts.path
    if not path.startswith("/en-au/"):
        path = "/en-au" + (path if path.startswith("/") else "/" + path)
    return urlunsplit(("https", "au-www.princess.com", path, parts.query, ""))


# Catalogue discovery stays on the known-working www.princess.com search page.
# Only itinerary detail requests are routed through Princess's Australian host.
base.au_detail_url = _australian_detail_url


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
