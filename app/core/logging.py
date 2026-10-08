from __future__ import annotations

import logging


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    # The SDK logs a partially masked HF token at INFO.
    logging.getLogger("testatlasvision").setLevel(logging.WARNING)
