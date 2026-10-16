import asyncio

from collectors.celebrity import collect


async def main():
    print("Collecting Celebrity Cruises (diagnostic mode)...")
    await collect(headless=True)


if __name__ == "__main__":
    asyncio.run(main())
