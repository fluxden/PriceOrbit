"""Central logging config shared by the web and worker processes.

Both processes call :func:`configure` at startup so their output goes to stdout
*and* to a shared ``log_file``; the Admin → Logs page tails that file to show the
combined output. The active level is stored in the database (see
``settings_store`` key ``log_level``) and can be changed at runtime.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

# Custom level below DEBUG so the UI can offer "trace".
TRACE = 5
logging.addLevelName(TRACE, "TRACE")

# UI name -> numeric level (ordered most→least severe for the selector).
LEVELS: dict[str, int] = {
    "fatal": logging.CRITICAL,
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
    "trace": TRACE,
}
LEVEL_NAMES = list(LEVELS.keys())

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_configured = False


def _numeric(level_name: str | None) -> int:
    return LEVELS.get((level_name or "info").lower(), logging.INFO)


def configure(level_name: str, log_file: str) -> None:
    """Attach stdout + rotating-file handlers (once) and set the root level."""
    global _configured
    root = logging.getLogger()
    root.setLevel(_numeric(level_name))
    if _configured:
        return
    fmt = logging.Formatter(_FMT)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)
    try:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        fileh = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=2,
                                    encoding="utf-8")
        fileh.setFormatter(fmt)
        root.addHandler(fileh)
    except Exception:  # noqa: BLE001 - file logging is best-effort (read-only FS, etc.)
        pass
    _configured = True


def set_level(level_name: str) -> None:
    """Change the active level on the running process's root logger."""
    logging.getLogger().setLevel(_numeric(level_name))


def tail(log_file: str, limit: int = 300) -> list[str]:
    """Return the last ``limit`` lines of the log file (oldest→newest)."""
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
            return [line.rstrip("\n") for line in fh.readlines()[-limit:]]
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001
        return []
