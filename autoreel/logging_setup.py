from __future__ import annotations

import logging
import logging.handlers

from .config import get_server_settings

_configured = False


def setup_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    _configured = True
    log_dir = get_server_settings().data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(processName)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    file = logging.handlers.RotatingFileHandler(log_dir / "autoreel.log", maxBytes=5_000_000, backupCount=3)
    file.setFormatter(fmt)
    root.addHandler(stream)
    root.addHandler(file)
    for noisy in ("httpx", "httpcore", "googleapiclient.discovery_cache", "urllib3", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
