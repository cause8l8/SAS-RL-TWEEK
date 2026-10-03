"""Bounded application logging without command lines or private user paths."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from sas_booster.constants import LOG_DIR


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("sas_booster")
    if logger.handlers:
        return logger
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_DIR / "sas_booster.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    return logger
