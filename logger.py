"""Logging configuration for claude-watch server"""

import logging

LOG_FILE = "/tmp/claude-watch.log"

# Create logger
logger = logging.getLogger("claude-watch")
logger.setLevel(logging.DEBUG)

# Prevent duplicate handlers if module is imported multiple times
if not logger.handlers:
    # File handler - detailed logs
    fh = logging.FileHandler(LOG_FILE)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    # Console handler - info and above
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("[%(name)s] %(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)
