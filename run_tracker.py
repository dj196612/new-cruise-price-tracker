import asyncio
from collectors.princess import collect
from storage import save_observations
from export_json import main as export_json

async def main():
    print("Collecting Princess cruises...")
    records = await collect(headless=True)
    print(f"Princess collector returned {len(records)} sailings.")
    if not records:
        raise SystemExit(
            "No Princess cruises were found. Check debug/princess-no-results.png "
            "and debug/princess-no-results.html for the current Princess page."
        )
    save_observations(records)
    export_json()

if __name__ == "__main__":
    asyncio.run(main())
