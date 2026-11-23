"""Quiet-by-default logging setup redirecting noisy warnings to ~/.widescrape/logs/."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path

LOG_DIR = Path.home() / ".widescrape" / "logs"
LOG_FILE = LOG_DIR / "widescrape.log"

_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "ddgs",
    "primp",
    "pypdf",
    "pypdf._reader",
    "urllib3",
    "asyncio",
)


def configure_quiet_logging() -> Path:
    """Silence third-party console chatter and route warnings to disk.

    Returns:
        Path to the persistent sidecar log file.
    """
    # Must be set before importing primp/ddgs so Rust env_logger stays silent.
    os.environ.setdefault("RUST_LOG", "off")
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    # Strip console handlers so warnings never corrupt CLI or MCP stdio output.
    for handler in list(root.handlers):
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            root.removeHandler(handler)

    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8"
        )
        file_handler.setLevel(logging.WARNING)
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root.addHandler(file_handler)

    for name in _NOISY_LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(logging.ERROR)

    return LOG_FILE
