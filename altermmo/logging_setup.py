"""Logging configuration: console + rotating log file, never blocking the bot.

The legacy bot only ``print``-ed (567 call sites, some of them executed for
every message on the server).  Two consequences:

* after a hang there was nothing to look at - no timestamps, no tracebacks;
* on Windows a console window in "QuickEdit" mode freezes the process on the
  next ``print`` as soon as somebody clicks into the window, until a key is
  pressed.  For an asyncio bot that means: everything stops, the gateway
  connection times out after a minute.

v2 logs through a queue: the bot thread only puts records on an in-memory
queue (cannot block), a background thread writes them to the console and to
``logs/altermmo.log`` (rotated, last 5 files kept).  QuickEdit mode is switched
off as well.
"""

from __future__ import annotations

import atexit
import logging
import logging.handlers
import queue
import sys
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_listener: logging.handlers.QueueListener | None = None


def disable_console_quick_edit() -> bool:
    """Turn off QuickEdit mode of the Windows console window (best effort)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False  # not a real console (service, IDE terminal, redirected)
        enable_quick_edit = 0x0040
        enable_extended_flags = 0x0080
        new_mode = (mode.value & ~enable_quick_edit) | enable_extended_flags
        return bool(kernel32.SetConsoleMode(handle, new_mode))
    except Exception:  # noqa: BLE001 - purely cosmetic, never fail start-up over it
        return False


def setup_logging(log_dir: Path, *, debug: bool = False) -> None:
    """Configure the root logger.  Safe to call more than once."""
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener = None

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    formatter = logging.Formatter(_FORMAT, _DATE_FORMAT)
    targets: list[logging.Handler] = []

    try:
        # Polish text and emoji must never crash on a legacy console code page.
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    targets.append(console)

    file_error: OSError | None = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "altermmo.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        targets.append(file_handler)
    except OSError as exc:
        file_error = exc

    log_queue: queue.SimpleQueue = queue.SimpleQueue()
    root.addHandler(logging.handlers.QueueHandler(log_queue))
    _listener = logging.handlers.QueueListener(log_queue, *targets, respect_handler_level=True)
    _listener.start()
    atexit.register(_stop_listener)

    disable_console_quick_edit()

    # discord.py is chatty on DEBUG; keep its reconnect / rate-limit warnings.
    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.INFO)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    if file_error is not None:
        root.error("Cannot open the log file in %s (%s); logging to console only.", log_dir, file_error)


def _stop_listener() -> None:
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener = None
