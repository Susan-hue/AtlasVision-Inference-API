from __future__ import annotations

import logging
import sys

from arq import Worker

from app.core.config import get_worker_settings
from app.core.logging import configure_logging
from app.worker.main import ModelStartupError, worker_options


def main() -> None:
    settings = get_worker_settings()
    configure_logging(settings.log_level)
    try:
        Worker(**worker_options(settings)).run()
    except ModelStartupError as exc:
        logging.getLogger("app.worker").error("Exiting: model failed to load (%s)", exc.code)
        sys.exit(1)


if __name__ == "__main__":
    main()
