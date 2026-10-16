from __future__ import annotations

import asyncio
import json

from playwright.async_api import async_playwright

SEARCH_API = "https://www.carnival.com.au/cruisesearch/api/search"


def _params(guests: int, senior: bool) -> dict[str, str]:
    return {
        "pageNumber": "1",
        "numadults": str(guests),
        "port": "SYD",
        "pagesize": "3",
        "sort": "FromPrice",
        "showBest": "true",
        "async": "true",
        "currency": "AUD",
        "locality": "7",
        "client": "cruisesearch",
        "senior": "true" if senior else "false",
    }


def _print_shape(prefix: str, value, depth: int = 0) -> None:
    if depth > 2:
        return
    if isinstance(value, dict):
        print(f"{prefix}_DICT_KEYS " + json.dumps(list(value.keys())))
        for key, child in value.items():
            if key in {"filters", "options"}:
                continue
            if isinstance(child, list):
                print(f"{prefix}_{key}_LIST_LEN {len(child)}")
                if child and isinstance(child[0], dict):
                    print(
                        f"{prefix}_{key}_SAMPLE "
                        + json.dumps(child[0], separators=(",", ":"), ensure_ascii=False)[:12000]
                    )
            elif isinstance(child, dict):
                _print_shape(f"{prefix}_{key}", child, depth + 1)
            elif key.lower() not in {"totalexecutiontime"}:
                print(f"{prefix}_{key} {child!r}")
    elif isinstance(value, list):
        print(f"{prefix}_LIST_LEN {len(value)}")
        if value and isinstance(value[0], dict):
            print(
                f"{prefix}_SAMPLE "
                + json.dumps(value[0], separators=(",", ":"), ensure_ascii=False)[:12000]
            )


async def main() -> None:
    async with async_playwright() as p:
        request = await p.request.new_context(
            extra_http_headers={
                "accept": "application/json, text/plain, */*",
                "accept-language": "en-AU,en;q=0.9",
                "referer": "https://www.carnival.com.au/cruise-search",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36",
            }
        )

        for guests in (1, 2):
            for senior in (False, True):
                label = f"CARNIVAL_SCHEMA_G{guests}_{'SENIOR' if senior else 'STANDARD'}"
                response = await request.get(SEARCH_API, params=_params(guests, senior), timeout=30000)
                print(f"{label}_STATUS {response.status}")
                body = await response.json()
                _print_shape(label, body)

        await request.dispose()

    raise RuntimeError("Carnival schema diagnostic complete.")


if __name__ == "__main__":
    asyncio.run(main())
