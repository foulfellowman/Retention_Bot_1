import logging
import os

LOG_FORMAT = (
    "[%(asctime)s] %(levelname)s in %(filename)s:%(lineno)d (%(funcName)s): %(message)s"
)


def configure_logging() -> None:
    """Configure root logging with a consistent stream handler and format."""
    root_logger = logging.getLogger()

    if not root_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root_logger.addHandler(handler)

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    try:
        root_logger.setLevel(level_name)
    except (ValueError, TypeError):
        root_logger.setLevel(logging.INFO)
