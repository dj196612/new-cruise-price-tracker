import asyncio

from collectors.ncl import collect
from export_json import main as export_json
from storage import save_observations


async def main():
    print("Collecting Norwegian Cruise Line...")
    records = await collect(headless=True)
    print(f"NCL collector returned {len(records)} sailings.")
    if not records:
        raise SystemExit("No NCL Sydney cruises were found.")

    save_observations(records, line="Norwegian Cruise Line")
    export_json()


if __name__ == "__main__":
    asyncio.run(main())
