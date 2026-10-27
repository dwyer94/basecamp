"""Basecamp's own log: ``<data>\\logs\\basecamp.log``, rotated, plus stderr."""

from __future__ import annotations

import logging
import logging.handlers
import sys

from basecamp.settings import Settings

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(settings: Settings, level: int = logging.INFO) -> None:
    settings.ensure_dirs()
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, "_basecamp", False):
            root.removeHandler(handler)
            handler.close()
    file_handler = logging.handlers.RotatingFileHandler(
        settings.logs_dir / "basecamp.log", maxBytes=5 * 1024 * 1024, backupCount=5,
        encoding="utf-8",
    )
    handlers: list[logging.Handler] = [file_handler]
    if sys.stderr is not None:  # None under pythonw.exe (the prod scheduled task)
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(logging.Formatter(FORMAT))
        handler._basecamp = True  # type: ignore[attr-defined]
        root.addHandler(handler)
