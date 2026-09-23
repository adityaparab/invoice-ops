"""Default key/value log output for application processes."""

import logging


def configure_logging() -> None:
    """Enable application events while respecting an existing host logging configuration."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
