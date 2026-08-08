"""One place to configure logging, so every process prints a readable, timestamped
line to stderr (which launchd captures in /tmp/cairn.serve.err, and which you see
directly when you run `tt serve` or any `tt` command in a terminal).

Set CAIRN_LOG=DEBUG for more detail, or CAIRN_LOG=WARNING for less.
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def setup() -> None:
    """Idempotent: attach a stderr handler to the `cairn` logger, with a clear format.

    We configure the `cairn` logger directly (not the root) so our lines always show
    regardless of what uvicorn does to the root logger, and don't double-print."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = os.environ.get("CAIRN_LOG", "INFO").upper()
    logger = logging.getLogger("cairn")
    logger.setLevel(getattr(logging, level, logging.INFO))
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s  %(levelname)-5s  %(name)s  %(message)s", datefmt="%H:%M:%S")
        )
        logger.addHandler(handler)
    logger.propagate = False
    _CONFIGURED = True


def get(name: str) -> logging.Logger:
    """A namespaced logger, e.g. get('capture') -> 'cairn.capture'."""
    setup()
    return logging.getLogger(f"cairn.{name}")
