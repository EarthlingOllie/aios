"""`python -m aios.run_service`: run a run service process until it's told to stop."""

import asyncio
import logging

from aios import tracing
from aios.run_service.executor import run_forever


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(run_forever())
    finally:
        tracing.flush()


if __name__ == "__main__":
    main()
