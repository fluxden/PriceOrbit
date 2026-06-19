"""Central logging config shared by the web and worker processes.

Both processes call :func:`configure` at startup so their output goes to stdout
*and* to a shared ``log_file``; the Admin → Logs page tails that file to show the
combined output. The active level is stored in the database (see
``settings_store`` key ``log_level``) and can be changed at runtime.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

# Log file: cap each file at 2 MB, rotate, and keep rotated files for 7 days.
LOG_MAX_BYTES = 2_000_000
LOG_KEEP_SECONDS = 7 * 24 * 3600

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


class SizedAgedRotatingHandler(RotatingFileHandler):
    """Rotate the log when it hits ``maxBytes``; keep rotated files for
    ``keep_seconds`` (by age), then delete them.

    The stdlib ``RotatingFileHandler`` retains a fixed *count* of backups, not
    an age. We rotate to timestamped names (``app.log.YYYYmmdd-HHMMSS-ffffff``)
    so pruning by modification time is straightforward.
    """

    def __init__(self, filename: str, max_bytes: int, keep_seconds: int,
                 encoding: str | None = None) -> None:
        # backupCount=0: we manage retention ourselves in _prune().
        super().__init__(filename, maxBytes=max_bytes, backupCount=0,
                         encoding=encoding)
        self.keep_seconds = keep_seconds

    def doRollover(self) -> None:  # noqa: N802 (stdlib name)
        if self.stream:
            self.stream.close()
            self.stream = None
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        rotated = f"{self.baseFilename}.{ts}"
        if os.path.exists(self.baseFilename):
            os.replace(self.baseFilename, rotated)
        self._prune()
        if not self.delay:
            self.stream = self._open()

    def _prune(self) -> None:
        cutoff = time.time() - self.keep_seconds
        for path in glob.glob(f"{glob.escape(self.baseFilename)}.*"):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:  # best-effort; never block logging on cleanup
                pass


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
        fileh = SizedAgedRotatingHandler(log_file, max_bytes=LOG_MAX_BYTES,
                                         keep_seconds=LOG_KEEP_SECONDS,
                                         encoding="utf-8")
        fileh.setFormatter(fmt)
        root.addHandler(fileh)
    except Exception:  # noqa: BLE001 - file logging is best-effort (read-only FS, etc.)
        pass
    _configured = True


def set_level(level_name: str) -> None:
    """Change the active level on the running process's root logger."""
    logging.getLogger().setLevel(_numeric(level_name))


def _read_lines(path: str) -> list[str]:
    """Read all lines of a file (oldest→newest), or ``[]`` if unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return [line.rstrip("\n") for line in fh]
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001
        return []


def tail(log_file: str, limit: int = 300) -> list[str]:
    """Return the last ``limit`` lines of the live log file (oldest→newest)."""
    return _read_lines(log_file)[-limit:]


def list_files(log_file: str) -> list[dict]:
    """List available log files newest→oldest: the live file first, then rotated
    files (newest rotation first). Each item carries display metadata."""
    out: list[dict] = []
    base = os.path.basename(log_file)
    if os.path.exists(log_file):
        out.append(_file_meta(log_file, base, "Current"))
    rotated = glob.glob(f"{glob.escape(log_file)}.*")
    rotated.sort(key=os.path.getmtime, reverse=True)
    for p in rotated:
        out.append(_file_meta(p, os.path.basename(p), None))
    return out


def _file_meta(path: str, name: str, label: str | None) -> dict:
    try:
        st = os.stat(path)
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = 0, 0.0
    return {
        "name": name,
        "label": label,
        "size_kb": round(size / 1024, 1),
        "when": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "",
    }


def read_selected(log_file: str, selected: list[str], limit: int) -> list[str]:
    """Read the chosen log files (by basename) oldest→newest and return the last
    ``limit`` lines. Names are whitelisted against :func:`list_files`, so an
    arbitrary path can't be read."""
    base_dir = os.path.dirname(log_file) or "."
    valid = {f["name"] for f in list_files(log_file)}
    ordered = list(reversed(list_files(log_file)))   # oldest→newest
    lines: list[str] = []
    for f in ordered:
        if f["name"] in selected and f["name"] in valid:
            lines.extend(_read_lines(os.path.join(base_dir, f["name"])))
    return lines[-limit:]


# Severity buckets exposed to the Logs UI (most→least severe). These are the
# user-facing names; the raw lines carry Python's level names, which we map below.
UI_LEVELS = ["FATAL", "ERROR", "WARN", "INFO", "DEBUG", "TRACE"]
_LEVELNAME_TO_UI = {
    "CRITICAL": "FATAL", "FATAL": "FATAL",
    "ERROR": "ERROR",
    "WARNING": "WARN", "WARN": "WARN",
    "INFO": "INFO",
    "DEBUG": "DEBUG",
    "TRACE": "TRACE",
}
# A full log entry: "<date> <time> [LEVEL] <logger>: <message>" (see `_FMT`).
_ENTRY_RE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[.,]\d+) "
    r"\[(?P<level>[A-Z]+)\] (?P<name>[^:]+): (?P<msg>.*)$"
)
# An access-log message body: "<client> <method> <path> <status> <duration>".
_ACCESS_RE = re.compile(
    r"^(?P<client>\S+) (?P<method>[A-Z]+) (?P<path>\S+) "
    r"(?P<status>\d{3}) (?P<dur>[\d.]+ms)$"
)


def _ts_num(ts_raw: str) -> int:
    """Turn a "2026-06-18 12:34:56,789" stamp into a sortable int (to seconds)."""
    digits = re.sub(r"\D", "", ts_raw or "")
    return int(digits[:14]) if len(digits) >= 14 else 0


def _blank_row(**over) -> dict:
    row = {"cont": False, "kind": "msg", "level": "OTHER", "time": "", "ts": 0,
           "source": "", "client": "", "method": "", "path": "", "status": "",
           "duration": "", "message": "", "raw": ""}
    row.update(over)
    return row


def parse_groups(lines: list[str]) -> list[list[dict]]:
    """Parse raw log lines into entry *groups* for the Logs table.

    Each group is a list of row dicts: the first is the primary entry, followed
    by any continuation lines (tracebacks, wrapped output). Grouping keeps those
    continuations attached to their entry when the table is sorted. Access-log
    entries (logger ``access``) are split into client/method/path/status/duration
    columns; everything else is a plain message row.
    """
    groups: list[list[dict]] = []
    last_level, last_ts = "OTHER", 0
    for line in lines:
        m = _ENTRY_RE.match(line)
        if m:
            level = _LEVELNAME_TO_UI.get(m.group("level"), "OTHER")
            ts_num = _ts_num(m.group("ts"))
            last_level, last_ts = level, ts_num
            name, msg = m.group("name"), m.group("msg")
            row = _blank_row(level=level, time=m.group("ts"), ts=ts_num,
                             source=name, raw=line)
            acc = _ACCESS_RE.match(msg) if name.strip() == "access" else None
            if acc:
                row.update(kind="access", client=acc.group("client"),
                           method=acc.group("method"), path=acc.group("path"),
                           status=acc.group("status"), duration=acc.group("dur"))
            else:
                row["message"] = msg   # logger name shown in its own Source column
            groups.append([row])
        else:
            row = _blank_row(cont=True, kind="cont", level=last_level,
                             ts=last_ts, message=line, raw=line)
            if groups:
                groups[-1].append(row)
            else:
                groups.append([row])
    return groups
