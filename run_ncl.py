import asyncio

from collectors.ncl import collect


async def main():
    print("Collecting Norwegian Cruise Line (diagnostic mode)...")
    await collect(headless=True)


if __name__ == "__main__":
    asyncio.run(main())
