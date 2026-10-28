"""
App-wide logging. All modules call get_logger(__name__).
Call setup_logging(config.log) once at startup in main.py.

Log discipline:
  DEBUG   - internal state, values, timings
  INFO    - session/task lifecycle events
  WARNING - recoverable issues
  ERROR   - failures affecting functionality
  CRITICAL - unrecoverable only

Never log raw audio bytes, base64, pixel data, or secrets.
Log sizes and counts instead of content.
"""

import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from config import LogConfig

# Resolve log dir relative to this file (GEM-AGENT root), not CWD
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_initialized = False


def setup_logging(config: LogConfig) -> None:
    global _initialized

    os.makedirs(config.log_dir, exist_ok=True)

    log_dir = config.log_dir
    if not os.path.isabs(log_dir):
        log_dir = os.path.join(_BASE_DIR, log_dir)
    os.makedirs(log_dir, exist_ok=True)

    # Rotate old session files — keep only max_log_files
    existing = sorted(
        [f for f in os.listdir(log_dir) if f.startswith("session_") and f.endswith(".log")],
        reverse=True,
    )
    for old in existing[config.max_log_files - 1:]:
        try:
            os.remove(os.path.join(log_dir, old))
        except OSError:
            pass

    log_path = os.path.join(
        log_dir,
        f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
    )

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, config.level.upper(), logging.DEBUG))

    # Remove existing handlers to avoid duplicates on re-init
    root.handlers.clear()

    # File handler — rotates if a single session grows large
    fh = RotatingFileHandler(
        log_path,
        maxBytes=config.max_log_size_kb * 1024,
        backupCount=1,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    root.addHandler(fh)

    # Console — WARNING and above only (no debug spam)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(logging.WARNING)
    root.addHandler(ch)

    # Suppress noisy third-party loggers
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("websockets.client").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    _initialized = True
    logging.getLogger("logger").info("Logging started — file: %s", log_path)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
