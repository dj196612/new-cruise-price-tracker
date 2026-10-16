from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from collectors import carnival


async def _narrow_capture(page, label: str):
    captured: list[dict] = []

    async def on_response(response):
        try:
            if len(captured) >= 25:
                return

            url = response.url
            parsed = urlparse(url)
            host = parsed.netloc.lower()
            low = url.lower()
            content_type = (response.headers.get("content-type") or "").lower()

            if not host.endswith("carnival.com.au"):
                return
            if "/_next/" in low or "/-/media/" in low:
                return
            if not any(
                key in low
                for key in (
                    "cruisesearch/api/search",
                    "/api/",
                    "booking",
                    "rate",
                    "fare",
                    "price",
                    "qualifier",
                    "sailing",
                    "stateroom",
                    "room",
                )
            ):
                return

            item = {
                "status": response.status,
                "url": carnival._safe_url(url),
                "content_type": content_type,
            }
            if "json" in content_type:
                try:
                    text = await response.text()
                    item["body"] = carnival._safe_text(" ".join(text.split())[:6000])
                except Exception as exc:
                    item["body_error"] = f"{type(exc).__name__}: {exc}"
            captured.append(item)
        except Exception:
            pass

    page.on("response", on_response)
    return captured


async def main():
    carnival._capture = _narrow_capture

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        await carnival._probe_member_rate(browser)
        await browser.close()

    raise RuntimeError("Carnival member-rate diagnostic complete.")


if __name__ == "__main__":
    asyncio.run(main())
