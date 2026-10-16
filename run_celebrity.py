import asyncio

from collectors.celebrity import collect
from collectors.solo_repair import repair_missing_solo
from export_json import main as export_json
from storage import save_observations


async def main():
    print("Collecting Celebrity Cruises...")
    records = await collect(headless=True)
    print(f"Celebrity collector returned {len(records)} sailings.")
    if not records:
        raise SystemExit("No Celebrity Sydney cruises were found.")

    records = await repair_missing_solo(records, "Celebrity Cruises")

    save_observations(records, line="Celebrity Cruises")
    export_json()


if __name__ == "__main__":
    asyncio.run(main())
