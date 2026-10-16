import asyncio

from collectors.royal_caribbean import collect


async def main():
    print("Collecting Royal Caribbean cruises (diagnostic mode)...")
    await collect(headless=True)


if __name__ == "__main__":
    asyncio.run(main())
