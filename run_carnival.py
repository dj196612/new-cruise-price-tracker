import asyncio

from collectors.carnival import collect


async def main():
    print("Collecting Carnival Cruise Line (diagnostic mode)...")
    await collect(headless=True)


if __name__ == "__main__":
    asyncio.run(main())
