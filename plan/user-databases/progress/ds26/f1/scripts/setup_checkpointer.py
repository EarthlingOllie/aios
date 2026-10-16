"""Creates the checkpointer's tables once, so a lone test doesn't do it under a held lock."""

import asyncio

from aios.checkpoints import turn


async def main() -> None:
    async with turn(0):
        pass


asyncio.run(main())
print("ok")
