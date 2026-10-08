from __future__ import annotations

import asyncio
import logging
import sys

from arq import Worker

from app.core.config import get_worker_settings
from app.core.logging import configure_logging
from app.worker.main import ModelStartupError, park, worker_options
from app.worker.model import PERMANENT_LOAD_ERRORS

logger = logging.getLogger("app.worker")


def main() -> None:
    settings = get_worker_settings()
    configure_logging(settings.log_level)
    try:
        Worker(**worker_options(settings)).run()
    except ModelStartupError as exc:
        if exc.code not in PERMANENT_LOAD_ERRORS:
            # Possibly transient (network, download, memory): exit so the restart policy
            # tries again, with Docker's growing delay between attempts.
            logger.error("Exiting: model failed to load (%s)", exc.code)
            sys.exit(1)
        logger.error(
            "Model access denied (%s). Not restarting: fix HF_TOKEN or the account's access "
            "to the gated model in .env.worker, then recreate the worker. Waiting until stopped.",
            exc.code,
        )
        asyncio.run(park(settings, exc.code))


if __name__ == "__main__":
    main()
