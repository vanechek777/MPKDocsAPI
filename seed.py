import asyncio

from app.dev.seed import seed


if __name__ == "__main__":
    asyncio.run(seed())

