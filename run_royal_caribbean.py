import asyncio

from collectors.royal_caribbean import collect
from export_json import main as export_json
from storage import save_observations


async def main():
    print("Collecting Royal Caribbean cruises...")
    records = await collect(headless=True)
    print(f"Royal Caribbean collector returned {len(records)} sailings.")
    if not records:
        raise SystemExit("No Royal Caribbean Sydney/Brisbane cruises were found.")

    save_observations(records, line="Royal Caribbean")
    export_json()


if __name__ == "__main__":
    asyncio.run(main())
