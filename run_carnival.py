import asyncio

from collectors.carnival import collect
from export_json import main as export_json
from storage import save_observations


async def main():
    print("Collecting Carnival Cruise Line...")
    records = await collect(headless=True)
    print(f"Carnival collector returned {len(records)} sailings.")
    if not records:
        raise SystemExit("No Carnival cruises were found.")

    save_observations(records, line="Carnival Cruise Line")
    export_json()


if __name__ == "__main__":
    asyncio.run(main())
